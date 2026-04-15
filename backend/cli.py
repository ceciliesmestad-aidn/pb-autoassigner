"""Command-line interface.

Subcommands:
  init-db      — create the SQLite schema
  ingest       — fetch unassigned notes, upsert into DB
  classify     — classify pending notes
  run          — ingest + classify in one pass (what launchd calls daily)
  status       — summary counts
  verify-map   — sanity check owner→email map against PB
  train        — propose scope updates (prints rationale + diff summary)
  serve        — start the FastAPI dev server (via uvicorn)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import db, owners, pb_client, pipeline, scopes_loader, train
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
    sub.add_parser("run")
    sub.add_parser("status")
    sub.add_parser("verify-map")

    p_train = sub.add_parser("train")
    p_train.add_argument("--apply-all", action="store_true",
                          help="Apply every proposed update without interactive prompt.")

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
        with db.connect(cfg.db_path) as conn:
            ingest_stats = pipeline.ingest(conn, client)
            classify_stats = pipeline.classify_pending(conn, cfg)
        print(json.dumps({"ingest": ingest_stats, "classify": classify_stats}, indent=2))
        return 0

    if args.cmd == "status":
        with db.connect(cfg.db_path) as conn:
            stats = db.dashboard_stats(conn)
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "verify-map":
        client = _pb_or_die(cfg)
        emails = [p.email for p in owners.PMS]
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


def _pb_or_die(cfg) -> pb_client.PBClient:
    if not cfg.productboard.token:
        print("ERROR: Productboard token missing. Set [productboard].token or PB_TOKEN.",
              file=sys.stderr)
        sys.exit(1)
    return pb_client.PBClient(
        token=cfg.productboard.token,
        ssl_verify=cfg.productboard.ssl_verify,
        patch_delay_seconds=cfg.productboard.patch_delay_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())
