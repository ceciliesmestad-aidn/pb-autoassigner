"""Command-line interface.

Subcommands:
  init-db      — create the SQLite schema
  ingest       — fetch unassigned notes, upsert into DB
  classify     — classify pending notes
  run          — ingest + classify in one pass (what launchd calls daily)
  status       — summary counts
  verify-map   — sanity check owner→email map against PB
  train        — propose scope updates (prints rationale + diff summary)
  tender-watchdog — read-only coverage check on the Tender Commitment board
  zendesk-transfer — send PB notes tagged send-to-zendesk to Zendesk (--dry-run first)
  serve        — start the FastAPI dev server (via uvicorn)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import (
    acknowledge, db, notify, owners, pb_client, pipeline, scopes_loader, tender_watchdog, train,
    zendesk_transfer,
)
from .config import PROJECT_ROOT, load_config


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(prog="pb-assigner")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db")
    sub.add_parser("ingest")
    sub.add_parser("classify")
    p_run = sub.add_parser("run")
    p_run.add_argument(
        "--dry-run", action="store_true",
        help="Run autopilot in dry-run mode: log what WOULD have been "
             "auto-assigned and record audit rows, but do not PATCH PB. "
             "Use during the manual→autopilot ramp.",
    )
    p_run.add_argument(
        "--no-autopilot", action="store_true",
        help="Force autopilot off for this run regardless of config.toml. "
             "Useful for one-off ingest+classify when troubleshooting.",
    )
    sub.add_parser("status")
    sub.add_parser("verify-map")

    p_train = sub.add_parser("train")
    p_train.add_argument("--apply-all", action="store_true",
                          help="Apply every proposed update without interactive prompt.")

    p_watch = sub.add_parser(
        "tender-watchdog",
        help="Check that every tender with requirements in PB also has a "
             "committed-deadline field (and therefore can appear on the "
             "Tender Commitment board). Read-only — never writes to PB.",
    )
    p_watch.add_argument(
        "--no-slack", action="store_true",
        help="Print the report but do not post it to Slack.",
    )
    p_watch.add_argument(
        "--raw", action="store_true",
        help="Dump the raw feature field configuration from PB and exit. Use "
             "this once on a machine that can reach api.productboard.com to "
             "confirm the v2 payload shape.",
    )

    p_ackp = sub.add_parser(
        "ack-preview",
        help="Render the confirmation-email drafts from the last N hours to an "
             "HTML file. Drafts only — nothing is sent.",
    )
    p_ackp.add_argument("--hours", type=int, default=24)
    p_ackp.add_argument("--out", type=Path, default=Path("data/ack_preview.html"))

    p_ackt = sub.add_parser(
        "ack-test",
        help="Draft confirmations for specific PB note ids (read-only, no DB) "
             "and write an HTML preview. Nothing is sent.",
    )
    p_ackt.add_argument("note_ids", nargs="+", help="PB note UUIDs")
    p_ackt.add_argument("--out", type=Path, default=Path("data/ack_test.html"))

    p_zd = sub.add_parser(
        "zendesk-transfer",
        help="Send PB notes tagged send-to-zendesk to Zendesk. Use --dry-run "
             "first: it only reads, and lists what would happen.",
    )
    p_zd.add_argument("--dry-run", action="store_true",
                      help="Read-only: no Zendesk ticket, no PB change, no DB row.")
    p_zd.add_argument("--only", action="append", default=None, metavar="NOTE_UUID",
                      help="Only this tagged note (repeatable). A live run with "
                           "--only works even while [zendesk].enabled = false, so "
                           "one note can be tested before the daily run is switched on.")

    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--reload", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(args.config)

    if args.cmd == "init-db":
        db.init_db(cfg.db_path)
        print(f"initialised db at {cfg.db_path}")
        return 0

    if args.cmd == "serve":
        import uvicorn
        host = args.host or cfg.server.host
        port = args.port or cfg.server.port
        # Import as string so --reload works.
        uvicorn.run("backend.app:app", host=host, port=port, reload=args.reload)
        return 0

    # For everything else, ensure the DB exists.
    db.init_db(cfg.db_path)

    if args.cmd == "ingest":
        client = _pb_or_die(cfg)
        with db.connect(cfg.db_path) as conn:
            stats = pipeline.ingest(conn, client)
        print(json.dumps(stats, indent=2))
        return 0

    if args.cmd == "classify":
        with db.connect(cfg.db_path) as conn:
            stats = pipeline.classify_pending(conn, cfg)
        print(json.dumps(stats, indent=2))
        return 0

    if args.cmd == "run":
        client = _pb_or_die(cfg)
        try:
            with db.connect(cfg.db_path) as conn:
                # Zendesk transfers first, so a note a human tagged
                # send-to-zendesk is never auto-assigned to a PM. Kept separate
                # from classification and must never break the run.
                zendesk_stats: dict | None = None
                if cfg.zendesk.enabled:
                    try:
                        zendesk_stats = zendesk_transfer.run_transfers(conn, client, cfg)
                    except Exception as e:  # noqa: BLE001
                        logging.getLogger(__name__).error(
                            "zendesk: transfer scan failed (run continues): %s",
                            type(e).__name__)
                        zendesk_stats = {"blocked": f"scan crashed: {type(e).__name__}"}

                ingest_stats = pipeline.ingest(
                    conn, client,
                    exclude_tag=cfg.zendesk.trigger_tag if cfg.zendesk.enabled else "",
                )
                classify_stats = pipeline.classify_pending(conn, cfg)

                # Autopilot — auto-PATCH high-confidence suggestions when enabled.
                # `--no-autopilot` forces it off for this run; `--dry-run` forces
                # dry-run regardless of config. Otherwise the config flag
                # `autopilot_dry_run` decides — that way the UI can flip dry-run↔live
                # without editing the launchd plist. Both flags are inert when
                # autopilot_enabled = false in config.
                autopilot_stats: dict | None = None
                if cfg.classifier.autopilot_enabled and not args.no_autopilot:
                    effective_dry_run = args.dry_run or cfg.classifier.autopilot_dry_run
                    autopilot_stats = pipeline.auto_assign_high_confidence(
                        conn, client, cfg, dry_run=effective_dry_run,
                    )
                else:
                    autopilot_stats = {"skipped": "autopilot disabled"}

                # Confirmation-email drafts for newly assigned kommune notes.
                # TEST PHASE: drafts only, there is no send path. Must never
                # break the assignment run, so any failure is just logged.
                ack_stats: dict | None = None
                if cfg.acknowledge.enabled:
                    try:
                        ack_stats = acknowledge.draft_acknowledgments(conn, client, cfg)
                    except Exception as e:  # noqa: BLE001
                        logging.getLogger(__name__).exception(
                            "ack: drafting failed (run continues): %s", e)
                        ack_stats = {"error": f"{type(e).__name__}: {e}"}

                # Whatever is still 'suggested' after autopilot could not be
                # auto-assigned (below threshold, leave-open, or capped) —
                # these go into the Slack alert section.
                needs_review = db.list_suggestions_with_notes(
                    conn, states=("suggested",), limit=200,
                )
        except Exception as e:
            # The run crashed — make sure Slack hears about it, then re-raise
            # so launchd / GitHub Actions also see a non-zero exit.
            notify.send_failure(cfg, f"{type(e).__name__}: {e}")
            raise

        notify.send_run_report(
            cfg,
            ingest_stats=ingest_stats,
            classify_stats=classify_stats,
            autopilot_stats=autopilot_stats,
            needs_review=needs_review,
            zendesk_stats=zendesk_stats,
        )

        print(json.dumps({
            "zendesk": _zendesk_summary(zendesk_stats),
            "ingest": ingest_stats,
            "classify": classify_stats,
            "autopilot": autopilot_stats,
            "acknowledge": ack_stats,
            "needs_review": len(needs_review),
        }, indent=2))
        return 0

    if args.cmd == "ack-preview":
        with db.connect(cfg.db_path) as conn:
            rows = db.list_ack_drafts(conn, hours=args.hours)
        drafts = [r for r in rows if r["status"] == "draft"]
        skipped = [r for r in rows if r["status"] == "skipped"]
        out = args.out if args.out.is_absolute() else PROJECT_ROOT / args.out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(acknowledge.render_preview_html(drafts, skipped), encoding="utf-8")
        print(f"{len(drafts)} drafts, {len(skipped)} skipped → {out}")
        return 0

    if args.cmd == "ack-test":
        client = _pb_or_die(cfg)
        page = acknowledge.preview_for_note_ids(client, cfg, args.note_ids)
        out = args.out if args.out.is_absolute() else PROJECT_ROOT / args.out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(page, encoding="utf-8")
        print(f"preview → {out}")
        return 0

    if args.cmd == "zendesk-transfer":
        only = {u.strip() for u in (args.only or []) if u.strip()} or None
        if not args.dry_run and not cfg.zendesk.enabled and not only:
            print("Zendesk transfer is disabled ([zendesk].enabled = false). "
                  "Use --dry-run to preview, or --only <note-uuid> to test one note.",
                  file=sys.stderr)
            return 1
        client = _pb_or_die(cfg)
        with db.connect(cfg.db_path) as conn:
            stats = zendesk_transfer.run_transfers(conn, client, cfg,
                                                   dry_run=args.dry_run, only=only)
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        if not args.dry_run:
            notify.send_zendesk_report(cfg, stats)
        return 1 if stats.get("blocked") and not args.dry_run else 0

    if args.cmd == "tender-watchdog":
        client = _pb_or_die(cfg)

        if args.raw:
            # Shape probe: dump what PB actually returns so the parsers can be
            # tightened against reality rather than the docs.
            fields = client.feature_field_config()
            print(json.dumps(fields, indent=2, ensure_ascii=False))
            return 0

        try:
            report = tender_watchdog.run(client)
        except Exception as e:
            notify.send_failure(cfg, f"tender-watchdog: {type(e).__name__}: {e}")
            raise

        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))

        if not args.no_slack and cfg.tender_watchdog.enabled:
            notify.send_tender_watchdog_report(
                cfg, report, owner_mention=cfg.tender_watchdog.owner_mention,
            )

        # Exit non-zero only when the check could not run. Findings are the
        # normal case and must not turn the workflow red — a red badge every
        # week trains everyone to ignore it.
        return 1 if report.inconclusive else 0

    if args.cmd == "status":
        with db.connect(cfg.db_path) as conn:
            stats = db.dashboard_stats(conn)
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "verify-map":
        client = _pb_or_die(cfg)
        emails = list(dict.fromkeys(p.owner_email for p in owners.get_all()))
        counts = pb_client.verify_owner_emails(client, emails)
        for email in emails:
            c = counts.get(email, 0)
            flag = "  " if c > 0 else ("??" if c == 0 else "!!")
            print(f"{flag}  {email:40s} {c}")
        return 0

    if args.cmd == "train":
        with db.connect(cfg.db_path) as conn:
            proposals = train.propose_scope_updates(
                conn, cfg.anthropic, cfg.training, cfg.scopes_dir
            )
            if not proposals:
                print("No proposals generated (no PMs met the min-notes threshold).")
                return 0

            for p in proposals:
                print(f"\n=== {p.pm_email}  (sample_size={p.sample_size}, changed={p.changed}) ===")
                print(f"rationale: {p.rationale_no}")
                if not args.apply_all:
                    ans = input(f"apply update to {p.pm_email}? [y/N] ").strip().lower()
                    if ans != "y":
                        print("  skipped")
                        continue
                train.apply_update(conn, cfg.scopes_dir, p)
                print(f"  applied → scopes/{owners.get_by_email(p.pm_email).scope_file}.yaml")
        return 0

    parser.error(f"unknown command: {args.cmd}")
    return 2


def _zendesk_summary(stats: dict | None) -> dict | None:
    """Counts only — no per-note list in the run's stdout (GitHub Actions log)."""
    if stats is None:
        return None
    return {k: v for k, v in stats.items() if k != "notes"}


def _pb_or_die(cfg) -> pb_client.PBClient:
    if not cfg.productboard.token:
        print("ERROR: Productboard token missing. Set [productboard].token or PB_TOKEN.",
              file=sys.stderr)
        sys.exit(1)
    return pb_client.PBClient(
        token=cfg.productboard.token,
        ssl_verify=cfg.productboard.ssl_verify,
        patch_delay_seconds=cfg.productboard.patch_delay_seconds,
        api_version=cfg.productboard.api_version,
        workspace=cfg.productboard.workspace,
    )


if __name__ == "__main__":
    sys.exit(main())
