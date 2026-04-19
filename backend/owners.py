"""Owner / PM registry.

This is the canonical email map, ported from v1. Emails must match exactly
what Productboard has on file — mismatches cause 422 errors on PATCH.
Run `pb-assigner verify-map` to sanity-check against the live PB workspace.

Known quirks (2026-04):
- Sandra's email has double 'a': `otteraaen`
- Sally Renshaw's email had issues in the April 2026 batch — verify before bulk assign

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


# ─── hardcoded list ───────────────────────────────────────────────────────────

_BUILTIN_PMS: list[PM] = [
    PM("line.adde@aidn.no",          "Line Adde",              "Team CPR",                 "line_adde"),
    PM("sandra.otteraaen@aidn.no",   "Sandra Otteraaen",       "Team Treatment",           "sandra_otteraaen"),
    PM("kristin.shovick@aidn.no",     "Kristin Shovick Hoiaas", "Team Case Handling",       "kristin_hoiaas"),
    PM("fredrik.behn@aidn.no",        "Fredrik Behn",           "Team OpenAIdn",             "fredrik_behn"),
    PM("hanne.linaae@aidn.no",       "Hanne Linaae",           "Team Messaging",           "hanne_linaae"),
    PM("erik.story@aidn.no",         "Erik Story",             "Team Patient",             "erik_story"),
    PM("jens.malm@aidn.no",          "Jens Aga Malm",          "Team Back Office",         "jens_malm"),
    PM("abraham.guzman@aidn.no",     "Abraham Guzman",         "Team IAM",                 "abraham_guzman"),
    PM("ashild.herdlevaer@aidn.no",  "Ashild Dronen Herdlevaer", "Team Collaboration",     "ashild_herdlevaer"),
    PM("sally.renshaw@aidn.no",      "Sally Renshaw",          "Design System",            "sally_renshaw"),
    PM("therese.borter@aidn.no",     "Therese Borter",         "Team Navigator",           "therese_borter"),
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
