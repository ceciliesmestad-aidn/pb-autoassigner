"""Confirmation-email drafts — all offline, nothing is ever sent."""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

from backend import acknowledge, db
from backend.config import Config


def _raw(user_id: str | None) -> dict:
    rel = ([{"type": "customer", "target": {"type": "user", "id": user_id}}]
           if user_id else [])
    return {"id": "x", "fields": {}, "relationships": {"data": rel}}


class FakeEntityClient:
    """Answers GET /v2/entities/{id} from a dict; records every call."""

    def __init__(self, emails: dict[str, str]):
        self.emails = emails
        self.calls: list[tuple[str, str]] = []

    def _request(self, method, path, **_):
        self.calls.append((method, path))
        uid = path.rsplit("/", 1)[-1]
        if uid not in self.emails:
            raise RuntimeError("404")
        return 200, {"data": {"id": uid, "fields": {"email": self.emails[uid]}}}


class FakeSummarizer:
    model = "fake-haiku"

    def __init__(self):
        self.seen: list[str] = []

    def summarize(self, notes):
        self.seen.extend(n["note_id"] for n in notes)
        return {n["note_id"]: f"Du foreslår {n['title'].lower()}." for n in notes}


def _seed(conn, uuid, title, content, user_id, *, created="2026-10-28T08:00:00Z",
          assigned=True):
    flat = {"pb_uuid": uuid, "content_hash": f"h-{content}", "title": title,
            "content": content, "tags": [], "company": "", "source": "",
            "display_url": "", "pb_created_at": created, "raw": _raw(user_id)}
    note_id, _ = db.upsert_note(conn, flat)
    if assigned:
        db.record_assignment(conn, note_id=note_id, pm_email="pm@aidn.no",
                             suggested_pm="pm@aidn.no", confidence=0.9,
                             assigned_by="autopilot", pb_status=200, pb_error=None)
        db.set_note_state(conn, note_id, "assigned")
    return note_id


# ─── unit ────────────────────────────────────────────────────────────────────

def test_kommune_filter():
    assert acknowledge.is_kommune_email("a.b@bodo.kommune.no")
    assert acknowledge.is_kommune_email("A@Kongsberg.Kommune.NO")
    assert not acknowledge.is_kommune_email("someone@aidn.no")
    assert not acknowledge.is_kommune_email("x@gmail.com")
    assert not acknowledge.is_kommune_email("x@fakekommune.no")
    assert not acknowledge.is_kommune_email("")


def test_customer_user_id_shapes():
    assert acknowledge.customer_user_id(_raw("u1")) == "u1"
    assert acknowledge.customer_user_id(_raw(None)) == ""
    company_only = {"relationships": {"data": [
        {"type": "customer", "target": {"type": "company", "id": "c1"}}]}}
    assert acknowledge.customer_user_id(company_only) == ""


def test_email_from_entity_variants():
    f = acknowledge._email_from_entity
    assert f({"data": {"fields": {"email": "A@x.kommune.no"}}}) == "a@x.kommune.no"
    assert f({"fields": {"email": "a@x.no"}}) == "a@x.no"
    assert f({"data": {"email": "a@x.no"}}) == "a@x.no"
    assert f({"data": {"fields": {}}}) == ""
    assert f(None) == ""


def test_template_has_no_team_name_and_no_reply_note():
    body = acknowledge.render_body("Du foreslår noe.")
    assert "Du foreslår noe." in body
    assert "produktteamet som har ansvar for dette området" in body
    assert "Obs: Du kan ikke svare på denne e-posten." in body
    assert "Team " not in body
    assert acknowledge.render_subject("Kalender") == \
        "Vi har mottatt forbedringsforslaget ditt: «Kalender»"


def test_mask_email():
    assert acknowledge.mask_email("kate.m@kongsberg.kommune.no") == "k***@kongsberg.kommune.no"


# ─── orchestration ───────────────────────────────────────────────────────────

def test_draft_flow_filters_dedupes_and_is_idempotent(tmp_path):
    cfg = Config()
    cfg.acknowledge.since = "2026-10-28"
    db.init_db(tmp_path / "t.db")
    client = FakeEntityClient({
        "u-kommune": "lege@oygarden.kommune.no",
        "u-aidn": "csm@aidn.no",
    })
    summ = FakeSummarizer()
    with db.connect(tmp_path / "t.db") as conn:
        with db.transaction(conn):
            ok = _seed(conn, "n1", "Sortering av legemidler", "ATC", "u-kommune")
            dup = _seed(conn, "n2", "Sortering av legemidler", "ATC", "u-kommune")
            _seed(conn, "n3", "Intern", "x", "u-aidn")
            _seed(conn, "n4", "Uten avsender", "y", None)
            _seed(conn, "n5", "Ukjent bruker", "z", "u-missing")
            _seed(conn, "n6", "Gammel", "old", "u-kommune", created="2026-09-01T00:00:00Z")
            _seed(conn, "n7", "Ikke tildelt", "open", "u-kommune", assigned=False)

        stats = acknowledge.draft_acknowledgments(conn, client, cfg, summarizer=summ)
        assert stats["considered"] == 5          # n6 too old, n7 not assigned
        assert stats["drafted"] == 1
        assert stats["skipped"] == {"duplicate": 1, "not_kommune": 1,
                                    "no_customer": 1, "lookup_failed": 1}
        assert summ.seen == [str(ok)]            # only qualifying notes hit the model

        drafts = conn.execute("SELECT * FROM ack_drafts WHERE status='draft'").fetchall()
        assert len(drafts) == 1 and drafts[0]["note_id"] == ok
        assert drafts[0]["recipient_email"] == "lege@oygarden.kommune.no"
        # Non-kommune addresses are never stored, only the domain.
        aidn = conn.execute("SELECT * FROM ack_drafts WHERE skip_reason='not_kommune'").fetchone()
        assert aidn["recipient_email"] == "" and aidn["recipient_domain"] == "aidn.no"
        assert conn.execute("SELECT 1 FROM ack_drafts WHERE note_id=?", (dup,)).fetchone()

        # Second run: nothing new.
        again = acknowledge.draft_acknowledgments(conn, client, cfg, summarizer=summ)
        assert again["considered"] == 0

        html = acknowledge.render_preview_html(
            [d for d in db.list_ack_drafts(conn) if d["status"] == "draft"],
            [d for d in db.list_ack_drafts(conn) if d["status"] == "skipped"])
        assert "l***@oygarden.kommune.no" in html
        assert "lege@oygarden.kommune.no" not in html


def test_missing_summary_is_retried_next_run(tmp_path):
    cfg = Config()
    db.init_db(tmp_path / "t.db")
    client = FakeEntityClient({"u": "a@bodo.kommune.no"})

    class Empty(FakeSummarizer):
        def summarize(self, notes):
            return {}

    with db.connect(tmp_path / "t.db") as conn:
        with db.transaction(conn):
            _seed(conn, "n1", "Kalender", "k", "u")
        s1 = acknowledge.draft_acknowledgments(conn, client, cfg, summarizer=Empty())
        assert s1["drafted"] == 0 and s1["skipped"] == {"no_summary": 1}
        s2 = acknowledge.draft_acknowledgments(conn, client, cfg, summarizer=FakeSummarizer())
        assert s2["drafted"] == 1


def test_is_duplicate_rules():
    prior = [("Interaksjoner", "Det er fint med varsel blå/gul/rød på interaksjoner.")]
    # Identical after normalising (spacing, punctuation, case).
    assert acknowledge.is_duplicate("interaksjoner ", "Det er fint med varsel  blå gul rød på interaksjoner",
                                    prior)
    # Same text, new title.
    assert acknowledge.is_duplicate("Varsel", "Det er fint med varsel blå/gul/rød på interaksjoner.", prior)
    # Same title, small edit on resubmission.
    assert acknowledge.is_duplicate("Interaksjoner", "Det er fint med varsel blå/gul/rød på interaksjonene.",
                                    prior)
    # Same generic title, different suggestion → not a duplicate.
    assert not acknowledge.is_duplicate("Interaksjoner", "Ønsker filter på legemiddeltype i listen.", prior)
    assert not acknowledge.is_duplicate("Kalender", "Noe helt annet.", [])


def test_resubmission_across_runs_gets_no_second_draft(tmp_path):
    cfg = Config()
    db.init_db(tmp_path / "t.db")
    client = FakeEntityClient({"u": "a@bodo.kommune.no"})
    with db.connect(tmp_path / "t.db") as conn:
        with db.transaction(conn):
            _seed(conn, "n1", "Kalender", "Velg alle ansatte på avdelingen.", "u")
        assert acknowledge.draft_acknowledgments(
            conn, client, cfg, summarizer=FakeSummarizer())["drafted"] == 1
        # Next day: same person sends it again with tiny differences.
        with db.transaction(conn):
            _seed(conn, "n2", "Kalender ", "Velg alle ansatte på avdelingen!", "u")
        s = acknowledge.draft_acknowledgments(conn, client, cfg, summarizer=FakeSummarizer())
        assert s["drafted"] == 0 and s["skipped"] == {"duplicate": 1}


def test_ack_is_off_by_default():
    assert Config().acknowledge.enabled is False


def test_acknowledge_module_cannot_send():
    """Structural guard: during the test phase the module must not contain any
    outbound write or mail path. PB access is GET-only."""
    src = Path(acknowledge.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "_request":
            method = node.args[0]
            assert isinstance(method, ast.Constant) and method.value == "GET"
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
            for n in names:
                assert not any(bad in n for bad in ("smtplib", "zendesk", "email.mime"))
    for forbidden in ("assign(", "add_tags(", "PATCH", "POST"):
        assert forbidden not in src
