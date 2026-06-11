"""Tests for the Slack notifier (backend/notify.py)."""
from __future__ import annotations

import json

import pytest

from backend import notify
from backend.config import Config


@pytest.fixture
def cfg() -> Config:
    c = Config()
    c.slack.webhook_url = "https://hooks.slack.com/services/T000/B000/XXX"
    c.slack.enabled = True
    c.slack.ssl_verify = True
    return c


@pytest.fixture
def captured(monkeypatch):
    """Capture the payload instead of hitting the network."""
    sent: list[dict] = []

    def fake_post(cfg, payload):
        sent.append(payload)
        return True

    monkeypatch.setattr(notify, "_post", fake_post)
    return sent


def test_no_webhook_is_silent_noop():
    cfg = Config()  # webhook_url empty
    assert notify.send_failure(cfg, "boom") is False


def test_digest_lists_assignments_and_needs_review(cfg, captured):
    ok = notify.send_run_report(
        cfg,
        ingest_stats={"inserted": 5},
        classify_stats={"classified": 5},
        autopilot_stats={
            "assigned": 3,
            "per_pm": {"line.adde@aidn.no": 2, "jens.malm@aidn.no": 1},
            "dry_run": False,
            "errors": 0,
            "queued_overflow_per_pm": 0,
            "queued_total_cap_exceeded": 0,
        },
        needs_review=[
            {
                "title": "Uklart notat om integrasjon",
                "display_url": "https://aidn.productboard.com/notes/abc",
                "suggested_pm": "abraham.guzman@aidn.no",
                "confidence": 0.41,
            },
            {
                "title": "Leave-open notat",
                "display_url": "",
                "suggested_pm": None,
                "confidence": None,
            },
        ],
    )
    assert ok is True
    text = captured[0]["text"]
    assert "Tildelte *3* notater automatisk" in text
    assert "Line Adde: 2" in text
    assert "*2 notat(er) trenger et menneske*" in text
    assert "<https://aidn.productboard.com/notes/abc|Uklart notat om integrasjon>" in text
    assert "0.41" in text
    assert "ingen klar eier" in text
    assert "DRY-RUN" not in text


def test_dry_run_is_labelled(cfg, captured):
    notify.send_run_report(
        cfg,
        ingest_stats={"inserted": 1},
        classify_stats={"classified": 1},
        autopilot_stats={"assigned": 1, "per_pm": {"x@aidn.no": 1}, "dry_run": True},
        needs_review=[],
    )
    assert "DRY-RUN" in captured[0]["text"]


def test_quiet_day_message(cfg, captured):
    notify.send_run_report(
        cfg,
        ingest_stats={"inserted": 0},
        classify_stats={"classified": 0},
        autopilot_stats={"assigned": 0, "per_pm": {}, "dry_run": False},
        needs_review=[],
    )
    assert "Ingen nye notater" in captured[0]["text"]


def test_cap_tripwires_alert(cfg, captured):
    notify.send_run_report(
        cfg,
        ingest_stats={"inserted": 300},
        classify_stats={"classified": 300},
        autopilot_stats={
            "assigned": 0,
            "per_pm": {},
            "dry_run": False,
            "queued_total_cap_exceeded": 250,
            "queued_overflow_per_pm": 0,
            "errors": 0,
        },
        needs_review=[],
    )
    assert "Total-grensen ble utløst" in captured[0]["text"]


def test_needs_review_truncates(cfg, captured):
    many = [
        {"title": f"Notat {i}", "display_url": "", "suggested_pm": "a@aidn.no", "confidence": 0.5}
        for i in range(20)
    ]
    notify.send_run_report(
        cfg,
        ingest_stats={"inserted": 20},
        classify_stats={"classified": 20},
        autopilot_stats={"assigned": 0, "per_pm": {}, "dry_run": False},
        needs_review=many,
    )
    text = captured[0]["text"]
    assert "og 5 til" in text


def test_failure_message(cfg, captured):
    notify.send_failure(cfg, "RuntimeError: kaboom")
    text = captured[0]["text"]
    assert "FEILET" in text
    assert "kaboom" in text


def test_post_payload_is_valid_json(cfg, monkeypatch):
    """_post should send well-formed JSON with a text key."""
    seen = {}

    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None, context=None):
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResp()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    assert notify._post(cfg, {"text": "hei"}) is True
    assert seen["body"] == {"text": "hei"}
