"""Claude-backed note classification.

Design:
- One large system prompt containing global routing + all per-PM scope YAMLs.
  Marked with cache_control so Anthropic caches it across calls — the system
  block is ~10–40 KB and stays stable between runs until Jens edits a scope.
- User message is a JSON array of notes. Model returns a JSON array of
  {note_id, pm_email, confidence, reasoning}. Strict response-shape enforcement
  via `tools` forcing — cleaner than freeform JSON parsing.
- Batching: `batch_size` notes per call by default. Keep batches small enough
  that a single low-confidence note in a batch doesn't force re-running the whole
  batch on Sonnet during escalation — we escalate per-note instead.
- Escalation: any suggestion with confidence < `escalate_below` is re-classified
  individually on the stronger model.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import anthropic
import httpx

from .config import AnthropicConfig
from .scopes_loader import LoadedScopes

log = logging.getLogger(__name__)


def build_anthropic_client(cfg: AnthropicConfig) -> anthropic.Anthropic:
    """Build an Anthropic SDK client that respects corporate-proxy SSL settings.

    When `ssl_verify=False` we hand the SDK a pre-configured httpx client so the
    SDK's internal client doesn't trip on intercepted TLS (Zscaler/Netskope).
    Mirrors the PB client's `ssl_verify=false` escape hatch.
    """
    if not cfg.api_key:
        raise ValueError(
            "Anthropic API key missing. Set [anthropic].api_key or ANTHROPIC_API_KEY."
        )
    kwargs: dict[str, Any] = {"api_key": cfg.api_key}
    if not cfg.ssl_verify:
        log.info("anthropic: TLS verification disabled (corporate proxy mode)")
        kwargs["http_client"] = httpx.Client(verify=False, timeout=cfg.timeout_seconds)
    return anthropic.Anthropic(**kwargs)


SYSTEM_INSTRUCTIONS_EN = """You classify Productboard feedback notes to the correct product manager (PM) at Aidn, a Norwegian healthcare software company.

The note text is in Norwegian. It may contain English technical terms. Do NOT translate the note; classify in place.

Below you have:
  1. Global routing principles (in Norwegian).
  2. One scope document per PM (in Norwegian), describing their team's responsibility, strong signals, excludes, and disambiguations.

For every note in the input, output exactly one classification record:
  - note_id: the id the user provided
  - pm_email: one of the PM emails from the scope docs, or null if the note should be left unassigned (test notes, VFT/VKP integrations, internal automation feedback, stub notes).
  - confidence: a number from 0.0 to 1.0. Follow the guidance in the global scope's `confidence_guidance`.
  - reasoning: one short sentence in Norwegian (or English if the signal is English-language), explaining the key signal you used. Max 200 characters.

Rules:
  - If a PB tag matches a PM's tag_routes, that's a strong signal (≥ 0.85) unless clearly contradicted by the body.
  - "Domain over technology": route by what the note is about, not by which technology is mentioned. E.g. "tale-til-tekst for kartlegging" is Case Handling, not AI.
  - If two PMs plausibly match, prefer the more specific domain signal. If truly ambiguous, lower the confidence — do not split.
  - If the note matches an exclude_rule in _global.yaml, set pm_email = null.
  - Never invent an email that isn't in the scope docs.
"""


def build_system_block(scopes: LoadedScopes) -> list[dict]:
    """Return the system-message content array with prompt caching applied.

    Anthropic caches the prefix up to and including the last content block
    marked with cache_control. We put everything static there and let the user
    message carry the per-call delta.
    """
    parts: list[str] = [SYSTEM_INSTRUCTIONS_EN, "\n\n# Global routing\n", scopes.global_yaml]
    for email in sorted(scopes.per_pm):
        parts.append(f"\n\n# Scope: {email}\n")
        parts.append(scopes.per_pm[email])
    text = "".join(parts)
    return [
        {
            "type": "text",
            "text": text,
            "cache_control": {"type": "ephemeral"},
        }
    ]


# The `classify_notes` tool forces a strict JSON-array response shape.
CLASSIFY_TOOL = {
    "name": "classify_notes",
    "description": "Return a classification for each input note.",
    "input_schema": {
        "type": "object",
        "properties": {
            "classifications": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "note_id": {"type": "string"},
                        "pm_email": {
                            "type": ["string", "null"],
                            "description": "A PM email from the scope docs, or null to leave unassigned.",
                        },
                        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "reasoning": {"type": "string", "maxLength": 300},
                    },
                    "required": ["note_id", "pm_email", "confidence", "reasoning"],
                },
            }
        },
        "required": ["classifications"],
    },
}


@dataclass
class NoteForClassification:
    note_id: str          # string so we can use our DB id or PB uuid interchangeably
    title: str
    content: str
    tags: list[str]
    company: str


@dataclass
class Classification:
    note_id: str
    pm_email: str | None
    confidence: float
    reasoning: str
    model: str
    escalated: bool = False


class Classifier:
    def __init__(self, cfg: AnthropicConfig, scopes: LoadedScopes,
                 client: anthropic.Anthropic | None = None):
        self.cfg = cfg
        self.scopes = scopes
        self.client = client or build_anthropic_client(cfg)
        self._system_block = build_system_block(scopes)

    # ─── public API ───────────────────────────────────────────────────────────

    def classify_batch(self, notes: list[NoteForClassification]) -> list[Classification]:
        """Classify a batch on the default model. Caller handles escalation."""
        if not notes:
            return []
        user_payload = {
            "notes": [self._note_to_dict(n) for n in notes],
        }
        return self._call(notes, user_payload, self.cfg.model_default, escalated=False)

    def classify_one_escalated(self, note: NoteForClassification) -> Classification:
        """Re-classify a single note on the escalation model."""
        user_payload = {"notes": [self._note_to_dict(note)]}
        results = self._call([note], user_payload, self.cfg.model_escalate, escalated=True)
        if not results:
            # Model returned no classification; fall back to a zero-confidence "leave open".
            return Classification(
                note_id=note.note_id,
                pm_email=None,
                confidence=0.0,
                reasoning="Escalation returned no classification.",
                model=self.cfg.model_escalate,
                escalated=True,
            )
        return results[0]

    def classify_with_escalation(
        self, notes: list[NoteForClassification]
    ) -> list[Classification]:
        """End-to-end: batch → escalate low-confidence items individually."""
        results: list[Classification] = []
        total_batches = (len(notes) + self.cfg.batch_size - 1) // self.cfg.batch_size
        log.info(
            "classify: starting — %d notes, %d batches of %d, model=%s",
            len(notes), total_batches, self.cfg.batch_size, self.cfg.model_default,
        )
        for batch_idx, i in enumerate(range(0, len(notes), self.cfg.batch_size), start=1):
            chunk = notes[i : i + self.cfg.batch_size]
            log.info(
                "classify: batch %d/%d — %d notes",
                batch_idx, total_batches, len(chunk),
            )
            try:
                results.extend(self.classify_batch(chunk))
            except Exception as e:
                log.exception(
                    "classify: batch %d/%d FAILED (%s): %s",
                    batch_idx, total_batches, type(e).__name__, e,
                )
                raise

        # Escalate per-note.
        by_id = {n.note_id: n for n in notes}
        escalations = [c for c in results if c.confidence < self.cfg.escalate_below]
        if escalations:
            log.info(
                "classify: escalating %d/%d notes below confidence %.2f to %s",
                len(escalations), len(results), self.cfg.escalate_below, self.cfg.model_escalate,
            )
        for idx, c in enumerate(results):
            if c.confidence < self.cfg.escalate_below and c.note_id in by_id:
                log.info("classify: escalating note %s (conf=%.2f)", c.note_id, c.confidence)
                try:
                    results[idx] = self.classify_one_escalated(by_id[c.note_id])
                except Exception as e:
                    log.exception(
                        "classify: escalation for note %s FAILED (%s): %s",
                        c.note_id, type(e).__name__, e,
                    )
                    # keep the non-escalated result rather than blowing up the whole batch
        return results

    # ─── internals ────────────────────────────────────────────────────────────

    def _note_to_dict(self, n: NoteForClassification) -> dict:
        # Trim very long bodies — the model only needs ~first 4 KB to classify a note.
        body = n.content
        if len(body) > 4000:
            body = body[:4000] + "\n…[truncated]"
        return {
            "note_id": n.note_id,
            "title": n.title,
            "body": body,
            "tags": n.tags,
            "company": n.company,
        }

    def _call(
        self,
        notes: list[NoteForClassification],
        user_payload: dict,
        model: str,
        escalated: bool,
    ) -> list[Classification]:
        response = self.client.messages.create(
            model=model,
            max_tokens=2048,
            system=self._system_block,
            tools=[CLASSIFY_TOOL],
            tool_choice={"type": "tool", "name": "classify_notes"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Classify the notes below. Use the scope docs in the system "
                                "prompt as your source of truth.\n\n"
                                f"{json.dumps(user_payload, ensure_ascii=False, indent=2)}"
                            ),
                        }
                    ],
                }
            ],
        )

        log_cache_usage(response, model)

        tool_block = next(
            (b for b in response.content if getattr(b, "type", None) == "tool_use"),
            None,
        )
        if tool_block is None:
            log.warning("model returned no tool_use block; content=%r", response.content)
            return []

        data = tool_block.input or {}
        raw_classifications = data.get("classifications", []) or []
        out: list[Classification] = []
        for c in raw_classifications:
            out.append(
                Classification(
                    note_id=str(c.get("note_id", "")),
                    pm_email=_normalize_pm(c.get("pm_email")),
                    confidence=float(c.get("confidence") or 0.0),
                    reasoning=str(c.get("reasoning") or ""),
                    model=model,
                    escalated=escalated,
                )
            )
        return out


def _normalize_pm(pm_email: Any) -> str | None:
    if pm_email is None:
        return None
    s = str(pm_email).strip().lower()
    if not s or s in ("null", "none", "open", "unassigned"):
        return None
    return s


def log_cache_usage(response: Any, model: str) -> None:
    """Log token usage + cache hits so we can see caching actually working."""
    u = getattr(response, "usage", None)
    if u is None:
        return
    log.info(
        "model=%s in=%s out=%s cache_create=%s cache_read=%s",
        model,
        getattr(u, "input_tokens", "?"),
        getattr(u, "output_tokens", "?"),
        getattr(u, "cache_creation_input_tokens", 0),
        getattr(u, "cache_read_input_tokens", 0),
    )
