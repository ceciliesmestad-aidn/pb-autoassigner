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
    currently_unassigned: set[str] = set()
    with db.transaction(conn):
        for raw in raw_notes:
            flat = pb_client.flatten_note(raw)
            if not flat["pb_uuid"]:
                continue
            currently_unassigned.add(flat["pb_uuid"])
            total_seen += 1
            _, was_inserted = db.upsert_note(conn, flat)
            if was_inserted:
                inserted += 1
            else:
                updated += 1

        # Reconcile: any note we still think is pending-review but that PB no
        # longer lists as unassigned has been handled externally (manual assign
        # in PB, deleted, etc.). Flip to 'assigned' so the reviewer queue stays
        # in sync with PB's live state.
        reconciled = 0
        stale_rows = conn.execute(
            "SELECT id, pb_uuid FROM notes WHERE state IN ('new', 'suggested')"
        ).fetchall()
        for row in stale_rows:
            if row["pb_uuid"] not in currently_unassigned:
                db.set_note_state(conn, row["id"], "assigned")
                reconciled += 1
        if reconciled:
            log.info("ingest: reconciled %d externally-handled note(s)", reconciled)

    stats = {
        "inserted": inserted,
        "updated_existing": updated,
        "total_seen": total_seen,
        "reconciled": reconciled,
    }
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


def auto_assign_high_confidence(
    conn: sqlite3.Connection,
    client: pb_client.PBClient,
    cfg: Config,
    *,
    dry_run: bool = False,
) -> dict:
    """Auto-PATCH every fresh suggestion at-or-above the autopilot threshold.

    Runs after classify_pending. Each candidate must satisfy ALL of:
      - state == 'suggested'
      - latest suggestion has pm_email set (i.e. not a 'leave open')
      - confidence >= cfg.classifier.autopilot_min_confidence

    Two circuit breakers guard against scope-doc decay or runaway
    misclassification:

      per_pm_cap   — if a single run would auto-assign more than N notes to
                     ONE PM, the first N go through and the rest stay queued
                     for human review. Catches "scope decay routes everything
                     to one person".

      total_cap    — if a single run would auto-assign more than M notes
                     overall, the WHOLE batch is held back and queued for
                     review with a warning log entry. This is a 'something is
                     very wrong' tripwire, not a daily ceiling.

    `dry_run=True` runs the same selection + capping logic but does NOT call
    PB. The decisions are written to the audit table with pb_status=None and
    a `[DRY-RUN]` marker in pb_error so the Recent Autopilot tab still shows
    what *would* have happened. Use during the manual→autopilot ramp.

    Returns a stats dict with per-PM counts, capped notes, and skipped notes.
    """
    threshold = cfg.classifier.autopilot_min_confidence
    per_pm_cap = cfg.classifier.autopilot_per_pm_cap
    total_cap = cfg.classifier.autopilot_total_cap

    candidates = db.list_suggestions_with_notes(
        conn,
        states=("suggested",),
        min_confidence=threshold,
        limit=10_000,
    )
    # Drop "leave open" suggestions — pm_email is None when Claude bailed.
    candidates = [c for c in candidates if c.get("suggested_pm")]

    log.info(
        "autopilot: %d candidate(s) at confidence >= %.2f%s",
        len(candidates), threshold, " [DRY-RUN]" if dry_run else "",
    )

    stats: dict = {
        "candidates": len(candidates),
        "assigned": 0,
        "queued_overflow_per_pm": 0,
        "queued_total_cap_exceeded": 0,
        "errors": 0,
        "per_pm": {},
        "dry_run": dry_run,
        "threshold": threshold,
    }

    if not candidates:
        return stats

    # Total-cap tripwire — if the whole batch is too large, we trust nothing
    # in this run. Queue everything for human review and bail out.
    if len(candidates) > total_cap:
        log.warning(
            "autopilot: total cap exceeded (%d > %d) — queueing entire batch "
            "for review. Investigate before flipping autopilot back on.",
            len(candidates), total_cap,
        )
        stats["queued_total_cap_exceeded"] = len(candidates)
        return stats

    # Per-PM cap — assign first N per PM, queue the overflow.
    pm_counts: dict[str, int] = {}
    for c in candidates:
        pm = c["suggested_pm"]
        seen = pm_counts.get(pm, 0)
        if seen >= per_pm_cap:
            stats["queued_overflow_per_pm"] += 1
            log.info(
                "autopilot: per-PM cap reached for %s (>%d) — queueing note %s",
                pm, per_pm_cap, c["note_id"],
            )
            continue
        pm_counts[pm] = seen + 1

        try:
            if dry_run:
                # Record a no-op audit row so Recent Autopilot can show it.
                with db.transaction(conn):
                    db.record_assignment(
                        conn,
                        note_id=c["note_id"],
                        pm_email=pm,
                        suggested_pm=pm,
                        confidence=c["confidence"],
                        assigned_by="autopilot",
                        pb_status=None,
                        pb_error="[DRY-RUN] would PATCH PB",
                    )
            else:
                assign_note(
                    conn, client, c["note_id"], pm,
                    assigned_by="autopilot",
                )
            stats["assigned"] += 1
            stats["per_pm"][pm] = stats["per_pm"].get(pm, 0) + 1
        except Exception as e:
            stats["errors"] += 1
            log.exception("autopilot: failed to assign note %s → %s: %s",
                          c["note_id"], pm, e)

    log.info("autopilot: done — %s", {k: v for k, v in stats.items() if k != "per_pm"})
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
