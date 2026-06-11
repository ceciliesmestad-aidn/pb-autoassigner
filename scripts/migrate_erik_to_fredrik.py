"""
One-off migration: split Erik Story's currently-assigned PB notes between
Erik (Team AI & Automation) and Fredrik Pedersen (Team Patient).

Erik used to wear two hats. From 2026-04-28 Fredrik owns Patient. We re-classify
every note Erik currently owns against the *new* split scopes and reassign the
ones that should belong to Fredrik.

Usage:
    # Step 1 — preview only. Reads PB, classifies, writes a JSON file.
    python -m scripts.migrate_erik_to_fredrik --preview

    # Step 2 — actually PATCH PB. Only run after eyeballing the preview file.
    python -m scripts.migrate_erik_to_fredrik --apply

Output:
    data/migration_erik_fredrik.json  — the preview / decision file

The --apply step reads that preview file (the SAME run, no re-classification)
so you patch exactly what you reviewed. If you want fresh decisions, delete
the file and run --preview again.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Make `backend.*` importable when run as `python scripts/migrate_erik_to_fredrik.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config as cfg_mod
from backend import pb_client, scopes_loader
from backend.classify import Classifier, NoteForClassification

ERIK = "erik.story@aidn.no"
FREDRIK = "fredrik.pedersen@aidn.no"

PREVIEW_PATH = Path(__file__).resolve().parent.parent / "data" / "migration_erik_fredrik.json"

log = logging.getLogger("migration")


def fetch_eriks_notes(client: pb_client.PBClient) -> list[dict]:
    """Pull every PB note currently owned by Erik."""
    log.info("fetching Erik's notes from Productboard…")
    notes = list(client.list_notes(owner_email=ERIK))
    log.info("Erik currently owns %d notes in PB", len(notes))
    return notes


def to_classification_input(raw: dict) -> NoteForClassification:
    flat = pb_client.flatten_note(raw)
    return NoteForClassification(
        note_id=flat["pb_uuid"] or raw.get("id", ""),
        title=flat["title"] or "",
        content=flat["content"] or "",
        tags=json.loads(flat["tags_json"]) if flat.get("tags_json") else [],
        company=flat.get("company") or "",
    )


def build_preview(cfg: cfg_mod.Config) -> dict:
    """Classify Erik's notes against the new scopes, return the preview structure."""
    scopes = scopes_loader.load_all(cfg.scopes_dir)
    classifier = Classifier(cfg.anthropic, scopes)
    client = pb_client.PBClient(
        cfg.productboard.token,
        ssl_verify=cfg.productboard.ssl_verify,
        api_version=cfg.productboard.api_version,
        patch_delay_seconds=cfg.productboard.patch_delay_seconds,
    )

    raws = fetch_eriks_notes(client)
    notes = [to_classification_input(r) for r in raws]
    by_uuid = {pb_client.flatten_note(r)["pb_uuid"]: r for r in raws}

    log.info("classifying %d notes against the new split scopes…", len(notes))
    classifications = classifier.classify_with_escalation(notes)
    by_id = {c.note_id: c for c in classifications}

    decisions = {"to_fredrik": [], "stays_with_erik": [], "other_or_open": []}

    for n in notes:
        c = by_id.get(n.note_id)
        if c is None:
            continue
        flat = pb_client.flatten_note(by_uuid[n.note_id])
        row = {
            "pb_uuid": n.note_id,
            "title": n.title,
            "display_url": flat.get("display_url") or "",
            "company": flat.get("company") or "",
            "suggested_pm": c.pm_email,
            "confidence": c.confidence,
            "reasoning": c.reasoning,
            "model": c.model,
            "escalated": c.escalated,
        }
        if c.pm_email == FREDRIK:
            decisions["to_fredrik"].append(row)
        elif c.pm_email == ERIK:
            decisions["stays_with_erik"].append(row)
        else:
            decisions["other_or_open"].append(row)

    summary = {
        "total_eriks_notes": len(notes),
        "to_fredrik": len(decisions["to_fredrik"]),
        "stays_with_erik": len(decisions["stays_with_erik"]),
        "other_or_open": len(decisions["other_or_open"]),
        "scopes_hash": scopes.combined_hash,
    }
    log.info("summary: %s", summary)
    return {"summary": summary, "decisions": decisions}


def write_preview(preview: dict) -> None:
    PREVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREVIEW_PATH.write_text(
        json.dumps(preview, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("preview written to %s", PREVIEW_PATH)


def apply_preview(cfg: cfg_mod.Config) -> None:
    """PATCH every note in preview.decisions.to_fredrik to Fredrik's email.

    Reads the preview file rather than re-classifying — so you patch exactly
    what you reviewed. Idempotent: PB just sets the owner; re-running is safe.
    """
    if not PREVIEW_PATH.exists():
        sys.exit(f"no preview file at {PREVIEW_PATH}; run --preview first")

    preview = json.loads(PREVIEW_PATH.read_text(encoding="utf-8"))
    targets = preview.get("decisions", {}).get("to_fredrik", [])
    if not targets:
        log.info("nothing to patch — to_fredrik list is empty")
        return

    client = pb_client.PBClient(
        cfg.productboard.token,
        ssl_verify=cfg.productboard.ssl_verify,
        api_version=cfg.productboard.api_version,
        patch_delay_seconds=cfg.productboard.patch_delay_seconds,
    )

    log.info("PATCHing %d notes Erik → Fredrik…", len(targets))
    ok = 0
    errors: list[dict] = []
    for row in targets:
        uuid = row["pb_uuid"]
        try:
            status = client.assign(uuid, FREDRIK)
            if 200 <= status < 300:
                ok += 1
                log.info("  %s ✓ (%d) — %s", uuid, status, row["title"][:60])
            else:
                errors.append({"pb_uuid": uuid, "status": status, "title": row["title"]})
                log.warning("  %s ✗ status=%d", uuid, status)
        except Exception as e:  # PBError or other
            errors.append({"pb_uuid": uuid, "error": str(e), "title": row["title"]})
            log.warning("  %s ✗ %s", uuid, e)

    log.info("done: %d ok, %d errors", ok, len(errors))
    if errors:
        err_path = PREVIEW_PATH.with_suffix(".errors.json")
        err_path.write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
        log.warning("error details written to %s", err_path)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--preview", action="store_true", help="classify Erik's notes, write preview JSON, no PATCH")
    g.add_argument("--apply", action="store_true", help="PATCH the notes flagged for Fredrik in the preview file")
    args = parser.parse_args()

    cfg = cfg_mod.load_config()

    if args.preview:
        write_preview(build_preview(cfg))
    else:
        apply_preview(cfg)


if __name__ == "__main__":
    main()
