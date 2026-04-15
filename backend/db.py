"""SQLite persistence layer.

Single-file DB with four tables:
  notes             — ingested PB notes, deduped by content hash
  suggestions       — classifier output per note, one row per classify run
  assignments       — audit log: who was assigned, by whom, when, override?
  scope_versions    — historical per-PM scope YAML for diffing/rollback

Design notes:
- Every row keeps `created_at` as ISO8601 UTC.
- `notes.state` is the lifecycle: new → suggested → assigned | skipped.
- `suggestions.run_id` groups rows from a single classify run.
- WAL mode for concurrent readers (UI) + single writer (backend job).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pb_uuid         TEXT    NOT NULL UNIQUE,
    content_hash    TEXT    NOT NULL,
    title           TEXT    NOT NULL DEFAULT '',
    content         TEXT    NOT NULL DEFAULT '',
    tags_json       TEXT    NOT NULL DEFAULT '[]',
    company         TEXT    NOT NULL DEFAULT '',
    source          TEXT    NOT NULL DEFAULT '',
    display_url     TEXT    NOT NULL DEFAULT '',
    pb_created_at   TEXT    NOT NULL DEFAULT '',
    state           TEXT    NOT NULL DEFAULT 'new'
                           CHECK (state IN ('new', 'suggested', 'assigned', 'skipped')),
    ingested_at     TEXT    NOT NULL,
    raw_json        TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_notes_state         ON notes(state);
CREATE INDEX IF NOT EXISTS idx_notes_content_hash  ON notes(content_hash);
CREATE INDEX IF NOT EXISTS idx_notes_pb_created_at ON notes(pb_created_at);

CREATE TABLE IF NOT EXISTS suggestions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id      INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    run_id       TEXT    NOT NULL,
    pm_email     TEXT,                      -- NULL means "leave open"
    confidence   REAL    NOT NULL,
    reasoning    TEXT    NOT NULL DEFAULT '',
    model        TEXT    NOT NULL,
    escalated    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_suggestions_note_id ON suggestions(note_id);
CREATE INDEX IF NOT EXISTS idx_suggestions_run_id  ON suggestions(run_id);

CREATE TABLE IF NOT EXISTS assignments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id       INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    pm_email      TEXT    NOT NULL,
    suggested_pm  TEXT,                      -- pm_email of the suggestion at the time
    was_override  INTEGER NOT NULL DEFAULT 0,
    confidence    REAL,                      -- confidence of the suggestion shown to user
    assigned_by   TEXT    NOT NULL DEFAULT 'user',
    assigned_at   TEXT    NOT NULL,
    pb_status     INTEGER,                   -- HTTP status from PATCH (201 = success)
    pb_error      TEXT                       -- error message if PATCH failed
);
CREATE INDEX IF NOT EXISTS idx_assignments_note_id    ON assignments(note_id);
CREATE INDEX IF NOT EXISTS idx_assignments_pm_email   ON assignments(pm_email);
CREATE INDEX IF NOT EXISTS idx_assignments_assigned_at ON assignments(assigned_at);

CREATE TABLE IF NOT EXISTS scope_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    pm_email     TEXT    NOT NULL,
    yaml_content TEXT    NOT NULL,
    content_hash TEXT    NOT NULL,
    source       TEXT    NOT NULL DEFAULT 'manual'  -- 'manual' | 'training'
                CHECK (source IN ('manual', 'training')),
    notes        TEXT    NOT NULL DEFAULT '',       -- rationale / diff notes
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scope_versions_pm_email ON scope_versions(pm_email);

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,    -- 'ingest' | 'classify' | 'train'
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    stats_json   TEXT NOT NULL DEFAULT '{}'
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)  # autocommit; use explicit transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(db_path: Path) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ─── notes ────────────────────────────────────────────────────────────────────

def upsert_note(conn: sqlite3.Connection, note: dict) -> tuple[int, bool]:
    """Insert or update a note by pb_uuid. Returns (note_id, inserted).

    `note` must contain at minimum: pb_uuid, content_hash, title, content.
    """
    existing = conn.execute(
        "SELECT id, content_hash FROM notes WHERE pb_uuid = ?", (note["pb_uuid"],)
    ).fetchone()

    now = utcnow_iso()
    if existing is None:
        cur = conn.execute(
            """
            INSERT INTO notes (pb_uuid, content_hash, title, content, tags_json,
                               company, source, display_url, pb_created_at,
                               state, ingested_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
            """,
            (
                note["pb_uuid"],
                note["content_hash"],
                note.get("title", ""),
                note.get("content", ""),
                json.dumps(note.get("tags", []), ensure_ascii=False),
                note.get("company", ""),
                note.get("source", ""),
                note.get("display_url", ""),
                note.get("pb_created_at", ""),
                now,
                json.dumps(note.get("raw", {}), ensure_ascii=False),
            ),
        )
        return cur.lastrowid, True

    # Note existed; update only if content_hash changed (title/content edits in PB).
    if existing["content_hash"] != note["content_hash"]:
        conn.execute(
            """
            UPDATE notes
               SET content_hash = ?, title = ?, content = ?, tags_json = ?,
                   company = ?, source = ?, display_url = ?, pb_created_at = ?,
                   raw_json = ?
             WHERE id = ?
            """,
            (
                note["content_hash"],
                note.get("title", ""),
                note.get("content", ""),
                json.dumps(note.get("tags", []), ensure_ascii=False),
                note.get("company", ""),
                note.get("source", ""),
                note.get("display_url", ""),
                note.get("pb_created_at", ""),
                json.dumps(note.get("raw", {}), ensure_ascii=False),
                existing["id"],
            ),
        )
    return existing["id"], False


def set_note_state(conn: sqlite3.Connection, note_id: int, state: str) -> None:
    conn.execute("UPDATE notes SET state = ? WHERE id = ?", (state, note_id))


def notes_needing_classification(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Notes in state 'new' plus notes in 'suggested' whose latest suggestion is stale.

    For now, just return 'new'. Re-classification of 'suggested' notes is a future
    enhancement (e.g. when scope YAMLs change).
    """
    return conn.execute(
        "SELECT * FROM notes WHERE state = 'new' ORDER BY pb_created_at"
    ).fetchall()


# ─── suggestions ──────────────────────────────────────────────────────────────

def insert_suggestion(
    conn: sqlite3.Connection,
    note_id: int,
    run_id: str,
    pm_email: str | None,
    confidence: float,
    reasoning: str,
    model: str,
    escalated: bool = False,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO suggestions (note_id, run_id, pm_email, confidence, reasoning,
                                 model, escalated, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (note_id, run_id, pm_email, confidence, reasoning, model,
         1 if escalated else 0, utcnow_iso()),
    )
    return cur.lastrowid


def latest_suggestion_for_note(conn: sqlite3.Connection, note_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM suggestions WHERE note_id = ? ORDER BY id DESC LIMIT 1",
        (note_id,),
    ).fetchone()


# ─── assignments ──────────────────────────────────────────────────────────────

def record_assignment(
    conn: sqlite3.Connection,
    note_id: int,
    pm_email: str,
    suggested_pm: str | None,
    confidence: float | None,
    assigned_by: str = "user",
    pb_status: int | None = None,
    pb_error: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO assignments (note_id, pm_email, suggested_pm, was_override,
                                 confidence, assigned_by, assigned_at, pb_status, pb_error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            note_id,
            pm_email,
            suggested_pm,
            1 if (suggested_pm is not None and suggested_pm != pm_email) else 0,
            confidence,
            assigned_by,
            utcnow_iso(),
            pb_status,
            pb_error,
        ),
    )
    return cur.lastrowid


# ─── scope versions ───────────────────────────────────────────────────────────

def record_scope_version(
    conn: sqlite3.Connection,
    pm_email: str,
    yaml_content: str,
    content_hash: str,
    source: str = "manual",
    notes: str = "",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO scope_versions (pm_email, yaml_content, content_hash, source, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (pm_email, yaml_content, content_hash, source, notes, utcnow_iso()),
    )
    return cur.lastrowid


# ─── runs ─────────────────────────────────────────────────────────────────────

def start_run(conn: sqlite3.Connection, kind: str) -> str:
    run_id = new_run_id()
    conn.execute(
        "INSERT INTO runs (run_id, kind, started_at) VALUES (?, ?, ?)",
        (run_id, kind, utcnow_iso()),
    )
    return run_id


def finish_run(conn: sqlite3.Connection, run_id: str, stats: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, stats_json = ? WHERE run_id = ?",
        (utcnow_iso(), json.dumps(stats, ensure_ascii=False), run_id),
    )


# ─── read helpers used by the API layer ───────────────────────────────────────

def list_suggestions_with_notes(
    conn: sqlite3.Connection,
    *,
    states: tuple[str, ...] = ("suggested",),
    pm_email: str | None = None,
    min_confidence: float | None = None,
    max_confidence: float | None = None,
    limit: int = 500,
) -> list[dict]:
    """Join notes with their latest suggestion. Used by the reviewer UI."""
    sql = """
    WITH latest AS (
        SELECT s.*
          FROM suggestions s
          JOIN (
              SELECT note_id, MAX(id) AS max_id
                FROM suggestions
               GROUP BY note_id
          ) m ON m.max_id = s.id
    )
    SELECT n.id           AS note_id,
           n.pb_uuid,
           n.title,
           n.content,
           n.tags_json,
           n.company,
           n.source,
           n.display_url,
           n.pb_created_at,
           n.state,
           l.pm_email     AS suggested_pm,
           l.confidence,
           l.reasoning,
           l.model,
           l.escalated,
           l.created_at   AS suggested_at
      FROM notes n
      LEFT JOIN latest l ON l.note_id = n.id
     WHERE n.state IN ({placeholders})
    """.format(placeholders=",".join("?" * len(states)))

    params: list[Any] = list(states)
    if pm_email is not None:
        sql += " AND l.pm_email = ?"
        params.append(pm_email)
    if min_confidence is not None:
        sql += " AND l.confidence >= ?"
        params.append(min_confidence)
    if max_confidence is not None:
        sql += " AND l.confidence <= ?"
        params.append(max_confidence)
    sql += " ORDER BY l.confidence DESC, n.pb_created_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def note_by_id(conn: sqlite3.Connection, note_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()


def note_by_pb_uuid(conn: sqlite3.Connection, pb_uuid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM notes WHERE pb_uuid = ?", (pb_uuid,)).fetchone()


def dashboard_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Aggregate counts for the Dashboard page."""
    by_state = {
        row["state"]: row["n"]
        for row in conn.execute("SELECT state, COUNT(*) AS n FROM notes GROUP BY state")
    }

    # Assignments last 7 / 30 days per PM.
    per_pm_7 = _per_pm_counts(conn, days=7)
    per_pm_30 = _per_pm_counts(conn, days=30)

    # Confidence distribution of current suggestions (state=suggested).
    conf_bins = {"0.0-0.2": 0, "0.2-0.4": 0, "0.4-0.6": 0, "0.6-0.8": 0, "0.8-1.0": 0}
    for row in conn.execute("""
        SELECT l.confidence AS c
          FROM notes n
          JOIN (
              SELECT s.* FROM suggestions s
                JOIN (SELECT note_id, MAX(id) AS max_id FROM suggestions GROUP BY note_id) m
                  ON m.max_id = s.id
          ) l ON l.note_id = n.id
         WHERE n.state = 'suggested'
    """):
        c = row["c"] or 0
        if c < 0.2:   conf_bins["0.0-0.2"] += 1
        elif c < 0.4: conf_bins["0.2-0.4"] += 1
        elif c < 0.6: conf_bins["0.4-0.6"] += 1
        elif c < 0.8: conf_bins["0.6-0.8"] += 1
        else:         conf_bins["0.8-1.0"] += 1

    # Weekly volume (assignments per ISO week, last 12 weeks).
    weekly = conn.execute("""
        SELECT strftime('%Y-W%W', assigned_at) AS week, COUNT(*) AS n
          FROM assignments
         WHERE assigned_at >= datetime('now', '-84 days')
         GROUP BY week
         ORDER BY week
    """).fetchall()

    return {
        "notes_by_state": by_state,
        "assignments_7d": per_pm_7,
        "assignments_30d": per_pm_30,
        "confidence_distribution": conf_bins,
        "weekly_volume": [dict(r) for r in weekly],
    }


def _per_pm_counts(conn: sqlite3.Connection, days: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT pm_email, COUNT(*) AS n
          FROM assignments
         WHERE assigned_at >= datetime('now', ?)
         GROUP BY pm_email
         ORDER BY n DESC
        """,
        (f"-{days} days",),
    ).fetchall()
    return [dict(r) for r in rows]


def assignments_for_pm(
    conn: sqlite3.Connection, pm_email: str, since_iso: str
) -> list[dict]:
    """Used by training to pull a PM's recently-assigned notes."""
    rows = conn.execute(
        """
        SELECT a.id          AS assignment_id,
               a.assigned_at,
               a.was_override,
               a.suggested_pm,
               a.confidence,
               n.id           AS note_id,
               n.pb_uuid,
               n.title,
               n.content,
               n.tags_json,
               n.company
          FROM assignments a
          JOIN notes n ON n.id = a.note_id
         WHERE a.pm_email = ? AND a.assigned_at >= ?
         ORDER BY a.assigned_at DESC
        """,
        (pm_email, since_iso),
    ).fetchall()
    return [dict(r) for r in rows]


def training_history(conn: sqlite3.Connection, pm_email: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, source, notes, content_hash, created_at
          FROM scope_versions
         WHERE pm_email = ?
         ORDER BY created_at DESC
        """,
        (pm_email,),
    ).fetchall()
    return [dict(r) for r in rows]
