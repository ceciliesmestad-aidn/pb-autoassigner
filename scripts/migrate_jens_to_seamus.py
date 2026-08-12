"""
One-off migration: move analytics/reporting notes from Jens (Team Back Office)
to Séamus Beirne (Team Data & Analytics).

From 2026-08-12 Séamus owns rapportering, analyse, statistikk, KOSTRA/KPR/IPLOS,
Power BI, Aidn Analytics and dashboards. We re-classify every note Jens
currently owns against the new split scopes and reassign the clear
Data & Analytics notes to Séamus. Economy, plassadministrasjon and
hjelpemidler stay with Jens.

Usage:
    # Step 1 — preview only. Reads PB, classifies, writes a JSON file. No changes.
    python -m scripts.migrate_jens_to_seamus --preview

    # Step 2 — actually PATCH PB. Only run after eyeballing the preview file.
    python -m scripts.migrate_jens_to_seamus --apply

Output:
    data/migration_jens_seamus.json  — the preview / decision file

Only notes the classifier routes to Séamus with confidence >= 0.7 are moved
on --apply. Lower-confidence candidates are listed in the preview under
`to_seamus_low_confidence` for manual review (reassign in PB by hand if
they belong there). Moved notes also get their team tag swapped:
"Team Back Office" removed, "Team Data & Analytics" added.

The --apply step reads the preview file (the SAME run, no re-classification)
so you patch exactly what you reviewed. For fresh decisions, delete the file
and run --preview again.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Make `backend.*` importable when run as `python scripts/migrate_jens_to_seamus.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config as cfg_mod
from backend import owners, pb_client, scopes_loader
from backend.classify import Classifier, NoteForClassification

JENS = "jens.malm@aidn.no"
SEAMUS = "seamus.beirne@aidn.no"
APPLY_MIN_CONFIDENCE = 0.7

OLD_TAG = owners.team_tag("Team Back Office")        # "Team Back Office"
NEW_TAG = owners.team_tag("Team Data & Analytics")   # "Team Data & Analytics"

PREVIEW_PATH = Path(__file__).resolve().parent.parent / "data" / "migration_jens_seamus.json"

log = logging.getLogger("migration")


def _client(cfg: cfg_mod.Config) -> pb_client.PBClient:
    return pb_client.PBClient(
        cfg.productboard.token,
        ssl_verify=cfg.productboard.ssl_verify,
        api_version=cfg.productboard.api_version,
        patch_delay_seconds=cfg.productboard.patch_delay_seconds,
        workspace=cfg.productboard.workspace,
    )


def fetch_jens_notes(client: pb_client.PBClient) -> list[dict]:
    """Pull every PB note currently owned by Jens."""
    log.info("fetching Jens's notes from Productboard…")
    notes = list(client.list_notes(owner_email=JENS))
    log.info("Jens currently owns %d notes in PB", len(notes))
    return notes


def to_classification_input(raw: dict) -> NoteForClassification:
    flat = pb_client.flatten_note(raw)
    return NoteForClassification(
        note_id=flat["pb_uuid"] or raw.get("id", ""),
        title=flat["title"] or "",
        content=flat["content"] or "",
        tags=flat.get("tags") or [],
        company=flat.get("company") or "",
    )


def build_preview(cfg: cfg_mod.Config) -> dict:
    """Classify Jens's notes against the new scopes, return the preview structure."""
    scopes = scopes_loader.load_all(cfg.scopes_dir)
    classifier = Classifier(cfg.anthropic, scopes)
    client = _client(cfg)

    raws = fetch_jens_notes(client)
    notes = [to_classification_input(r) for r in raws]
    by_uuid = {pb_client.flatten_note(r)["pb_uuid"]: r for r in raws}

    log.info("classifying %d notes against the new split scopes…", len(notes))
    classifications = classifier.classify_with_escalation(notes)
    by_id = {c.note_id: c for c in classifications}

    decisions: dict[str, list] = {
        "to_seamus": [],                 # will be PATCHed on --apply
        "to_seamus_low_confidence": [],  # review by hand, NOT auto-applied
        "stays_with_jens": [],
        "suggested_other": [],           # classifier thinks a third PM — not touched
    }

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
        if c.pm_email == SEAMUS and c.confidence >= APPLY_MIN_CONFIDENCE:
            decisions["to_seamus"].append(row)
        elif c.pm_email == SEAMUS:
            decisions["to_seamus_low_confidence"].append(row)
        elif c.pm_email == JENS or c.pm_email is None:
            decisions["stays_with_jens"].append(row)
        else:
            decisions["suggested_other"].append(row)

    summary = {
        "total_jens_notes": len(notes),
        "to_seamus": len(decisions["to_seamus"]),
        "to_seamus_low_confidence": len(decisions["to_seamus_low_confidence"]),
        "stays_with_jens": len(decisions["stays_with_jens"]),
        "suggested_other": len(decisions["suggested_other"]),
        "apply_min_confidence": APPLY_MIN_CONFIDENCE,
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
    log.info("review it, then run:  python -m scripts.migrate_jens_to_seamus --apply")


def apply_preview(cfg: cfg_mod.Config) -> None:
    """PATCH every note in preview.decisions.to_seamus to Séamus + swap team tag.

    Reads the preview file rather than re-classifying — so you patch exactly
    what you reviewed. Idempotent: re-running is safe.
    """
    if not PREVIEW_PATH.exists():
        sys.exit(f"no preview file at {PREVIEW_PATH}; run --preview first")

    preview = json.loads(PREVIEW_PATH.read_text(encoding="utf-8"))
    targets = preview.get("decisions", {}).get("to_seamus", [])
    if not targets:
        log.info("nothing to patch — to_seamus list is empty")
        return

    client = _client(cfg)

    log.info("PATCHing %d notes Jens → Séamus…", len(targets))
    ok = 0
    errors: list[dict] = []
    for row in targets:
        uuid = row["pb_uuid"]
        try:
            status = client.assign(uuid, SEAMUS)
            if 200 <= status < 300:
                ok += 1
                log.info("  %s ✓ (%d) — %s", uuid, status, row["title"][:60])
                # Swap team tags — best-effort, never blocks the migration.
                try:
                    client.add_tags(uuid, [NEW_TAG])
                    client.remove_tags(uuid, [OLD_TAG])
                except Exception as e:  # noqa: BLE001
                    log.warning("  %s tag swap failed (%s) — owner change OK", uuid, e)
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
    g.add_argument("--preview", action="store_true",
                   help="classify Jens's notes, write preview JSON, no PATCH")
    g.add_argument("--apply", action="store_true",
                   help="PATCH the notes flagged for Séamus in the preview file")
    args = parser.parse_args()

    cfg = cfg_mod.load_config()

    if args.preview:
        write_preview(build_preview(cfg))
    else:
        apply_preview(cfg)


if __name__ == "__main__":
    main()
