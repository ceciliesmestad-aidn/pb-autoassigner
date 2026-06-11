"""Tests for the autopilot pipeline + circuit breakers.

Covers the manual→autopilot ramp:
  - Confidence threshold filters correctly (≥ threshold patched, < queued)
  - Leave-open suggestions (pm_email=None) never auto-assign
  - Per-PM cap queues overflow but lets the first N through
  - Total cap aborts the whole batch when something looks anomalous
  - Dry-run records audit rows but does not PATCH PB
  - autopilot_enabled=false makes the function a no-op (defense in depth —
    the CLI already gates this, but tests prevent silent regressions)

All tests use FakePBClient + FakeAnthropicClient — no real PB, no real model.
"""
from __future__ import annotations

import copy

from backend import classify as classify_mod
from backend import db, pipeline
from tests.conftest import SAMPLE_PB_NOTES


# Sample fixture has confidences:
#   revurdering    → kristin.shovick     0.95
#   legemiddelkurve→ sandra.otteraaen    0.90
#   mitt aidn      → erik.story          0.90
#   test           → (none, leave-open)  0.20
# With threshold 0.90, the first three are auto-assign candidates;
# the fourth never qualifies (no pm_email AND below threshold).


def _setup(tmp_config, fake_pb, fake_anthropic, monkeypatch):
    """Common setup: monkey-patch Anthropic, ingest, and classify."""
    monkeypatch.setattr(
        classify_mod,
        "anthropic",
        type("M", (), {"Anthropic": lambda **kwargs: fake_anthropic}),
    )
    conn = db.connect(tmp_config.db_path).__enter__()
    pipeline.ingest(conn, fake_pb)
    pipeline.classify_pending(conn, tmp_config)
    return conn


def test_autopilot_assigns_high_confidence_only(
    tmp_config, fake_pb, fake_anthropic, monkeypatch
):
    """≥0.90 should PATCH PB; <0.90 should stay queued; leave-opens never PATCH."""
    tmp_config.classifier.autopilot_min_confidence = 0.9
    tmp_config.classifier.autopilot_enabled = True

    conn = _setup(tmp_config, fake_pb, fake_anthropic, monkeypatch)

    stats = pipeline.auto_assign_high_confidence(conn, fake_pb, tmp_config)

    # 3 notes meet the threshold (0.95, 0.90, 0.90); 1 does not (0.20 leave-open).
    assert stats["candidates"] == 3
    assert stats["assigned"] == 3
    assert stats["errors"] == 0

    # PB received exactly the three high-confidence PATCHes.
    patched_uuids = {p[0] for p in fake_pb.patches}
    assert patched_uuids == {
        "pb-uuid-revurdering", "pb-uuid-kurve", "pb-uuid-mittaidn"
    }
    # The leave-open ("test") note was NOT patched.
    assert "pb-uuid-test" not in patched_uuids

    # Audit rows created with assigned_by='autopilot'.
    auto_rows = conn.execute(
        "SELECT pm_email, confidence FROM assignments WHERE assigned_by='autopilot'"
    ).fetchall()
    assert len(auto_rows) == 3
    assert all(r["confidence"] >= 0.9 for r in auto_rows)


def test_autopilot_skips_leave_open_even_above_threshold(
    tmp_config, fake_pb, monkeypatch
):
    """A suggestion with pm_email=None must never auto-assign, even if
    confidence somehow lands above the threshold (defensive)."""
    # Custom anthropic rules where the "test" note returns pm_email=None
    # at high confidence — pathological but we want to be safe.
    from tests.conftest import FakeAnthropicClient
    fake_a = FakeAnthropicClient(rules={
        # No keyword matches → fallthrough to the high-conf-but-no-PM branch.
    })
    # Override the fallback so it returns high confidence with pm_email=None.
    original_create = fake_a.create
    def custom_create(**kw):
        resp = original_create(**kw)
        for block in resp.content:
            if block.name == "classify_notes":
                for c in block.input["classifications"]:
                    c["pm_email"] = None
                    c["confidence"] = 0.99  # tries to look "high confidence"
        return resp
    fake_a.create = custom_create

    tmp_config.classifier.autopilot_min_confidence = 0.9
    tmp_config.classifier.autopilot_enabled = True

    conn = _setup(tmp_config, fake_pb, fake_a, monkeypatch)
    stats = pipeline.auto_assign_high_confidence(conn, fake_pb, tmp_config)

    # All four notes had pm_email=None → none should auto-assign.
    assert stats["candidates"] == 0
    assert stats["assigned"] == 0
    assert fake_pb.patches == []


def test_autopilot_per_pm_cap_queues_overflow(
    tmp_config, fake_pb, monkeypatch
):
    """If a single run wants to assign N notes to one PM and N > per_pm_cap,
    the first cap go through and the rest stay queued for review."""
    # Build 5 notes that would all route to Sandra (legemiddelkurve keyword).
    flood = []
    for i in range(5):
        n = copy.deepcopy(SAMPLE_PB_NOTES[1])  # the kurve template → Sandra @ 0.9
        n["id"] = f"pb-flood-{i}"
        n["title"] = f"Legemiddelkurve sak {i}"
        flood.append(n)

    from tests.conftest import FakePBClient, FakeAnthropicClient
    pb = FakePBClient(notes=flood)
    a = FakeAnthropicClient()

    tmp_config.classifier.autopilot_min_confidence = 0.9
    tmp_config.classifier.autopilot_enabled = True
    tmp_config.classifier.autopilot_per_pm_cap = 2  # tight cap to force overflow
    tmp_config.classifier.autopilot_total_cap = 100  # not the cap under test

    conn = _setup(tmp_config, pb, a, monkeypatch)
    stats = pipeline.auto_assign_high_confidence(conn, pb, tmp_config)

    assert stats["candidates"] == 5
    assert stats["assigned"] == 2  # cap of 2 took effect
    assert stats["queued_overflow_per_pm"] == 3
    assert len(pb.patches) == 2  # only 2 PATCHes hit PB


def test_autopilot_total_cap_aborts_whole_batch(
    tmp_config, fake_pb, monkeypatch
):
    """If the run wants to auto-assign more than total_cap notes overall,
    nothing is patched. This is a 'something is very wrong' tripwire."""
    # 4 high-confidence notes from the default sample.
    tmp_config.classifier.autopilot_min_confidence = 0.85   # let all 3 named ones in
    tmp_config.classifier.autopilot_enabled = True
    tmp_config.classifier.autopilot_per_pm_cap = 100
    tmp_config.classifier.autopilot_total_cap = 2  # below candidate count

    from tests.conftest import FakeAnthropicClient
    a = FakeAnthropicClient()
    conn = _setup(tmp_config, fake_pb, a, monkeypatch)
    stats = pipeline.auto_assign_high_confidence(conn, fake_pb, tmp_config)

    # 3 candidates exceed total_cap=2 → entire batch held back.
    assert stats["candidates"] == 3
    assert stats["assigned"] == 0
    assert stats["queued_total_cap_exceeded"] == 3
    assert fake_pb.patches == []


def test_autopilot_dry_run_records_but_does_not_patch(
    tmp_config, fake_pb, fake_anthropic, monkeypatch
):
    """Dry-run records audit rows so the Recent Autopilot tab shows what
    WOULD have happened, but no PATCH actually fires."""
    tmp_config.classifier.autopilot_min_confidence = 0.9
    tmp_config.classifier.autopilot_enabled = True

    conn = _setup(tmp_config, fake_pb, fake_anthropic, monkeypatch)
    stats = pipeline.auto_assign_high_confidence(conn, fake_pb, tmp_config, dry_run=True)

    assert stats["dry_run"] is True
    assert stats["assigned"] == 3  # logical decisions, not real PATCHes
    assert fake_pb.patches == []   # crucial — PB was NOT touched

    # Audit rows are present and clearly marked as dry-run.
    rows = conn.execute(
        "SELECT pb_status, pb_error FROM assignments WHERE assigned_by='autopilot'"
    ).fetchall()
    assert len(rows) == 3
    assert all(r["pb_status"] is None for r in rows)
    assert all("DRY-RUN" in (r["pb_error"] or "") for r in rows)


def test_recent_autopilot_query_returns_assignments_with_context(
    tmp_config, fake_pb, fake_anthropic, monkeypatch
):
    """db.recent_autopilot_assignments joins notes for title + reasoning so
    the UI can render the Recent Autopilot tab without extra round-trips."""
    tmp_config.classifier.autopilot_min_confidence = 0.9
    tmp_config.classifier.autopilot_enabled = True

    conn = _setup(tmp_config, fake_pb, fake_anthropic, monkeypatch)
    pipeline.auto_assign_high_confidence(conn, fake_pb, tmp_config)

    rows = db.recent_autopilot_assignments(conn, hours=24)
    assert len(rows) == 3
    # All rows include title + reasoning + confidence — needed for the UI.
    for r in rows:
        assert r["title"]
        assert r["reasoning"]
        assert r["confidence"] >= 0.9
        assert r["pm_email"]


def test_autopilot_no_candidates_returns_empty_stats(
    tmp_config, fake_pb, fake_anthropic, monkeypatch
):
    """If no suggestions clear the threshold, autopilot is a no-op (no errors)."""
    # Set threshold higher than any rule produces (max is 0.95).
    tmp_config.classifier.autopilot_min_confidence = 0.99
    tmp_config.classifier.autopilot_enabled = True

    conn = _setup(tmp_config, fake_pb, fake_anthropic, monkeypatch)
    stats = pipeline.auto_assign_high_confidence(conn, fake_pb, tmp_config)

    assert stats["candidates"] == 0
    assert stats["assigned"] == 0
    assert fake_pb.patches == []
