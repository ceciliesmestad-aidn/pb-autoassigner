"""
One-off migration: move every note Abraham Guzman owns to Silje Larsen.

From 2026-10-05 Silje is interim PM for Team IAM, replacing Abraham. Same team,
same scope — so this is a straight owner swap: no re-classification and no team
tag change (both are "Team IAM").

Usage:
    # Step 1 — preview only. Reads PB, writes a JSON file. No changes.
    python -m scripts.migrate_abraham_to_silje --preview

    # Step 2 — actually PATCH PB. Only run after checking the preview.
    python -m scripts.migrate_abraham_to_silje --apply

Output:
    data/migration_abraham_silje.json  — the list of notes that will be moved

Archived notes are not included (list_notes filters archived=false); they stay
with Abraham. --apply reads the preview file, so it moves exactly what you
reviewed. Idempotent: re-running is safe.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config as cfg_mod
from backend import pb_client

ABRAHAM = "abraham.guzman@aidn.no"
SILJE = "silje.larsen@aidn.no"

PREVIEW_PATH = Path(__file__).resolve().parent.parent / "data" / "migration_abraham_silje.json"

log = logging.getLogger("migration")


def _client(cfg: cfg_mod.Config) -> pb_client.PBClient:
    return pb_client.PBClient(
        cfg.productboard.token,
        ssl_verify=cfg.productboard.ssl_verify,
        api_version=cfg.productboard.api_version,
        patch_delay_seconds=cfg.productboard.patch_delay_seconds,
        workspace=cfg.productboard.workspace,
    )


def build_preview(cfg: cfg_mod.Config) -> dict:
    client = _client(cfg)
    log.info("fetching Abraham's notes from Productboard…")
    raws = list(client.list_notes(owner_email=ABRAHAM))
    rows = []
    for r in raws:
        flat = pb_client.flatten_note(r)
        rows.append({
            "pb_uuid": flat["pb_uuid"] or r.get("id", ""),
            "title": flat["title"] or "",
            "company": flat.get("company") or "",
            "display_url": flat.get("display_url") or "",
            "pb_created_at": flat.get("pb_created_at") or "",
        })
    log.info("Abraham currently owns %d (non-archived) notes", len(rows))
    return {"summary": {"from": ABRAHAM, "to": SILJE, "count": len(rows)}, "notes": rows}


def write_preview(preview: dict) -> None:
    PREVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREVIEW_PATH.write_text(json.dumps(preview, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("preview written to %s", PREVIEW_PATH)
    log.info("%d notes will move to Silje. To do it, run:", preview["summary"]["count"])
    log.info("    python -m scripts.migrate_abraham_to_silje --apply")


def apply_preview(cfg: cfg_mod.Config) -> None:
    if not PREVIEW_PATH.exists():
        sys.exit(f"no preview file at {PREVIEW_PATH}; run --preview first")
    targets = json.loads(PREVIEW_PATH.read_text(encoding="utf-8")).get("notes", [])
    if not targets:
        log.info("nothing to move — preview is empty")
        return

    client = _client(cfg)
    log.info("PATCHing %d notes Abraham → Silje…", len(targets))
    ok, errors = 0, []
    for row in targets:
        uuid = row["pb_uuid"]
        try:
            status = client.assign(uuid, SILJE)
            if 200 <= status < 300:
                ok += 1
                log.info("  %s ✓ — %s", uuid, row["title"][:60])
            else:
                errors.append({**row, "status": status})
                log.warning("  %s ✗ status=%d", uuid, status)
        except Exception as e:  # noqa: BLE001
            errors.append({**row, "error": str(e)})
            log.warning("  %s ✗ %s", uuid, e)
            if "422" in str(e) and ok == 0:
                sys.exit("422 on the first note — PB doesn't recognise silje.larsen@aidn.no. "
                         "Add her as a member in Productboard, then re-run --apply.")

    log.info("done: %d moved, %d errors", ok, len(errors))
    if errors:
        err_path = PREVIEW_PATH.with_suffix(".errors.json")
        err_path.write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
        log.warning("error details written to %s", err_path)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--preview", action="store_true", help="list Abraham's notes, no PATCH")
    g.add_argument("--apply", action="store_true", help="move the previewed notes to Silje")
    args = parser.parse_args()
    cfg = cfg_mod.load_config()
    if args.preview:
        write_preview(build_preview(cfg))
    else:
        apply_preview(cfg)


if __name__ == "__main__":
    main()
