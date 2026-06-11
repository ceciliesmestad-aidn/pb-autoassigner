"""Insights mode — per-PM content analysis.

Given a PM + time window, fetches the PM's PB notes and asks Claude to:
  - categorise each note (tender | feedback | bug | feature_request | question | other)
  - write a Norwegian summary of the *feedback* content (excluding tender notes)

Return payload is consumed by the Insights tab.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import anthropic

from . import classify as classify_mod
from . import pb_client as pb_mod
from .config import AnthropicConfig

log = logging.getLogger(__name__)


MAX_NOTES = 60           # ~6–8k input tokens; keeps single call well under timeout
MAX_BODY_CHARS = 500     # snippet per note
INSIGHTS_TIMEOUT = 300.0 # seconds — this call legitimately takes 1–2 min

VALID_CATEGORIES = {"tender", "feedback", "bug", "feature_request", "question", "other"}


INSIGHTS_PROMPT_EN = """You analyse Productboard feedback notes owned by one product manager at Aidn (a Norwegian healthcare software company).

The notes are in Norwegian (may contain English technical terms). Do NOT translate.

For EACH note, assign one category:
  - "tender"           — anbud, tilbudssvar, anskaffelse, kravspec-dokumenter (procurement material, not actual end-user feedback)
  - "feedback"         — brukertilbakemeldinger, observasjoner, ønsker uten konkret kravformulering
  - "bug"              — rapportert feil, noe som ikke fungerer som forventet
  - "feature_request"  — konkret ønske om ny funksjonalitet
  - "question"         — spørsmål eller avklaringsbehov
  - "other"            — alt annet (interne notater, test, automatisering)

Then write a concise Norwegian summary (max ~400 words) of the non-tender content. The summary should:
  - surface the main themes / pain points / requests across the feedback
  - be useful to a PM skimming for product signals
  - group similar items; do not list every note individually
  - quote specific municipality names when it illustrates a pattern
  - skip tender notes entirely

Format the summary as flowing plain-text paragraphs. Do NOT use Markdown — no ###, no **, no bullet dashes. Use short paragraph breaks to separate topics.

If there are no non-tender notes, return an empty summary string.
"""


INSIGHTS_TOOL = {
    "name": "return_insights",
    "description": "Categorise each note and summarise the non-tender feedback.",
    "input_schema": {
        "type": "object",
        "properties": {
            "categories": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "note_id": {"type": "string"},
                        "category": {
                            "type": "string",
                            "enum": sorted(VALID_CATEGORIES),
                        },
                    },
                    "required": ["note_id", "category"],
                },
            },
            "summary_no": {
                "type": "string",
                "description": "Norwegian markdown summary of feedback (non-tender). May be empty.",
            },
        },
        "required": ["categories", "summary_no"],
    },
}


@dataclass
class InsightsResult:
    pm_email: str
    window_days: int
    total_notes: int
    notes_by_category: dict[str, int]
    notes_by_municipality: list[dict]          # [{name, n}] sorted desc
    frequency: list[dict]                      # [{bucket: "2026-W15", n}]
    summary_no: str
    model: str


def compute_insights(
    pm_email: str,
    window_days: int,
    pb,
    cfg_anthropic: AnthropicConfig,
    client: anthropic.Anthropic | None = None,
) -> InsightsResult:
    """Fetch notes for the PM, categorise them, and summarise feedback."""
    client = client or classify_mod.build_anthropic_client(
        cfg_anthropic, timeout_seconds=INSIGHTS_TIMEOUT
    )
    since_dt = datetime.now(timezone.utc) - timedelta(days=window_days)
    since_iso = since_dt.isoformat(timespec="seconds")

    # ── 1. Fetch PB notes ────────────────────────────────────────────────────
    notes: list[dict] = []
    try:
        company_names = pb.company_names()  # v2: id→name map; v1: {}
        for raw in pb.list_notes(owner_email=pm_email):
            created = raw.get("createdAt") or raw.get("created_at") or ""
            if created and created < since_iso:
                continue
            notes.append(pb_mod.flatten_note(raw, company_names))
    except Exception as e:
        log.warning("insights: PB fetch for %s failed: %s", pm_email, e)
        raise

    log.info("insights: %s — %d notes in last %d days", pm_email, len(notes), window_days)

    total_notes = len(notes)
    if total_notes == 0:
        return InsightsResult(
            pm_email=pm_email,
            window_days=window_days,
            total_notes=0,
            notes_by_category={},
            notes_by_municipality=[],
            frequency=[],
            summary_no="",
            model=cfg_anthropic.model_escalate,
        )

    # Sort newest-first, cap before sending to Claude
    notes.sort(key=lambda n: n.get("pb_created_at") or "", reverse=True)
    sample = notes[:MAX_NOTES]

    # ── 2. Ask Claude to categorise + summarise ──────────────────────────────
    payload = []
    for i, n in enumerate(sample):
        body = (n.get("content") or "")[:MAX_BODY_CHARS]
        payload.append({
            "note_id": str(i),   # stable index into `sample`
            "title": n.get("title") or "",
            "body": body,
            "tags": n.get("tags") or [],
            "company": n.get("company") or "",
        })

    log.info("insights: calling %s on %d notes", cfg_anthropic.model_escalate, len(sample))
    response = client.messages.create(
        model=cfg_anthropic.model_escalate,
        max_tokens=4096,
        system=INSIGHTS_PROMPT_EN,
        tools=[INSIGHTS_TOOL],
        tool_choice={"type": "tool", "name": "return_insights"},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"PM: {pm_email}\n"
                            f"Window: last {window_days} days\n"
                            f"Notes ({len(payload)} of {total_notes} total, newest first):\n"
                            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
                        ),
                    }
                ],
            }
        ],
    )
    classify_mod.log_cache_usage(response, cfg_anthropic.model_escalate)

    tool_block = next(
        (b for b in response.content if getattr(b, "type", None) == "tool_use"), None,
    )
    data = (tool_block.input if tool_block else None) or {}
    raw_cats = data.get("categories") or []
    summary_no = str(data.get("summary_no") or "").strip()

    # Map note_id (string index) -> category
    cat_by_id: dict[str, str] = {}
    for c in raw_cats:
        nid = str(c.get("note_id") or "")
        cat = str(c.get("category") or "other").lower()
        if cat not in VALID_CATEGORIES:
            cat = "other"
        cat_by_id[nid] = cat

    # ── 3. Aggregate ─────────────────────────────────────────────────────────
    cat_counts: Counter[str] = Counter()
    muni_counts: Counter[str] = Counter()
    for i, n in enumerate(sample):
        cat = cat_by_id.get(str(i), "other")
        cat_counts[cat] += 1
        muni = (n.get("company") or "").strip()
        if muni:
            muni_counts[muni] += 1

    # For notes beyond MAX_NOTES (if any) — count them as "other" so total matches,
    # and still include their municipality.
    for n in notes[MAX_NOTES:]:
        cat_counts["other"] += 1
        muni = (n.get("company") or "").strip()
        if muni:
            muni_counts[muni] += 1

    # ── 4. Frequency buckets (tender excluded) ───────────────────────────────
    # Build set of sample-indices that are tender so they're excluded from the chart.
    tender_sample_indices = {
        int(nid) for nid, cat in cat_by_id.items() if cat == "tender"
    }
    non_tender_notes = [
        n for i, n in enumerate(notes)
        if i not in tender_sample_indices
    ]
    log.info(
        "insights: %d total notes, %d tender filtered, %d non-tender for frequency",
        len(notes), len(tender_sample_indices), len(non_tender_notes),
    )
    frequency = _bucket_frequency(non_tender_notes, window_days)

    return InsightsResult(
        pm_email=pm_email,
        window_days=window_days,
        total_notes=total_notes,
        notes_by_category=dict(cat_counts),
        notes_by_municipality=[],   # removed from UI
        frequency=frequency,
        summary_no=summary_no,
        model=cfg_anthropic.model_escalate,
    )


def _parse_pb_date(ts: str) -> "datetime | None":
    """Parse a Productboard ISO-8601 timestamp robustly.

    PB returns dates in several formats depending on the API version:
      "2026-04-15T10:30:00.000Z"       (ms, Z suffix)
      "2026-04-15T10:30:00Z"           (no ms, Z suffix)
      "2026-04-15T10:30:00+00:00"      (offset, no ms)
      "2026-04-15T10:30:00.000+00:00"  (offset + ms)
      "2026-04-15T10:30:00"            (naive, no suffix)
    """
    if not ts:
        return None
    try:
        normalized = ts.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception as exc:
        log.debug("insights: could not parse date %r: %s", ts, exc)
        return None


def _bucket_frequency(notes: list[dict], window_days: int) -> list[dict]:
    """Bucket notes by day (≤ 14d window), week (≤ 90d), or month (> 90d).

    Returns buckets in chronological order, filling in zero-count gaps so the
    frontend bar chart shows a proper timeline.
    """
    if window_days <= 14:
        granularity = "day"
    elif window_days <= 90:
        granularity = "week"
    else:
        granularity = "month"

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=window_days)

    counts: Counter[str] = Counter()
    parsed = 0
    skipped = 0
    for n in notes:
        ts = n.get("pb_created_at") or ""
        dt = _parse_pb_date(ts)
        if dt is None:
            skipped += 1
            continue
        if dt < start:
            skipped += 1
            continue
        counts[_bucket_key(dt, granularity)] += 1
        parsed += 1

    log.info(
        "insights: frequency — %d/%d notes counted, granularity=%s, buckets=%d, "
        "first_ts=%r",
        parsed,
        len(notes),
        granularity,
        len(counts),
        (notes[0].get("pb_created_at") if notes else ""),
    )

    # Build the full timeline so empty buckets still appear
    timeline: list[str] = []
    seen: set[str] = set()
    cursor = start
    while cursor <= now:
        key = _bucket_key(cursor, granularity)
        if key not in seen:
            timeline.append(key)
            seen.add(key)
        cursor += timedelta(days=1)

    return [{"bucket": key, "n": counts.get(key, 0)} for key in timeline]


def _bucket_key(dt: "datetime", granularity: str) -> str:
    if granularity == "day":
        return dt.strftime("%Y-%m-%d")
    if granularity == "week":
        iso = dt.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return dt.strftime("%Y-%m")
