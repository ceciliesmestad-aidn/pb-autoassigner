"""Training mode — scope-doc rewriting.

For each PM with ≥ min_notes_per_pm recently-assigned notes, ask Claude to
propose an updated scope YAML that better reflects the actual routing pattern.
The UI shows the diff; user approves; we write the new YAML + a scope_versions
audit row.

No model weights. No fine-tuning. "Memory" of past classifications lives in
the scope YAMLs themselves — keeping routing logic human-readable.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import anthropic

from . import classify as classify_mod
from . import db, owners, scopes_loader
from .config import AnthropicConfig, TrainingConfig

log = logging.getLogger(__name__)


TRAINING_PROMPT_EN = """You are refining a product-routing scope document.

You are given:
  1. The current scope YAML for one product manager at Aidn (in Norwegian).
  2. Recent Productboard notes that were actually assigned to this PM (ground truth).
  3. Metadata for each: whether the assignment was an override of a different suggestion.

Your task: propose an **updated** version of the scope YAML that more accurately captures this PM's actual scope, based on the recent assignments.

Guidelines:
  - Keep the same YAML structure (pm_email, pm_name, team, description_no, includes, excludes, tag_routes, keywords_strong, disambiguations, hard_negatives).
  - Additions should be grounded in the assigned notes. Don't invent terminology the notes don't use.
  - Pay extra attention to override assignments — those tell you the classifier was wrong and the PM corrected it. Those are the most valuable training signal.
  - Prefer minimal, surgical edits. Don't rewrite the whole document if one added keyword or a clarifying `excludes` entry is enough.
  - The description_no should remain stable; only edit if the PM's scope has genuinely shifted.
  - Keep the text Norwegian.

Return BOTH:
  - The complete updated YAML as a single string.
  - A short Norwegian rationale (max 400 chars) explaining what you changed and why.

Do not apply changes that aren't supported by the evidence in the provided notes.
"""


PROPOSE_TOOL = {
    "name": "propose_scope_update",
    "description": "Propose a revised scope YAML for a PM based on recently-assigned notes.",
    "input_schema": {
        "type": "object",
        "properties": {
            "updated_yaml": {
                "type": "string",
                "description": "The full new YAML contents. Must be a complete, valid YAML document.",
            },
            "rationale_no": {
                "type": "string",
                "description": "Short Norwegian rationale (max ~400 chars) summarising the changes.",
                "maxLength": 600,
            },
            "changed": {
                "type": "boolean",
                "description": "True if meaningful changes were proposed; false if current YAML already captures the pattern.",
            },
        },
        "required": ["updated_yaml", "rationale_no", "changed"],
    },
}


@dataclass
class ProposedUpdate:
    pm_email: str
    current_yaml: str
    proposed_yaml: str
    rationale_no: str
    changed: bool
    sample_size: int
    model: str


MAX_NOTES_PER_PM = 80   # ~8–10k input tokens; stays under the 30k/min org limit

def propose_scope_updates(
    conn,
    cfg_anthropic: AnthropicConfig,
    cfg_training: TrainingConfig,
    scopes_dir,
    pb=None,           # pb_client.PBClient — optional; if None, falls back to DB
    client: anthropic.Anthropic | None = None,
    pm_emails: list[str] | None = None,   # None = all eligible PMs
    window_days: int | None = None,       # override cfg_training.window_days
) -> list[ProposedUpdate]:
    """Iterate over PMs; propose an update per eligible PM.

    Primary source: notes currently owned by each PM in Productboard (fetched
    live via pb_client.list_notes(owner_email=…)). This means training works on
    day one, before any notes have been assigned through the app itself.

    Secondary signal: any override assignments recorded in the local DB are
    flagged in the prompt — overrides tell the model "the classifier was wrong
    here", which is the strongest training signal.

    Falls back to DB-only assignments when pb=None (used in unit tests).
    """
    client = client or classify_mod.build_anthropic_client(cfg_anthropic)

    effective_window = window_days if window_days is not None else cfg_training.window_days
    since = (
        datetime.now(timezone.utc) - timedelta(days=effective_window)
    ).isoformat(timespec="seconds")

    # Filter to requested PM(s) if specified.
    pm_filter = set(e.lower() for e in pm_emails) if pm_emails else None

    proposals: list[ProposedUpdate] = []
    eligible_pms = [
        pm for pm in owners.get_all()
        if pm_filter is None or pm.email.lower() in pm_filter
    ]
    for i, pm in enumerate(eligible_pms):
        # ── 1. fetch notes for this PM ────────────────────────────────────────
        if pb is not None:
            notes = _fetch_pb_notes_for_pm(pb, pm.email, since)
        else:
            # test / fallback path: use DB assignments table
            notes = db.assignments_for_pm(conn, pm.email, since)

        if len(notes) < cfg_training.min_notes_per_pm:
            log.info(
                "training: skipping %s — only %d notes in last %d days (need %d)",
                pm.email, len(notes), effective_window,
                cfg_training.min_notes_per_pm,
            )
            continue

        log.info(
            "training: proposing for %s — %d notes in window (capped at %d)",
            pm.email, len(notes), MAX_NOTES_PER_PM,
        )

        current = scopes_loader.read_scope(scopes_dir, pm.email) or ""
        if not current:
            log.warning("training: no scope file for %s", pm.email)
            continue

        # ── 2. enrich with override flags from DB where available ─────────────
        override_map = _override_map(conn, pm.email, since)
        # Sort newest-first so the cap keeps the most recent signal.
        notes_sorted = sorted(
            notes,
            key=lambda r: r.get("pb_created_at") or r.get("created_at") or "",
            reverse=True,
        )
        sample = _serialise_notes_for_training(notes_sorted[:MAX_NOTES_PER_PM], override_map)

        # ── 3. rate-limit guard: sleep between PMs when processing >1 ─────────
        if i > 0:
            time.sleep(5)   # 30k tokens/min org limit; each call ~8–10k tokens

        # ── 4. ask Claude to propose a scope update ───────────────────────────
        response = client.messages.create(
            model=cfg_anthropic.model_escalate,
            max_tokens=4096,
            system=TRAINING_PROMPT_EN,
            tools=[PROPOSE_TOOL],
            tool_choice={"type": "tool", "name": "propose_scope_update"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"PM: {pm.name} ({pm.email}) — {pm.team}\n\n"
                                f"=== CURRENT SCOPE YAML ===\n{current}\n\n"
                                f"=== NOTES OWNED BY THIS PM IN PB ({len(sample)} sampled "
                                f"from {len(notes)} total, last {effective_window} days) ===\n"
                                f"{json.dumps(sample, ensure_ascii=False, indent=2)}"
                            ),
                        }
                    ],
                }
            ],
        )

        tool_block = next(
            (b for b in response.content if getattr(b, "type", None) == "tool_use"), None
        )
        if tool_block is None:
            log.warning("training: no tool_use returned for %s", pm.email)
            continue

        data = tool_block.input or {}
        proposals.append(
            ProposedUpdate(
                pm_email=pm.email,
                current_yaml=current,
                proposed_yaml=str(data.get("updated_yaml") or current),
                rationale_no=str(data.get("rationale_no") or ""),
                changed=bool(data.get("changed", False)),
                sample_size=len(notes),
                model=cfg_anthropic.model_escalate,
            )
        )

    return proposals


def apply_update(conn, scopes_dir, update: ProposedUpdate) -> None:
    """Write the proposed YAML to disk and log a scope_versions row."""
    scopes_loader.write_scope(scopes_dir, update.pm_email, update.proposed_yaml)
    content_hash = hashlib.sha256(update.proposed_yaml.encode("utf-8")).hexdigest()[:16]
    db.record_scope_version(
        conn,
        pm_email=update.pm_email,
        yaml_content=update.proposed_yaml,
        content_hash=content_hash,
        source="training",
        notes=f"[sample_size={update.sample_size}] {update.rationale_no}",
    )


def _fetch_pb_notes_for_pm(pb, pm_email: str, since_iso: str) -> list[dict]:
    """Fetch notes currently owned by a PM from PB, filtered by created_at."""
    from . import pb_client as pb_mod
    try:
        notes = []
        for raw in pb.list_notes(owner_email=pm_email):
            # PB API v1 uses camelCase; accept both spellings.
            created = raw.get("createdAt") or raw.get("created_at") or ""
            if created and created < since_iso:
                continue  # PB returns newest-first; once we're past the window, skip
            flat = pb_mod.flatten_note(raw)
            notes.append(flat)
        return notes
    except Exception as e:
        log.warning("training: PB fetch for %s failed (%s) — skipping", pm_email, e)
        return []


def _override_map(conn, pm_email: str, since_iso: str) -> dict[str, dict]:
    """Return {pb_uuid: {was_override, suggested_pm}} for app-recorded assignments."""
    rows = db.assignments_for_pm(conn, pm_email, since_iso)
    return {
        r["pb_uuid"]: {
            "was_override": bool(r.get("was_override")),
            "suggested_pm": r.get("suggested_pm"),
        }
        for r in rows
    }


def _serialise_notes_for_training(
    notes: list[dict], override_map: dict[str, dict] | None = None
) -> list[dict]:
    """Shrink note content to keep the training prompt bounded.

    Notes can come from either PB (flattened) or the DB assignments join — both
    share the same field names after flatten_note(). Override metadata is grafted
    in from the override_map when available; it's the strongest training signal
    (tells the model the classifier was wrong and the PM manually corrected it).
    """
    out = []
    override_map = override_map or {}
    for r in notes:
        pb_uuid = r.get("pb_uuid") or ""
        body = (r.get("content") or "")[:800]
        tags_raw = r.get("tags") or r.get("tags_json") or []
        if isinstance(tags_raw, str):
            try:
                tags_raw = json.loads(tags_raw)
            except Exception:
                tags_raw = []
        override_info = override_map.get(pb_uuid, {})
        entry: dict = {
            "pb_uuid": pb_uuid,
            "title": r.get("title"),
            "body": body,
            "tags": tags_raw,
            "company": r.get("company"),
            "created_at": r.get("pb_created_at") or r.get("created_at") or "",
        }
        if override_info.get("was_override"):
            entry["was_override"] = True
            entry["suggested_pm_when_overridden"] = override_info.get("suggested_pm")
        out.append(entry)
    return out
