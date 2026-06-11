"""End-to-end smoke test: ingest → classify → assign → train, all offline."""
from __future__ import annotations

import json

from backend import classify as classify_mod
from backend import db, pipeline, scopes_loader, train


def test_scopes_load(tmp_config):
    scopes = scopes_loader.load_all(tmp_config.scopes_dir)
    assert len(scopes.per_pm) >= 10, "expected a scope YAML for every PM"
    assert scopes.global_yaml, "expected a _global.yaml"
    assert scopes.combined_hash


def test_ingest_inserts_notes(tmp_config, fake_pb):
    with db.connect(tmp_config.db_path) as conn:
        stats = pipeline.ingest(conn, fake_pb)
        assert stats["inserted"] == 4
        rows = conn.execute("SELECT title, state FROM notes").fetchall()
        assert len(rows) == 4
        assert all(r["state"] == "new" for r in rows)


def test_ingest_is_idempotent(tmp_config, fake_pb):
    with db.connect(tmp_config.db_path) as conn:
        pipeline.ingest(conn, fake_pb)
        stats = pipeline.ingest(conn, fake_pb)
        assert stats["inserted"] == 0
        assert stats["updated_existing"] == 4


def test_classify_assigns_pms(tmp_config, fake_pb, fake_anthropic, monkeypatch):
    # Patch the Classifier to use our fake Anthropic client instead of hitting the API.
    # Accept **kwargs so we don't care which construction kwargs classify.py adds
    # (timeout, http_client, …) — the fake ignores them all.
    monkeypatch.setattr(
        classify_mod,
        "anthropic",
        type("M", (), {"Anthropic": lambda **kwargs: fake_anthropic}),
    )

    with db.connect(tmp_config.db_path) as conn:
        pipeline.ingest(conn, fake_pb)
        stats = pipeline.classify_pending(conn, tmp_config)

        assert stats["classified"] == 4

        # Inspect the suggestions.
        by_title = {
            r["title"]: dict(r)
            for r in conn.execute(
                """
                SELECT n.title, s.pm_email, s.confidence, s.reasoning
                  FROM notes n
                  JOIN suggestions s ON s.note_id = n.id
                 ORDER BY s.id DESC
                """
            )
        }
        assert by_title["Revurdering av tjenester fungerer ikke"]["pm_email"] == "kristin.shovick@aidn.no"
        assert by_title["Legemiddelkurve — dosering mangler"]["pm_email"] == "sandra.otteraaen@aidn.no"
        assert by_title["Mitt Aidn timebestilling feiler"]["pm_email"] == "erik.story@aidn.no"
        assert by_title["test"]["pm_email"] is None  # low-confidence fallback


def test_assign_records_audit_and_updates_state(tmp_config, fake_pb, fake_anthropic, monkeypatch):
    monkeypatch.setattr(
        classify_mod,
        "anthropic",
        type("M", (), {"Anthropic": lambda **kwargs: fake_anthropic}),
    )

    with db.connect(tmp_config.db_path) as conn:
        pipeline.ingest(conn, fake_pb)
        pipeline.classify_pending(conn, tmp_config)

        revurdering = conn.execute(
            "SELECT id, pb_uuid FROM notes WHERE title LIKE 'Revurdering%'"
        ).fetchone()

        result = pipeline.assign_note(
            conn, fake_pb, revurdering["id"], "kristin.shovick@aidn.no"
        )
        assert result["pb_status"] == 201
        assert result["was_override"] is False
        assert fake_pb.patches == [(revurdering["pb_uuid"], "kristin.shovick@aidn.no")]

        state = conn.execute(
            "SELECT state FROM notes WHERE id = ?", (revurdering["id"],)
        ).fetchone()["state"]
        assert state == "assigned"

        audit = conn.execute(
            "SELECT pm_email, suggested_pm, was_override, pb_status FROM assignments"
        ).fetchone()
        assert audit["pm_email"] == "kristin.shovick@aidn.no"
        assert audit["suggested_pm"] == "kristin.shovick@aidn.no"
        assert audit["was_override"] == 0
        assert audit["pb_status"] == 201


def test_assign_with_override_flags_audit(tmp_config, fake_pb, fake_anthropic, monkeypatch):
    monkeypatch.setattr(
        classify_mod,
        "anthropic",
        type("M", (), {"Anthropic": lambda **kwargs: fake_anthropic}),
    )
    with db.connect(tmp_config.db_path) as conn:
        pipeline.ingest(conn, fake_pb)
        pipeline.classify_pending(conn, tmp_config)

        kurve = conn.execute(
            "SELECT id FROM notes WHERE title LIKE 'Legemiddelkurve%'"
        ).fetchone()
        # Suggested was Sandra; user overrides to Line.
        result = pipeline.assign_note(conn, fake_pb, kurve["id"], "line.adde@aidn.no")
        assert result["was_override"] is True
        audit = conn.execute(
            "SELECT was_override FROM assignments WHERE note_id = ?", (kurve["id"],)
        ).fetchone()
        assert audit["was_override"] == 1


def test_train_proposes_and_applies(tmp_config, fake_pb, fake_anthropic, monkeypatch, tmp_path):
    monkeypatch.setattr(
        classify_mod,
        "anthropic",
        type("M", (), {"Anthropic": lambda **kwargs: fake_anthropic}),
    )
    monkeypatch.setattr(
        train,
        "anthropic",
        type("M", (), {"Anthropic": lambda **kwargs: fake_anthropic}),
    )

    # Redirect scopes to a tmp copy so the test doesn't overwrite repo files.
    import shutil
    scopes_tmp = tmp_path / "scopes"
    shutil.copytree(tmp_config.scopes_dir, scopes_tmp)
    tmp_config.storage.scopes_dir = str(scopes_tmp)

    with db.connect(tmp_config.db_path) as conn:
        pipeline.ingest(conn, fake_pb)
        pipeline.classify_pending(conn, tmp_config)

        # Assign each note to its suggested PM so we have training data.
        for row in conn.execute(
            "SELECT n.id, s.pm_email FROM notes n "
            "JOIN (SELECT note_id, MAX(id) AS mid FROM suggestions GROUP BY note_id) x "
            "ON x.note_id = n.id JOIN suggestions s ON s.id = x.mid "
            "WHERE s.pm_email IS NOT NULL"
        ).fetchall():
            pipeline.assign_note(conn, fake_pb, row["id"], row["pm_email"])

        proposals = train.propose_scope_updates(
            conn, tmp_config.anthropic, tmp_config.training, tmp_config.scopes_dir
        )
        assert proposals, "expected at least one proposal"

        # Apply the first one and confirm file + history got written.
        p = proposals[0]
        train.apply_update(conn, tmp_config.scopes_dir, p)

        new_text = scopes_loader.read_scope(tmp_config.scopes_dir, p.pm_email)
        assert "updated by fake model" in new_text

        history = db.training_history(conn, p.pm_email)
        assert history and history[0]["source"] == "training"


def test_dashboard_stats_shape(tmp_config, fake_pb, fake_anthropic, monkeypatch):
    monkeypatch.setattr(
        classify_mod,
        "anthropic",
        type("M", (), {"Anthropic": lambda **kwargs: fake_anthropic}),
    )
    with db.connect(tmp_config.db_path) as conn:
        pipeline.ingest(conn, fake_pb)
        pipeline.classify_pending(conn, tmp_config)
        stats = db.dashboard_stats(conn)
        assert "notes_by_state" in stats
        assert stats["notes_by_state"]["suggested"] == 4
        assert "confidence_distribution" in stats
        assert sum(stats["confidence_distribution"].values()) == 4
