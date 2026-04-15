"""Pipeline orchestration: ingest → classify, plus the assign flow.

Separated from CLI + API layers so both can call the same code paths.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Callable

from . import classify as classify_mod
from . import db, owners, pb_client, scopes_loader
from .config import Config

log = logging.getLogger(__name__)


def ingest(
    conn: sqlite3.Connection,
    client: pb_client.PBClient,
) -> dict:
    """Pull unassigned notes from PB, upsert into SQLite."""
    run_id = db.start_run(conn, "ingest")
    inserted = 0
    updated = 0
    total_seen = 0

    log.info("ingest: fetching unassigned notes from Productboard…")
    try:
        raw_notes = client.fetch_unassigned()
    except pb_client.PBError as e:
        log.error("ingest: PB fetch failed — HTTP %s: %s", e.status, e.body[:300])
        with db.transaction(conn):
            db.finish_run(conn, run_id, {"error": str(e), "stage": "fetch"})
        raise
    except Exception as e:
        log.exception("ingest: PB fetch FAILED (%s): %s", type(e).__name__, e)
        with db.transaction(conn):
            db.finish_run(conn, run_id, {"error": str(e), "stage": "fetch"})
        raise

    log.info("ingest: PB returned %d unassigned notes; upserting…", len(raw_notes))
    with db.transaction(conn):
        for raw in raw_notes:
            flat = pb_client.flatten_note(raw)
            if not flat["pb_uuid"]:
                continue
            total_seen += 1
            _, was_inserted = db.upsert_note(conn, flat)
            if was_inserted:
                inserted += 1
            else:
                updated += 1

    stats = {"inserted": inserted, "updated_existing": updated, "total_seen": total_seen}
    with db.transaction(conn):
        db.finish_run(conn, run_id, stats)
    log.info("ingest: done — %s", stats)
    return stats


def classify_pending(
    conn: sqlite3.Connection,
    cfg: Config,
    scopes: scopes_loader.LoadedScopes | None = None,
) -> dict:
    """Classify every note in state='new' and move it to state='suggested'."""
    scopes = scopes or scopes_loader.load_all(cfg.scopes_dir)
    classifier = classify_mod.Classifier(cfg.anthropic, scopes)

    pending = db.notes_needing_classification(conn)
    if not pending:
        log.info("classify: no pending notes — nothing to do")
        return {"classified": 0, "escalated": 0}

    log.info("classify: %d pending notes (scope hash=%s)", len(pending), scopes.combined_hash[:12])

    notes_for_model = [
        classify_mod.NoteForClassification(
            note_id=str(r["id"]),
            title=r["title"] or "",
            content=r["content"] or "",
            tags=_parse_tags(r["tags_json"]),
            company=r["company"] or "",
        )
        for r in pending
    ]

    run_id = db.start_run(conn, "classify")
    try:
        classifications = classifier.classify_with_escalation(notes_for_model)
    except Exception as e:
        log.exception("classify: FAILED (%s): %s", type(e).__name__, e)
        with db.transaction(conn):
            db.finish_run(conn, run_id, {"error": f"{type(e).__name__}: {e}"})
        raise

    by_note_id = {c.note_id: c for c in classifications}

    classified = 0
    escalated = 0
    with db.transaction(conn):
        for row in pending:
            nid = str(row["id"])
            c = by_note_id.get(nid)
            if c is None:
                log.warning("no classification returned for note id=%s", nid)
                continue
            db.insert_suggestion(
                conn,
                note_id=row["id"],
                run_id=run_id,
                pm_email=c.pm_email,
                confidence=c.confidence,
                reasoning=c.reasoning,
                model=c.model,
                escalated=c.escalated,
            )
            db.set_note_state(conn, row["id"], "suggested")
            classified += 1
            if c.escalated:
                escalated += 1

        stats = {
            "classified": classified,
            "escalated": escalated,
            "scopes_hash": scopes.combined_hash,
        }
        db.finish_run(conn, run_id, stats)

    log.info("classify: done — %s", stats)
    return stats


def assign_note(
    conn: sqlite3.Connection,
    client: pb_client.PBClient,
    note_id: int,
    pm_email: str,
    assigned_by: str = "user",
) -> dict:
    """Push an assignment to PB and log the outcome."""
    note = db.note_by_id(conn, note_id)
    if note is None:
        raise ValueError(f"unknown note id: {note_id}")

    # Resolve the suggestion that was visible to the user at decision time.
    latest = db.latest_suggestion_for_note(conn, note_id)
    suggested_pm = latest["pm_email"] if latest else None
    confidence = latest["confidence"] if latest else None

    pm = owners.get_by_email(pm_email)
    if pm is None:
        raise ValueError(f"unknown PM email: {pm_email}")

    pb_status = None
    pb_error = None
    try:
        pb_status = client.assign(note["pb_uuid"], pm_email)
    except pb_client.PBError as e:
        pb_error = f"HTTP {e.status}: {e.body[:500]}"
        log.error("PATCH failed: %s", pb_error)

    with db.transaction(conn):
        db.record_assignment(
            conn,
            note_id=note_id,
            pm_email=pm_email,
            suggested_pm=suggested_pm,
            confidence=confidence,
            assigned_by=assigned_by,
            pb_status=pb_status,
            pb_error=pb_error,
        )
        if pb_error is None and pb_status in (200, 201, 204):
            db.set_note_state(conn, note_id, "assigned")

    return {
        "note_id": note_id,
        "pm_email": pm_email,
        "pb_status": pb_status,
        "pb_error": pb_error,
        "was_override": suggested_pm is not None and suggested_pm != pm_email,
    }


def skip_note(conn: sqlite3.Connection, note_id: int, assigned_by: str = "user") -> None:
    """Mark a note as 'skipped' (leave-open decision, audit logged)."""
    latest = db.latest_suggestion_for_note(conn, note_id)
    with db.transaction(conn):
        db.record_assignment(
            conn,
            note_id=note_id,
            pm_email="__skipped__",   # sentinel so the audit row exists
            suggested_pm=latest["pm_email"] if latest else None,
            confidence=latest["confidence"] if latest else None,
            assigned_by=assigned_by,
            pb_status=None,
            pb_error=None,
        )
        db.set_note_state(conn, note_id, "skipped")


def _parse_tags(tags_json: str | None) -> list[str]:
    import json as _json
    if not tags_json:
        return []
    try:
        v = _json.loads(tags_json)
        return [t for t in v if isinstance(t, str)]
    except Exception:
        return []
