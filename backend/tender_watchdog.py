"""Tender commitment board watchdog.

Answers the question raised in #team-product-operations on 2026-09-07: when a
new BAFO goes in, how do we know the Tender Commitment board actually got
updated?

Background
----------
The documented routine ("Managing tender requirements as insights in
Productboard", Notion → Ways of Working) has four steps:

  1. Tender team logs requirements as Notes in PB (within ~5 days of BAFO)
  2. PB AutoAssigner routes each note to the right PM
  3. PM links the insight to a feature
  4. Product Director fills in the Committed Deadline date on that feature

Step 4 silently assumes a `<Tender> - Committed deadline` DATE field already
exists, and that the Tender Commitment board has a matching column + filter.
Nobody owns creating those. That is the gap this module detects.

What it can and cannot do
-------------------------
Productboard's public API v2 has NO endpoints for prioritization boards, and
no endpoint for creating custom *field definitions* — only for creating
*values* (options) of an existing select field. So the last mile is
irreducibly manual:

  automatable   : read tender options, read committed-deadline fields, compare
  NOT automatable: create the date field, add the board column, add the board
                   filter  → these are UI-only, and the report spells out the
                   exact click-path plus who owns it.

The watchdog therefore never writes to Productboard. It reads, compares, and
tells a human what to go and do. Read-only by design — a wrong rename here
would break every board using the field as a column.

Findings produced
-----------------
  MISSING_DEADLINE_FIELD  tender has T/S options but no committed-deadline
                          field → needs field + board column + board filter
  MISSING_TENDER_OPTIONS  committed-deadline field exists but the tender has
                          no T/S options → tender team has not logged it
  NAMING_DRIFT            the two sides only matched via the alias table, i.e.
                          the names do not follow the convention
  PREFIX_ANOMALY          a tender option whose prefix is neither T nor S
  INCOMPLETE_PAIR         a tender with a T option but no S option, or vice
                          versa

Convention this module assumes
------------------------------
  date field     : "<Tender> - Committed deadline"
  tender options : "T - <Tender>" (hard requirement / krav)
                   "S - <Tender>" (soft commitment)
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

log = logging.getLogger(__name__)

# The multi-select field on features that carries one T/S option per tender.
TENDER_REQUIREMENTS_FIELD_NAME = "Tender requirements"

# Canonical shape of a per-tender commitment date field.
CANONICAL_DEADLINE_SUFFIX = " - Committed deadline"

# Matches "<Tender> - Committed deadline" and the off-convention
# "<Tender> - Committed date" so drift is *reported*, not missed.
DEADLINE_FIELD_RE = re.compile(
    r"^(?P<tender>.+?)\s*[-–]\s*committed\s+(?P<noun>deadline|date)\s*$",
    re.IGNORECASE,
)

# Matches "T - Bergen" / "S - Bergen", and any other single-letter prefix so
# anomalies like "B - Bodø" surface instead of being silently dropped.
TENDER_OPTION_RE = re.compile(
    r"^(?P<prefix>[A-Za-zÆØÅæøå])\s*[-–]\s*(?P<tender>.+?)\s*$",
)

EXPECTED_PREFIXES = ("T", "S")

# Known, accepted differences between how a tender is named on the option and
# on the date field. Keys and values are both normalised forms (see _norm).
# An entry here means "these two are the same tender" — the pair is still
# reported as NAMING_DRIFT so a human can decide whether to align the names.
TENDER_ALIASES: dict[str, str] = {
    "innlandet": "digi innlandet",
    "kongsberg": "kongsbergregionen",
    "lofoten": "lofoten ikt",
    "ulvik og eidsfjord": "ulvik og eidfjord",
    "ullenvang": "ullensvang",
    "rf": "romerike og follo",
}

# Option names that are deliberately not tender commitments and must not be
# treated as one. Normalised.
OPTION_IGNORE: set[str] = {
    "vaernes usability test",
}


# ─── normalisation ────────────────────────────────────────────────────────────

def _norm(name: str) -> str:
    """Fold a tender name to a comparable key.

    Lowercases, unifies "&"/"og", turns hyphens and multiple spaces into single
    spaces, and strips diacritics *except* that Norwegian æ/ø/å are expanded to
    ae/oe/aa first so "Sør-Varanger" and "Sor Varanger" collide as intended.
    """
    s = name.strip().lower()
    s = s.replace("&", " og ")
    s = (s.replace("æ", "ae").replace("ø", "oe").replace("å", "aa"))
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[-–_/,]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _canonical(norm_name: str) -> str:
    """Resolve a normalised tender name through the alias table."""
    return TENDER_ALIASES.get(norm_name, norm_name)


# ─── data model ───────────────────────────────────────────────────────────────

@dataclass
class Finding:
    kind: str          # one of the FINDING_* constants below
    tender: str        # tender name as displayed (best available)
    detail: str        # one-line human explanation
    action: str = ""   # what a human must do, in the UI
    severity: str = "warning"   # "alert" | "warning" | "info"


FINDING_MISSING_FIELD = "MISSING_DEADLINE_FIELD"
FINDING_MISSING_OPTIONS = "MISSING_TENDER_OPTIONS"
FINDING_NAMING_DRIFT = "NAMING_DRIFT"
FINDING_PREFIX_ANOMALY = "PREFIX_ANOMALY"
FINDING_INCOMPLETE_PAIR = "INCOMPLETE_PAIR"


@dataclass
class WatchdogReport:
    tenders_with_options: list[str] = field(default_factory=list)
    tenders_with_deadline_field: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    # True when the tender-requirements field itself could not be located —
    # the report is then meaningless and must not be read as "all clear".
    inconclusive: str = ""

    @property
    def alerts(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "alert"]

    @property
    def ok(self) -> bool:
        return not self.inconclusive and not self.findings

    def to_dict(self) -> dict:
        return {
            "inconclusive": self.inconclusive,
            "tenders_with_options": self.tenders_with_options,
            "tenders_with_deadline_field": self.tenders_with_deadline_field,
            "findings": [
                {
                    "kind": f.kind,
                    "tender": f.tender,
                    "detail": f.detail,
                    "action": f.action,
                    "severity": f.severity,
                }
                for f in self.findings
            ],
        }


# ─── parsing ──────────────────────────────────────────────────────────────────

def parse_deadline_fields(fields: Iterable[dict]) -> dict[str, dict]:
    """Pick the per-tender commitment date fields out of a field list.

    `fields` is the list of field descriptors from
    GET /v2/entities/configurations/feature — each item is expected to carry at
    least a display name (`name` or `label`) and a type (`type` or `fieldType`).

    Returns {canonical_tender_key: {"display": <tender as written>,
                                    "field_name": <full field name>,
                                    "off_convention": bool}}
    """
    out: dict[str, dict] = {}
    for f in fields:
        name = (f.get("name") or f.get("label") or "").strip()
        if not name:
            continue
        m = DEADLINE_FIELD_RE.match(name)
        if not m:
            continue
        ftype = str(f.get("type") or f.get("fieldType") or "").upper()
        # Only DATE fields are commitment dates. If the payload does not tell
        # us the type, accept it rather than silently dropping a real field.
        if ftype and "DATE" not in ftype:
            continue
        tender_display = m.group("tender").strip()
        key = _canonical(_norm(tender_display))
        out[key] = {
            "display": tender_display,
            "field_name": name,
            "off_convention": name != tender_display + CANONICAL_DEADLINE_SUFFIX,
            "id": f.get("id", ""),
        }
    return out


def parse_tender_options(options: Iterable[dict]) -> tuple[dict[str, dict], list[Finding]]:
    """Group the Tender requirements select options by tender.

    Returns ({canonical_tender_key: {"display", "prefixes": set, "raw": [...]}} ,
             [PREFIX_ANOMALY findings])
    """
    grouped: dict[str, dict] = {}
    anomalies: list[Finding] = []

    for opt in options:
        raw = (opt.get("name") or opt.get("label") or "").strip()
        if not raw or _norm(raw) in OPTION_IGNORE:
            continue

        m = TENDER_OPTION_RE.match(raw)
        if not m:
            # No "X - " prefix at all. Still a real tender as far as coverage
            # goes — Bergen looked exactly like this in September 2026 — so it
            # is grouped anyway, with an empty prefix set. Dropping it here
            # would hide a missing board column behind a cosmetic warning.
            anomalies.append(Finding(
                kind=FINDING_PREFIX_ANOMALY,
                tender=raw,
                detail=f'Option "{raw}" does not follow "T - <Tender>" / "S - <Tender>".',
                action=(
                    f'Tender team: rename to "T - {raw}" (hard) or "S - {raw}" (soft) '
                    "so it is clear which kind of commitment it is."
                ),
                severity="info",
            ))
            key = _canonical(_norm(raw))
            entry = grouped.setdefault(
                key, {"display": raw, "prefixes": set(), "raw": []}
            )
            entry["raw"].append(raw)
            continue

        prefix = m.group("prefix").upper()
        tender_display = m.group("tender").strip()
        key = _canonical(_norm(tender_display))

        entry = grouped.setdefault(
            key, {"display": tender_display, "prefixes": set(), "raw": []}
        )
        entry["prefixes"].add(prefix)
        entry["raw"].append(raw)

        if prefix not in EXPECTED_PREFIXES:
            anomalies.append(Finding(
                kind=FINDING_PREFIX_ANOMALY,
                tender=tender_display,
                detail=f'Option "{raw}" uses prefix "{prefix}" — expected T (hard) or S (soft).',
                action=f'Tender team: rename to "T - {tender_display}" or "S - {tender_display}".',
                severity="info",
            ))

    return grouped, anomalies


# ─── the comparison ───────────────────────────────────────────────────────────

def build_report(
    *,
    feature_fields: Iterable[dict],
    tender_options: Iterable[dict] | None,
) -> WatchdogReport:
    """Compare tender options against committed-deadline fields.

    Pure function — no network, no writes. Everything in this module that
    matters is testable through here.
    """
    report = WatchdogReport()

    if tender_options is None:
        report.inconclusive = (
            f'Could not read the "{TENDER_REQUIREMENTS_FIELD_NAME}" field from '
            "Productboard. No conclusion can be drawn about board coverage."
        )
        return report

    deadline_fields = parse_deadline_fields(feature_fields)
    options, anomalies = parse_tender_options(tender_options)

    report.tenders_with_options = sorted(v["display"] for v in options.values())
    report.tenders_with_deadline_field = sorted(
        v["display"] for v in deadline_fields.values()
    )
    report.findings.extend(anomalies)

    # 1. Tender logged, but no commitment date field → the actual board gap.
    for key, opt in sorted(options.items()):
        if key in deadline_fields:
            continue
        name = opt["display"]
        report.findings.append(Finding(
            kind=FINDING_MISSING_FIELD,
            tender=name,
            detail=(
                f'"{name}" has tender requirements in Productboard '
                f'({", ".join(sorted(opt["raw"]))}) but no '
                f'"{name}{CANONICAL_DEADLINE_SUFFIX}" field exists, so it cannot '
                "appear on the Tender Commitment board."
            ),
            action=(
                "PB admin, in the UI (no API for this): "
                f'1) create a DATE custom field named "{name}{CANONICAL_DEADLINE_SUFFIX}" '
                f"and scope it to the {name} teamspace + Aidn roadmap; "
                "2) add it as a column on the Tender Commitment board; "
                "3) add it to the board filter."
            ),
            severity="alert",
        ))

    # 2. Commitment date field exists, but nothing logged against the tender.
    for key, fld in sorted(deadline_fields.items()):
        if key in options:
            continue
        name = fld["display"]
        report.findings.append(Finding(
            kind=FINDING_MISSING_OPTIONS,
            tender=name,
            detail=(
                f'Field "{fld["field_name"]}" exists but "{TENDER_REQUIREMENTS_FIELD_NAME}" '
                f'has no "T - {name}" / "S - {name}" option — either the tender\'s '
                "requirements were never logged, or the names disagree."
            ),
            action=(
                f"Tender team: confirm whether {name} requirements are in PB. If they "
                f'are, check the option naming; if not, log them as Notes.'
            ),
            severity="alert",
        ))

    # 3. Matched, but only via the alias table → the names disagree.
    for key, opt in sorted(options.items()):
        fld = deadline_fields.get(key)
        if not fld:
            continue
        opt_norm = _norm(opt["display"])
        fld_norm = _norm(fld["display"])
        reasons = []
        if opt_norm != fld_norm:
            reasons.append(
                f'option says "{opt["display"]}", field says "{fld["display"]}"'
            )
        if fld["off_convention"]:
            reasons.append(
                f'field is named "{fld["field_name"]}", convention is '
                f'"{fld["display"]}{CANONICAL_DEADLINE_SUFFIX}"'
            )
        if reasons:
            report.findings.append(Finding(
                kind=FINDING_NAMING_DRIFT,
                tender=opt["display"],
                detail="; ".join(reasons) + ".",
                action=(
                    "Report only — renaming a custom field affects every board using "
                    "it as a column. Decide with the PB admin before changing anything."
                ),
                severity="warning",
            ))

    # 4. T without S, or S without T.
    for key, opt in sorted(options.items()):
        missing = [p for p in EXPECTED_PREFIXES if p not in opt["prefixes"]]
        if not missing or not (opt["prefixes"] & set(EXPECTED_PREFIXES)):
            # Nothing missing, or nothing recognisable at all (already reported
            # as a PREFIX_ANOMALY above).
            continue
        name = opt["display"]
        kinds = {"T": "hard requirements (T)", "S": "soft commitments (S)"}
        report.findings.append(Finding(
            kind=FINDING_INCOMPLETE_PAIR,
            tender=name,
            detail=(
                f'"{name}" has no ' + " or ".join(kinds[p] for p in missing) +
                " option. Soft commitments were the exact gap flagged in "
                "#support-productboard on 2026-08-28."
            ),
            action=(
                f'Tender team: confirm whether {name} really had no '
                + " / ".join(kinds[p] for p in missing) +
                ", and add the option if it did."
            ),
            severity="warning",
        ))

    return report


# ─── live run ─────────────────────────────────────────────────────────────────

def run(client) -> WatchdogReport:
    """Fetch the feature field configuration from PB and build the report.

    `client` is a backend.pb_client.PBClient. Read-only: this issues GETs only.
    """
    fields = client.feature_field_config()
    tender_field = None
    for f in fields:
        name = (f.get("name") or f.get("label") or "").strip()
        if name.casefold() == TENDER_REQUIREMENTS_FIELD_NAME.casefold():
            tender_field = f
            break

    if tender_field is None:
        log.error('watchdog: field "%s" not found on features',
                  TENDER_REQUIREMENTS_FIELD_NAME)
        return build_report(feature_fields=fields, tender_options=None)

    options = client.select_field_values(tender_field)
    if options is None:
        log.error("watchdog: could not read options for the tender-requirements field")
        return build_report(feature_fields=fields, tender_options=None)

    return build_report(feature_fields=fields, tender_options=options)
