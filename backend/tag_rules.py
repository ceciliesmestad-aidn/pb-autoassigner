"""Fixed, tag-based routing rules that run before the AI classifier.

Mitt Aidn → Fredrik Pedersen (Team Patient), added 2026-11-17 per Cecilie.

Feedback from the Mitt Aidn app (patients and next of kin) arrives in PB with
the tag "Mitt Aidn". Those notes belong to Fredrik — UNLESS the note is clearly
about calendar/scheduling, which stays with William Knudtzon (Team Scheduling).
"Clearly about scheduling" is a fixed word list, not an AI judgement, so the
outcome is the same every run and easy to check. Scheduling notes are simply
not caught by this rule and go through normal AI routing, whose scope files
already send calendar/appointments to William.
"""
from __future__ import annotations

import re

MITT_AIDN_TAG = "mitt aidn"
MITT_AIDN_PM = "fredrik.pedersen@aidn.no"
RULE_MODEL = "rule:mitt-aidn"

# Word stems that mark a note as calendar/scheduling. Matched case-insensitively
# at the start of a word ("avtale" also catches "avtaler", "avtalen").
SCHEDULING_STEMS = (
    "kalender",
    "avdelingskalender",
    "avtale",
    "gruppeavtale",
    "timebok",
    "timebestilling",
    "timeavtale",
    "bestille time",
    "booke",
    "booking",
    "påminnelse",
    "sms-varsel",
    "sms varsel",
    "innkalling",
)
_SCHED_RE = re.compile(r"\b(" + "|".join(re.escape(s) for s in SCHEDULING_STEMS) + r")",
                       re.IGNORECASE)


# Applications (søknader) submitted through Mitt Aidn → Kristin Shovick (Team
# Case Handling), per Cecilie 2026-11-17. Fixed rule, confidence 1.0.
APPLICATION_PM = "kristin.shovick@aidn.no"
APPLICATION_STEMS = (
    "søknad",
    "søke om",
    "plassering",
    "vedtak",
)
_APP_RE = re.compile(r"\b(" + "|".join(re.escape(s) for s in APPLICATION_STEMS) + r")",
                     re.IGNORECASE)


def _norm_tags(tags) -> set[str]:
    return {(t or "").strip().lower() for t in (tags or [])}


def has_mitt_aidn_tag(tags) -> bool:
    t = _norm_tags(tags)
    return MITT_AIDN_TAG in t or "mittaidn" in t


def scheduling_hits(title: str, content: str) -> list[str]:
    """The scheduling words found in the note (empty list = not scheduling)."""
    text = f"{title or ''}\n{content or ''}"
    return sorted({m.group(1).lower() for m in _SCHED_RE.finditer(text)})


def application_hits(title: str, content: str) -> list[str]:
    """The application words found in the note (empty list = not an application)."""
    text = f"{title or ''}\n{content or ''}"
    return sorted({m.group(1).lower() for m in _APP_RE.finditer(text)})


def mitt_aidn_route(title: str, content: str, tags) -> str | None:
    """Who a Mitt Aidn-tagged note goes to, or None for normal AI routing.

    Order: calendar/scheduling → None (AI, → William); søknad → Kristin;
    everything else → Fredrik.
    """
    if not has_mitt_aidn_tag(tags):
        return None
    if scheduling_hits(title, content):
        return None  # calendar/scheduling → normal routing (→ William)
    if application_hits(title, content):
        return APPLICATION_PM
    return MITT_AIDN_PM
