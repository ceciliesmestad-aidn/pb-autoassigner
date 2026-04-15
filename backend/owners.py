"""Owner / PM registry.

This is the canonical email map, ported from v1. Emails must match exactly
what Productboard has on file — mismatches cause 422 errors on PATCH.
Run `pb-assigner verify-map` to sanity-check against the live PB workspace.

Known quirks (2026-04):
- Sandra's email has double 'a': `otteraaen`
- Sally Renshaw's email had issues in the April 2026 batch — verify before bulk assign
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PM:
    email: str
    name: str
    team: str
    scope_file: str  # filename under scopes/ (without .yaml)


PMS: list[PM] = [
    PM("line.adde@aidn.no",          "Line Adde",              "Team CPR",                 "line_adde"),
    PM("sandra.otteraaen@aidn.no",   "Sandra Otteraaen",       "Team Treatment",           "sandra_otteraaen"),
    PM("kristin.hoiaas@aidn.no",     "Kristin Shovick Hoiaas", "Team Case Handling",       "kristin_hoiaas"),
    PM("hanne.linaae@aidn.no",       "Hanne Linaae",           "Team Messaging",           "hanne_linaae"),
    PM("erik.story@aidn.no",         "Erik Story",             "Team Patient",             "erik_story"),
    PM("jens.malm@aidn.no",          "Jens Aga Malm",          "Team Back Office",         "jens_malm"),
    PM("abraham.guzman@aidn.no",     "Abraham Guzman",         "Team IAM",                 "abraham_guzman"),
    PM("ashild.herdlevaer@aidn.no",  "Ashild Dronen Herdlevaer", "Team Collaboration",     "ashild_herdlevaer"),
    PM("sally.renshaw@aidn.no",      "Sally Renshaw",          "Design System",            "sally_renshaw"),
    PM("therese.borter@aidn.no",     "Therese Borter",         "Team Navigator",           "therese_borter"),
    PM("viktor.ernholm@aidn.no",     "Viktor Ernholm",         "Team Mobile App",          "viktor_ernholm"),
]

BY_EMAIL: dict[str, PM] = {p.email: p for p in PMS}


def get_by_email(email: str) -> PM | None:
    return BY_EMAIL.get(email.lower().strip())
