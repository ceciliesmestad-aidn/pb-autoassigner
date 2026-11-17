"""Mitt Aidn tag rule (2026-11-17): tagged notes → Fredrik, unless scheduling → normal routing (William)."""
from __future__ import annotations

import copy

from backend import classify as classify_mod
from backend import db, pipeline, tag_rules
from tests.conftest import FakePBClient

FREDRIK = "fredrik.pedersen@aidn.no"


def test_tagged_app_feedback_goes_to_fredrik():
    assert tag_rules.mitt_aidn_route("Appen henger", "Kan ikke logge inn", ["Mitt Aidn"]) == FREDRIK
    assert tag_rules.mitt_aidn_route("x", "y", ["mitt aidn ", "feedback"]) == FREDRIK


def test_untagged_note_is_not_caught():
    assert tag_rules.mitt_aidn_route("Mitt Aidn appen henger", "", ["feedback"]) is None


def test_scheduling_note_is_left_to_normal_routing():
    for title, body in [
        ("Avtaler vises ikke", "Pasient ser ikke avtalen sin i appen"),
        ("Kalender", ""),
        ("Feil", "Får ikke bestille time"),
        ("Påminnelse", "Ønsker påminnelser på SMS"),
    ]:
        assert tag_rules.mitt_aidn_route(title, body, ["Mitt Aidn"]) is None, title


def test_application_note_goes_to_kristin():
    kristin = tag_rules.APPLICATION_PM
    assert tag_rules.mitt_aidn_route("Søknad - plassering", "", ["Mitt Aidn"]) == kristin
    assert tag_rules.mitt_aidn_route("Feil", "Får ikke sendt søknaden", ["Mitt Aidn"]) == kristin
    assert tag_rules.mitt_aidn_route("Søknad", "", ["feedback"]) is None  # untagged


def test_pipeline_routes_tagged_note_without_calling_ai(tmp_config, fake_anthropic, monkeypatch):
    notes = [
        {"id": "pb-app", "title": "Innlogging feiler", "content": "<p>BankID-feil i appen</p>",
         "tags": [{"name": "Mitt Aidn"}], "company": {"name": "X"}, "owner": None,
         "created_at": "2026-11-01T00:00:00Z", "display_url": ""},
        {"id": "pb-cal", "title": "Avtaler mangler", "content": "<p>Ser ikke avtalen min</p>",
         "tags": [{"name": "Mitt Aidn"}], "company": {"name": "X"}, "owner": None,
         "created_at": "2026-11-01T00:00:00Z", "display_url": ""},
    ]
    pb = FakePBClient(copy.deepcopy(notes))
    monkeypatch.setattr(classify_mod, "anthropic",
                        type("M", (), {"Anthropic": lambda **kw: fake_anthropic}))
    conn = db.connect(tmp_config.db_path).__enter__()
    pipeline.ingest(conn, pb)
    stats = pipeline.classify_pending(conn, tmp_config)
    assert stats["rule_routed"] == 1

    rows = {r["pb_uuid"]: r for r in conn.execute(
        "SELECT n.pb_uuid, s.pm_email, s.confidence, s.model FROM suggestions s "
        "JOIN notes n ON n.id = s.note_id").fetchall()}
    assert rows["pb-app"]["pm_email"] == FREDRIK
    assert rows["pb-app"]["confidence"] == 1.0
    assert rows["pb-app"]["model"] == tag_rules.RULE_MODEL
    # The calendar note went through the AI, not the rule.
    assert rows["pb-cal"]["model"] != tag_rules.RULE_MODEL
