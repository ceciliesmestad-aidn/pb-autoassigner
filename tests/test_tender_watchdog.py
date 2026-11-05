"""Tests for the Tender Commitment board watchdog.

The LIVE_* fixtures are the real state of the Aidn workspace as read on
2026-09-07 (feature field list + "Tender requirements" options). Keeping them
verbatim means the tests assert what the watchdog would actually have said on
the day Pål reported the problem, not what a hand-rolled example implies.
"""
from __future__ import annotations

import pytest

from backend import notify, tender_watchdog as tw
from backend.config import Config


@pytest.fixture
def cfg() -> Config:
    c = Config()
    c.slack.webhook_url = "https://hooks.slack.com/services/T000/B000/XXX"
    c.slack.enabled = True
    return c


# ─── fixtures: real workspace state, 2026-09-07 ───────────────────────────────

LIVE_FEATURE_FIELDS = [
    {"name": "Description", "fieldType": "RICH_TEXT"},
    {"name": "Name", "fieldType": "TEXT"},
    {"name": "Status", "fieldType": "STATUS"},
    {"name": "Effort", "fieldType": "NUMBER"},
    {"name": "Earliest committed date", "fieldType": "DATE"},
    {"name": "Company Goal OKR", "fieldType": "MULTI_SELECT"},
    {"name": "Tender requirements", "fieldType": "MULTI_SELECT", "id": "fld-tender"},
    {"name": "Bodø - Committed deadline", "fieldType": "DATE"},
    {"name": "Digi Innlandet - Committed deadline", "fieldType": "DATE"},
    {"name": "DigiTV - Committed deadline", "fieldType": "DATE"},
    {"name": "Kongsbergregionen - Committed deadline", "fieldType": "DATE"},
    {"name": "Lofoten IKT - Committed deadline", "fieldType": "DATE"},
    {"name": "Romerike & Follo - Committed date", "fieldType": "DATE"},
    {"name": "Stavanger - Committed deadline", "fieldType": "DATE"},
    {"name": "Sør-Varanger - Committed deadline", "fieldType": "DATE"},
    {"name": "Ullensvang - Committed deadline", "fieldType": "DATE"},
    {"name": "Ulvik & Eidfjord - Committed deadline", "fieldType": "DATE"},
    {"name": "Voss - Committed deadline", "fieldType": "DATE"},
    {"name": "Værnes - Committed deadline", "fieldType": "DATE"},
    {"name": "Øygarden - Committed deadline", "fieldType": "DATE"},
]

LIVE_TENDER_OPTIONS = [
    {"name": "T - Innlandet"}, {"name": "S - Innlandet"},
    {"name": "Bergen"},
    {"name": "T - Sør-Varanger"}, {"name": "S - Sør Varanger"},
    {"name": "S - Kongsberg"}, {"name": "T - Kongsberg"},
    {"name": "T - DigiTV"}, {"name": "S - DigiTV"},
    {"name": "T - Ulvik og Eidsfjord"}, {"name": "S - Ulvik og Eidsfjord"},
    {"name": "T - Lofoten"}, {"name": "S - Lofoten"},
    {"name": "S - Øygarden"}, {"name": "T - Øygarden"},
    {"name": "T - Voss"}, {"name": "S - Voss"},
    {"name": "S - Ullenvang"}, {"name": "T - Ullensvang"},
    {"name": "S - Hadsel"}, {"name": "T - Hadsel"},
    {"name": "S - Bø, Øksnes, Askøy, Løddingen"},
    {"name": "T - Bø, Øksnes, Askøy, Løddingen"},
    {"name": "S - Værnes"}, {"name": "T - Værnes"},
    {"name": "Værnes - Usability test"},
    {"name": "S - Bodø"}, {"name": "B - Bodø"},
    {"name": "S - Stavanger"}, {"name": "T - Stavanger"},
]


@pytest.fixture
def live_report():
    return tw.build_report(
        feature_fields=LIVE_FEATURE_FIELDS,
        tender_options=LIVE_TENDER_OPTIONS,
    )


def _tenders(report, kind):
    return {f.tender for f in report.findings if f.kind == kind}


# ─── normalisation ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    ("Sør-Varanger", "Sør Varanger"),
    ("Ulvik & Eidfjord", "Ulvik og Eidfjord"),
    ("Romerike & Follo", "Romerike og Follo"),
    ("Bø, Øksnes, Askøy, Løddingen", "Bø Øksnes Askøy Løddingen"),
    ("  DigiTV  ", "digitv"),
])
def test_norm_collapses_equivalent_spellings(a, b):
    assert tw._norm(a) == tw._norm(b)


def test_norm_keeps_genuinely_different_tenders_apart():
    assert tw._norm("Voss") != tw._norm("Værnes")
    assert tw._norm("Bodø") != tw._norm("Bergen")


# ─── parsing ──────────────────────────────────────────────────────────────────

def test_parse_deadline_fields_picks_only_per_tender_dates():
    got = tw.parse_deadline_fields(LIVE_FEATURE_FIELDS)
    # "Earliest committed date" is a roll-up, not a tender — must not be counted.
    assert "earliest" not in got
    assert len(got) == 13
    assert got[tw._canonical(tw._norm("Voss"))]["field_name"] == "Voss - Committed deadline"


def test_parse_deadline_fields_ignores_non_date_fields():
    got = tw.parse_deadline_fields([
        {"name": "Fake - Committed deadline", "fieldType": "TEXT"},
        {"name": "Real - Committed deadline", "fieldType": "DATE"},
    ])
    assert set(got) == {"real"}


def test_parse_deadline_fields_flags_off_convention_name():
    got = tw.parse_deadline_fields(
        [{"name": "Romerike & Follo - Committed date", "fieldType": "DATE"}]
    )
    entry = next(iter(got.values()))
    assert entry["off_convention"] is True


def test_parse_tender_options_groups_t_and_s():
    grouped, anomalies = tw.parse_tender_options(
        [{"name": "T - Voss"}, {"name": "S - Voss"}]
    )
    assert len(grouped) == 1
    assert next(iter(grouped.values()))["prefixes"] == {"T", "S"}
    assert anomalies == []


def test_parse_tender_options_ignores_non_commitment_option():
    grouped, _ = tw.parse_tender_options([{"name": "Værnes - Usability test"}])
    assert grouped == {}


def test_parse_tender_options_flags_unexpected_prefix():
    _, anomalies = tw.parse_tender_options([{"name": "B - Bodø"}])
    assert [a.kind for a in anomalies] == [tw.FINDING_PREFIX_ANOMALY]


def test_parse_tender_options_flags_option_with_no_prefix():
    grouped, anomalies = tw.parse_tender_options([{"name": "Bergen"}])
    assert [a.kind for a in anomalies] == [tw.FINDING_PREFIX_ANOMALY]
    # Still counted as a tender — a cosmetic naming warning must not hide a
    # missing board column.
    assert next(iter(grouped.values()))["display"] == "Bergen"


def test_unprefixed_option_still_reported_as_missing_from_board(live_report):
    """Bergen: BAFO went in 2026-08-20, option exists with no T/S prefix, and
    there is no 'Bergen - Committed deadline' field. This is the case that
    must not slip through."""
    assert "Bergen" in _tenders(live_report, tw.FINDING_MISSING_FIELD)


def test_unprefixed_option_is_not_reported_as_incomplete_pair(live_report):
    """Bergen has neither T nor S, so INCOMPLETE_PAIR would be noise on top of
    the PREFIX_ANOMALY already raised."""
    assert "Bergen" not in _tenders(live_report, tw.FINDING_INCOMPLETE_PAIR)


# ─── the findings that matter ─────────────────────────────────────────────────

def test_flags_tenders_with_no_committed_deadline_field(live_report):
    """The actual bug Pål reported: requirements logged, nothing on the board."""
    missing = _tenders(live_report, tw.FINDING_MISSING_FIELD)
    assert "Hadsel" in missing
    assert "Bø, Øksnes, Askøy, Løddingen" in missing


def test_flags_field_with_no_tender_options(live_report):
    """Romerike & Follo has a date field but no T/S options at all."""
    orphans = _tenders(live_report, tw.FINDING_MISSING_OPTIONS)
    assert "Romerike & Follo" in orphans


def test_no_false_positive_for_healthy_tenders(live_report):
    """Voss, DigiTV and Stavanger are correctly set up on both sides."""
    broken = (
        _tenders(live_report, tw.FINDING_MISSING_FIELD)
        | _tenders(live_report, tw.FINDING_MISSING_OPTIONS)
    )
    for healthy in ("Voss", "DigiTV", "Stavanger", "Øygarden"):
        assert healthy not in broken


def test_alias_table_prevents_false_positives_for_known_renames(live_report):
    """Innlandet/Digi Innlandet and Kongsberg/Kongsbergregionen are the same
    tender, so neither may show up as missing."""
    broken = (
        _tenders(live_report, tw.FINDING_MISSING_FIELD)
        | _tenders(live_report, tw.FINDING_MISSING_OPTIONS)
    )
    assert "Innlandet" not in broken
    assert "Kongsberg" not in broken
    assert "Kongsbergregionen" not in broken
    assert "Lofoten" not in broken


def test_reports_naming_drift_without_treating_it_as_missing(live_report):
    drift = _tenders(live_report, tw.FINDING_NAMING_DRIFT)
    assert "Innlandet" in drift        # option "Innlandet" vs field "Digi Innlandet"
    assert "Kongsberg" in drift        # vs "Kongsbergregionen"
    for f in live_report.findings:
        if f.kind == tw.FINDING_NAMING_DRIFT:
            assert f.severity == "warning", "drift must never escalate to an alert"
            assert "Report only" in f.action


def test_flags_tender_missing_its_soft_commitment_option(live_report):
    """Bodø has 'S - Bodø' and 'B - Bodø' — so no T option. This is exactly the
    soft/hard gap raised in #support-productboard on 2026-08-28."""
    incomplete = _tenders(live_report, tw.FINDING_INCOMPLETE_PAIR)
    assert "Bodø" in incomplete


def test_missing_field_findings_carry_the_ui_click_path(live_report):
    for f in live_report.findings:
        if f.kind == tw.FINDING_MISSING_FIELD:
            assert f.severity == "alert"
            assert "board" in f.action.lower()
            assert "filter" in f.action.lower()


def test_watchdog_never_writes_to_productboard(live_report, monkeypatch):
    """Structural guarantee: the module must not reference any write path."""
    src = (tw.__file__)
    text = open(src, encoding="utf-8").read()
    for forbidden in ("PATCH", "POST", "entity_operations", "manage_field_options"):
        assert forbidden not in text.replace("no API for this", ""), forbidden


# ─── inconclusive is not "all clear" ──────────────────────────────────────────

def test_missing_tender_field_is_inconclusive_not_ok():
    report = tw.build_report(feature_fields=LIVE_FEATURE_FIELDS, tender_options=None)
    assert report.inconclusive
    assert not report.ok
    assert report.findings == []


def test_clean_workspace_reports_ok():
    report = tw.build_report(
        feature_fields=[
            {"name": "Voss - Committed deadline", "fieldType": "DATE"},
            {"name": "Tender requirements", "fieldType": "MULTI_SELECT"},
        ],
        tender_options=[{"name": "T - Voss"}, {"name": "S - Voss"}],
    )
    assert report.ok, report.to_dict()


# ─── Slack rendering ──────────────────────────────────────────────────────────

def _capture(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(notify, "_post", lambda cfg, payload: sent.append(payload) or True)
    return sent


def test_slack_report_names_the_missing_tenders(cfg, monkeypatch):
    sent = _capture(monkeypatch)
    report = tw.build_report(
        feature_fields=LIVE_FEATURE_FIELDS, tender_options=LIVE_TENDER_OPTIONS,
    )
    notify.send_tender_watchdog_report(cfg, report, owner_mention="<@U123>")
    text = sent[0]["text"]
    assert "Hadsel" in text
    assert "<@U123>" in text
    assert "Tender Commitment board" in text


def test_slack_report_posts_all_clear_when_clean(cfg, monkeypatch):
    sent = _capture(monkeypatch)
    report = tw.build_report(
        feature_fields=[{"name": "Voss - Committed deadline", "fieldType": "DATE"}],
        tender_options=[{"name": "T - Voss"}, {"name": "S - Voss"}],
    )
    notify.send_tender_watchdog_report(cfg, report)
    assert "Every tender" in sent[0]["text"]


def test_slack_report_never_reads_as_clear_when_inconclusive(cfg, monkeypatch):
    sent = _capture(monkeypatch)
    report = tw.build_report(feature_fields=[], tender_options=None)
    notify.send_tender_watchdog_report(cfg, report)
    text = sent[0]["text"]
    assert "could not run" in text
    assert "all clear" in text
