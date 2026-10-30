"""Owner / PM registry.

This is the canonical email map, ported from v1. Emails must match exactly
what Productboard has on file — mismatches cause 422 errors on PATCH.
Run `pb-assigner verify-map` to sanity-check against the live PB workspace.

Known quirks (2026-04):
- Sandra's email has double 'a': `otteraaen`

Interim cover (added 2026-10-30): a team without its own PM can be routed to
another PM while keeping its own team tag. The registry entry gets a routing
key as `email` (what the classifier returns) and the real PB owner in
`pb_owner_email`. Example: Team Navigator → Erik Story while Navigator has no PM.
Notes are owned by Erik in PB but tagged "Team Navigator", so they can be
moved to a new Navigator PM later by filtering on owner + tag.

Custom PMs can be added at runtime via `POST /api/pms` and are stored in
`pms_custom.json` at the project root (next to config.toml). That file is
NOT gitignored and should be committed — it is the persistent source of
truth for any PMs added after the initial deployment.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import PROJECT_ROOT

# Project-root level, same layer as config.toml and the scopes/ directory.
# Not in data/ (which is semi-ignored) so it's tracked by git from the start.
CUSTOM_PMS_PATH = PROJECT_ROOT / "pms_custom.json"


@dataclass(frozen=True)
class PM:
    email: str
    name: str
    team: str
    scope_file: str  # filename under scopes/ (without .yaml)
    # Set only for interim-cover entries: the PB member who actually owns the
    # notes. `email` is then just a routing key and is never sent to PB.
    pb_owner_email: str | None = None

    @property
    def owner_email(self) -> str:
        """Email to use as note owner in Productboard."""
        return self.pb_owner_email or self.email

    @property
    def is_interim_cover(self) -> bool:
        return bool(self.pb_owner_email) and self.pb_owner_email != self.email


# ─── hardcoded list ───────────────────────────────────────────────────────────

_BUILTIN_PMS: list[PM] = [
    PM("line.adde@aidn.no",          "Line Adde",              "Team CPR",                 "line_adde"),
    PM("sandra.otteraaen@aidn.no",   "Sandra Otteraaen",       "Team Treatment",           "sandra_otteraaen"),
    PM("kristin.shovick@aidn.no",     "Kristin Shovick Hoiaas", "Team Case Handling",       "kristin_hoiaas"),
    PM("fredrik.behn@aidn.no",        "Fredrik Behn",           "Team OpenAIdn",             "fredrik_behn"),
    PM("hanne.linaae@aidn.no",       "Hanne Linaae",           "Team Messaging",           "hanne_linaae"),
    PM("erik.story@aidn.no",         "Erik Story",             "Team AI & Automation",     "erik_story"),
    PM("fredrik.pedersen@aidn.no",   "Fredrik Pedersen",       "Team Patient",             "fredrik_pedersen"),
    PM("jens.malm@aidn.no",          "Jens Aga Malm",          "Team Back Office",         "jens_malm"),
    PM("silje.larsen@aidn.no",       "Silje Larsen",           "Team IAM",                 "silje_larsen"),  # interim PM, replaced Abraham Guzman 2026-10-05
    PM("ashild.herdlevaer@aidn.no",  "Ashild Dronen Herdlevaer", "Team Collaboration",     "ashild_herdlevaer"),
    # Design System isn't a product team; Ronald (Sr. front-end, Design System)
    # takes its feedback. Replaced Sally Renshaw (left Aidn) 2026-10-30.
    PM("ronald.baars@aidn.no",       "Ronald Baars",           "Design System",            "ronald_baars"),
    # Team Navigator: Therese Borter is leaving; Erik Story is interim PM from
    # 2026-10-30. Routing key ≠ real email so Navigator notes keep the
    # "Team Navigator" tag. When a new Navigator PM starts: set email to theirs,
    # drop pb_owner_email, rename the scope file, and move Erik's notes tagged
    # "Team Navigator" (scripts/migrate_owner.py --tag "Team Navigator").
    PM("navigator.interim@aidn.no",  "Erik Story (interim Navigator)", "Team Navigator", "team_navigator",
       pb_owner_email="erik.story@aidn.no"),
    PM("viktor.ernholm@aidn.no",     "Viktor Ernholm",         "Team Mobile App",          "viktor_ernholm"),
]


# ─── runtime list (builtin + custom) ─────────────────────────────────────────

def _load_custom() -> list[PM]:
    if not CUSTOM_PMS_PATH.exists():
        return []
    try:
        raw = json.loads(CUSTOM_PMS_PATH.read_text(encoding="utf-8"))
        return [PM(**entry) for entry in raw]
    except Exception:
        return []


def get_all() -> list[PM]:
    """Return built-in PMs merged with any custom ones from data/pms_custom.json."""
    custom = _load_custom()
    custom_emails = {p.email for p in custom}
    merged = [p for p in _BUILTIN_PMS if p.email not in custom_emails]
    merged.extend(custom)
    return merged


# Module-level alias kept for backward compatibility — code that already does
# `from .owners import PMS` continues to work; it just gets the builtin list.
# Use `get_all()` when you need the live merged list (including custom PMs).
PMS = _BUILTIN_PMS


def get_by_email(email: str) -> PM | None:
    email = email.lower().strip()
    for pm in get_all():
        if pm.email == email:
            return pm
    return None


# ─── PB team tags ─────────────────────────────────────────────────────────────

# The registry's team names don't all match the tag names that already exist
# in the Productboard workspace (checked live 2026-08-12). Map the exceptions
# here so assignments reuse the existing tags instead of creating near-
# duplicates. Teams not listed use their registry name as-is.
_PB_TEAM_TAG_OVERRIDES: dict[str, str] = {
    "Team CPR":             "Team Core Patient Record",
    "Team AI & Automation": "Team Automation",
    "Team OpenAIdn":        "Team Open Aidn",
    "Design System":        "Team Design System",
    "Product Leadership":   "Team Product Leadership",
}


def team_tag(team: str) -> str:
    """PB tag name for a registry team name."""
    return _PB_TEAM_TAG_OVERRIDES.get(team, team)


def pb_note_scope(pm_email: str) -> tuple[str, str | None]:
    """How to find a registry entry's notes in PB: (owner_email, required_tag).

    Normal PMs: their own email, no tag filter. Interim-cover entries (e.g. Team
    Navigator → Erik): the real owner's email, and only notes carrying the
    team's tag — so Navigator insights/training don't include Erik's own notes.
    Unknown emails fall through unchanged.
    """
    pm = get_by_email(pm_email)
    if pm is None:
        return pm_email, None
    if pm.is_interim_cover:
        return pm.owner_email, team_tag(pm.team)
    return pm.email, None


def has_tag(tags: list[str] | None, tag: str) -> bool:
    want = tag.strip().lower()
    return any((t or "").strip().lower() == want for t in (tags or []))


# ─── adding a new PM ──────────────────────────────────────────────────────────

def email_to_scope_file(email: str) -> str:
    """Derive a safe filename stem from an email address.

    'john.doe@aidn.no' → 'john_doe'
    """
    local = email.split("@")[0]
    return re.sub(r"[^a-z0-9]+", "_", local.lower()).strip("_")


def add_pm(email: str, name: str, team: str, scope_file: str | None = None) -> PM:
    """Persist a new PM to data/pms_custom.json and return the PM object.

    Raises ValueError if the email is already registered (builtin or custom).
    """
    email = email.lower().strip()
    if get_by_email(email) is not None:
        raise ValueError(f"PM with email {email!r} already exists.")
    sf = scope_file or email_to_scope_file(email)
    pm = PM(email=email, name=name, team=team, scope_file=sf)

    existing = _load_custom()
    existing.append(pm)
    CUSTOM_PMS_PATH.write_text(
        json.dumps([asdict(p) for p in existing], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return pm
