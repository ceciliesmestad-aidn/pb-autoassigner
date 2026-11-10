"""PB → Zendesk transfer (backend/zendesk_transfer.py, backend/zendesk_client.py).

Fully offline: FakeTransferPB + FakeZendesk stand in for both APIs, and the
two real clients are only exercised with their single HTTP entry point stubbed.
No live ticket is ever created.
"""
from __future__ import annotations

import copy
import json
import logging
import urllib.error

import pytest

from backend import db, notify, pipeline, zendesk_transfer
from backend.config import Config, StorageConfig, ZendeskConfig
from backend.pb_client import PBClient, PBError
from backend.zendesk_client import ZendeskClient, ZendeskError

from .conftest import FakePBClient

OWNER = "torstein.bore@aidn.no"
SECRET_TOKEN = "zd-secret-token-123"
REQ_EMAIL = "kari.nordmann@bodo.kommune.no"
TITLE = "Får ikke logget inn i Aidn"
BODY_HTML = "<p>Hei!</p><p>Vi får <strong>feilmelding</strong> ved innlogging.</p>"


def v2_note(uid, *, tags=("send-to-zendesk",), owner=None, processed=False,
            archived=False, user_id="user-1", title=TITLE, content=BODY_HTML):
    rel = ([{"type": "customer", "target": {"type": "user", "id": user_id}}]
           if user_id else [])
    return {
        "id": uid,
        "createdAt": "2026-11-05T08:00:00Z",
        "fields": {"name": title, "content": content,
                   "tags": [{"name": t} for t in tags],
                   "owner": {"email": owner} if owner else None,
                   "processed": processed, "archived": archived},
        "links": {"html": f"https://aidn.productboard.com/insights/feedback?d=notes%2F{uid}"},
        "relationships": {"data": rel},
    }


class FakeTransferPB(FakePBClient):
    """FakePBClient + the v2 bits the transfer needs."""

    def __init__(self, notes, *, members=(OWNER,), users=None):
        super().__init__(notes)
        self.api_version = "v2"
        self.workspace = "aidn"
        self.members = set(members)
        self.users = users if users is not None else {"user-1": (REQ_EMAIL, "Kari Nordmann"),
                                                      "user-2": ("ola@askoy.kommune.no", "Ola")}
        self.mark_calls: list[str] = []
        self.fail_mark: set[str] = set()

    def member_exists(self, email):
        return email in self.members

    def _request(self, method, path, **kw):
        assert method == "GET", "transfer must only GET via _request"
        uid = path.rsplit("/", 1)[-1]
        if uid not in self.users:
            raise PBError(404, "", url=path)
        email, name = self.users[uid]
        return 200, {"data": {"fields": {"email": email, "name": name}}}

    def mark_sent_to_zendesk(self, note_uuid, *, owner_email, add_tag, remove_tag):
        self.mark_calls.append(note_uuid)
        if note_uuid in self.fail_mark:
            raise PBError(500, "boom", url="")
        n = self.note(note_uuid)
        tags = [t["name"] for t in n["fields"]["tags"]]
        tags = [t for t in tags + [add_tag] if t != remove_tag]
        n["fields"]["tags"] = [{"name": t} for t in dict.fromkeys(tags)]
        n["fields"]["owner"] = {"email": owner_email}
        n["fields"]["processed"] = True
        return 200

    def note(self, uid):
        return next(n for n in self._notes if n["id"] == uid)

    def tags(self, uid):
        return [t["name"] for t in self.note(uid)["fields"]["tags"]]


class FakeZendesk:
    def __init__(self):
        self.tickets: list[dict] = []
        self.creates: list[dict] = []
        self.internal_notes: list[tuple[int, str]] = []
        # per external_id behaviour: "fail" | "timeout_created" | "timeout_lost"
        self.behaviour: dict[str, str] = {}
        self.lookup_fails = False

    def ticket_url(self, tid):
        return f"https://aidnsupport.zendesk.com/agent/tickets/{tid}"

    def find_by_external_id(self, ext):
        if self.lookup_fails:
            raise ZendeskError(None, "TimeoutError")
        hits = [t for t in self.tickets if t["external_id"] == ext]
        return hits[0] if hits else None

    def create_ticket(self, **kw):
        self.creates.append(kw)
        mode = self.behaviour.pop(kw["external_id"], "")
        if mode == "fail":
            raise ZendeskError(422, "RecordInvalid")
        ticket = {"id": 1000 + len(self.tickets), "external_id": kw["external_id"],
                  "created_at": "2026-11-05T08:00:00Z", **kw}
        if mode == "timeout_lost":
            raise ZendeskError(None, "TimeoutError", uncertain=True)
        self.tickets.append(ticket)
        if mode == "timeout_created":
            raise ZendeskError(None, "TimeoutError", uncertain=True)
        return ticket

    def add_internal_note(self, tid, body):
        self.internal_notes.append((tid, body))


@pytest.fixture
def zcfg(tmp_path):
    cfg = Config(storage=StorageConfig(db_path=str(tmp_path / "zd.db")),
                 zendesk=ZendeskConfig(enabled=True, pb_owner_email=OWNER,
                                       email="api@aidn.no", api_token=SECRET_TOKEN))
    db.init_db(cfg.db_path)
    return cfg


def run(cfg, pb, zd, **kw):
    with db.connect(cfg.db_path) as conn:
        return zendesk_transfer.run_transfers(conn, pb, cfg, zd=zd, **kw)


def row(cfg, uid):
    with db.connect(cfg.db_path) as conn:
        return db.zendesk_transfer(conn, uid)


# ─── the happy path ───────────────────────────────────────────────────────────

def test_tagged_note_creates_one_ticket_with_expected_fields(zcfg):
    pb, zd = FakeTransferPB([v2_note("n1")]), FakeZendesk()
    stats = run(zcfg, pb, zd)

    assert stats["transferred"] == 1 and len(zd.creates) == 1
    t = zd.creates[0]
    assert t["subject"] == TITLE
    assert t["body"] == "Hei!\nVi får feilmelding ved innlogging."
    assert t["requester_email"] == REQ_EMAIL and t["requester_name"] == "Kari Nordmann"
    assert t["external_id"] == "productboard:n1"
    assert t["tags"] == ["from_productboard"]
    # PB link only in the private note, never in the public comment.
    assert "productboard.com" not in t["body"]
    assert zd.internal_notes == [(1000, "Opprettet automatisk fra Productboard-notat. "
                                  "Kommentarer fra produktteamet ligger på det opprinnelige notatet: "
                                  "https://aidn.productboard.com/insights/feedback?d=notes%2Fn1")]


def test_success_updates_pb_owner_tags_and_processed(zcfg):
    pb, zd = FakeTransferPB([v2_note("n1")]), FakeZendesk()
    run(zcfg, pb, zd)
    f = pb.note("n1")["fields"]
    assert f["owner"] == {"email": OWNER} and f["processed"] is True
    assert pb.tags("n1") == ["sent-to-zendesk"]
    r = row(zcfg, "n1")
    assert r["status"] == "completed" and r["zendesk_ticket_id"] == 1000
    assert r["zendesk_ticket_url"].endswith("/agent/tickets/1000")
    assert r["internal_note_added"] == 1 and r["completed_at"]


def test_assigned_and_processed_notes_found_untagged_ignored(zcfg):
    pb = FakeTransferPB([
        v2_note("assigned", owner="line.adde@aidn.no", processed=True),
        v2_note("plain", tags=("feedback",)),
        v2_note("archived", archived=True),
        v2_note("upper", tags=("Send-To-Zendesk",)),
    ])
    zd = FakeZendesk()
    stats = run(zcfg, pb, zd)
    assert stats["found"] == 2
    assert {c["external_id"] for c in zd.creates} == {"productboard:assigned",
                                                      "productboard:upper"}
    assert pb.note("plain")["fields"]["owner"] is None
    assert "plain" not in pb.mark_calls


# ─── failures leave the note actionable ───────────────────────────────────────

def test_zendesk_failure_leaves_pb_note_actionable(zcfg):
    pb, zd = FakeTransferPB([v2_note("n1")]), FakeZendesk()
    zd.behaviour["productboard:n1"] = "fail"
    stats = run(zcfg, pb, zd)
    assert stats["failed"] == 1 and pb.mark_calls == []
    assert pb.tags("n1") == ["send-to-zendesk"] and pb.note("n1")["fields"]["owner"] is None
    assert row(zcfg, "n1")["status"] == "failed"
    # Known-failed create → next run may try again, and succeeds.
    stats = run(zcfg, pb, zd)
    assert stats["transferred"] == 1 and len(zd.tickets) == 1


def test_repeated_runs_do_not_create_duplicates(zcfg):
    pb, zd = FakeTransferPB([v2_note("n1")]), FakeZendesk()
    run(zcfg, pb, zd)
    pb.note("n1")["fields"]["tags"].append({"name": "send-to-zendesk"})  # re-tagged by a human
    stats = run(zcfg, pb, zd)
    run(zcfg, pb, zd)
    assert len(zd.creates) == 1
    assert stats["already_transferred"] == 1


def test_uncertain_create_is_recovered_by_external_id_same_run(zcfg):
    pb, zd = FakeTransferPB([v2_note("n1")]), FakeZendesk()
    zd.behaviour["productboard:n1"] = "timeout_created"
    stats = run(zcfg, pb, zd)
    assert len(zd.creates) == 1 and len(zd.tickets) == 1
    assert stats["transferred"] == 1
    assert row(zcfg, "n1")["zendesk_ticket_id"] == 1000


def test_uncertain_create_not_found_stays_pending_then_lookup_first(zcfg):
    pb, zd = FakeTransferPB([v2_note("n1")]), FakeZendesk()
    zd.behaviour["productboard:n1"] = "timeout_lost"
    stats = run(zcfg, pb, zd)
    assert stats["failed"] == 1 and row(zcfg, "n1")["status"] == "pending"
    assert pb.tags("n1") == ["send-to-zendesk"]
    # Zendesk turns out to have saved it after all (late write).
    zd.tickets.append({"id": 77, "external_id": "productboard:n1", "created_at": "x"})
    stats = run(zcfg, pb, zd)
    assert len(zd.creates) == 1, "must look up, not create again"
    assert stats["completed_after_partial_failure"] == 1
    assert row(zcfg, "n1")["zendesk_ticket_id"] == 77


def test_lookup_failure_never_creates(zcfg):
    pb, zd = FakeTransferPB([v2_note("n1")]), FakeZendesk()
    zd.lookup_fails = True
    stats = run(zcfg, pb, zd)
    assert stats["failed"] == 1 and zd.creates == []


def test_existing_mapping_resumes_pb_updates_without_new_ticket(zcfg):
    pb, zd = FakeTransferPB([v2_note("n1")]), FakeZendesk()
    with db.connect(zcfg.db_path) as conn:
        db.upsert_zendesk_transfer(conn, "n1", status="ticket_created",
                                   zendesk_ticket_id=555, zendesk_ticket_url="u",
                                   internal_note_added=True)
    stats = run(zcfg, pb, zd)
    assert zd.creates == [] and zd.internal_notes == []
    assert stats["completed_after_partial_failure"] == 1
    assert pb.tags("n1") == ["sent-to-zendesk"]


def test_pb_update_failure_is_recoverable_next_run(zcfg):
    pb, zd = FakeTransferPB([v2_note("n1")]), FakeZendesk()
    pb.fail_mark.add("n1")
    stats = run(zcfg, pb, zd)
    assert stats["failed"] == 1
    r = row(zcfg, "n1")
    assert r["status"] == "ticket_created" and r["last_error"] == "pb update: HTTP 500"
    assert pb.tags("n1") == ["send-to-zendesk"]
    pb.fail_mark.clear()
    stats = run(zcfg, pb, zd)
    assert len(zd.creates) == 1
    assert stats["completed_after_partial_failure"] == 1
    assert row(zcfg, "n1")["status"] == "completed"


def test_lost_local_db_is_caught_by_external_id(zcfg, tmp_path):
    """The Actions cache holding the DB can be evicted."""
    pb, zd = FakeTransferPB([v2_note("n1")]), FakeZendesk()
    pb.fail_mark.add("n1")
    run(zcfg, pb, zd)                      # ticket made, PB update failed
    pb.fail_mark.clear()
    fresh = copy.deepcopy(zcfg)
    fresh.storage.db_path = str(tmp_path / "fresh.db")
    db.init_db(fresh.db_path)
    stats = run(fresh, pb, zd)
    assert len(zd.creates) == 1
    assert stats["completed_after_partial_failure"] == 1


def test_missing_requester_email_creates_no_ticket(zcfg):
    pb = FakeTransferPB([v2_note("nouser", user_id=""), v2_note("unknown", user_id="ghost")])
    zd = FakeZendesk()
    stats = run(zcfg, pb, zd)
    assert stats["needs_review"] == 2 and zd.creates == [] and pb.mark_calls == []
    assert pb.tags("nouser") == ["send-to-zendesk"]
    assert row(zcfg, "nouser")["status"] == "needs_review"


def test_failures_are_isolated_per_note(zcfg):
    pb = FakeTransferPB([v2_note("bad"), v2_note("good", user_id="user-2"),
                         v2_note("pbfail")])
    zd = FakeZendesk()
    zd.behaviour["productboard:bad"] = "fail"
    pb.fail_mark.add("pbfail")
    stats = run(zcfg, pb, zd)
    assert stats["transferred"] == 1 and stats["failed"] == 2
    assert pb.tags("good") == ["sent-to-zendesk"]


def test_unexpected_exception_is_isolated(zcfg, monkeypatch):
    pb, zd = FakeTransferPB([v2_note("a"), v2_note("b")]), FakeZendesk()
    real = zendesk_transfer.transfer_note

    def flaky(conn, pb_, zd_, cfg, raw, **kw):
        if raw["id"] == "a":
            raise RuntimeError("kaboom")
        return real(conn, pb_, zd_, cfg, raw, **kw)
    monkeypatch.setattr(zendesk_transfer, "transfer_note", flaky)
    stats = run(zcfg, pb, zd)
    assert stats["failed"] == 1 and stats["transferred"] == 1


# ─── guards ───────────────────────────────────────────────────────────────────

def test_unverified_owner_blocks_live_run(zcfg):
    pb, zd = FakeTransferPB([v2_note("n1")], members=()), FakeZendesk()
    stats = run(zcfg, pb, zd)
    assert "verified" in stats["blocked"] and zd.creates == [] and pb.mark_calls == []


def test_dry_run_writes_nothing(zcfg):
    pb, zd = FakeTransferPB([v2_note("n1"), v2_note("nouser", user_id="")]), FakeZendesk()
    stats = run(zcfg, pb, zd, dry_run=True)
    assert stats["would_transfer"] == 1 and stats["needs_review"] == 1
    assert zd.creates == [] and zd.internal_notes == [] and pb.mark_calls == []
    assert row(zcfg, "n1") is None and row(zcfg, "nouser") is None


def test_disabled_by_default():
    assert Config().zendesk.enabled is False


def test_ingest_leaves_tagged_notes_out_of_classification(zcfg):
    pb = FakeTransferPB([v2_note("tagged"), v2_note("normal", tags=())])
    with db.connect(zcfg.db_path) as conn:
        pipeline.ingest(conn, pb, exclude_tag="send-to-zendesk")
        uuids = {r["pb_uuid"] for r in conn.execute("SELECT pb_uuid FROM notes")}
    assert uuids == {"normal"}


def test_secrets_and_note_content_are_not_logged(zcfg, caplog):
    caplog.set_level(logging.DEBUG)
    pb = FakeTransferPB([v2_note("ok"), v2_note("bad"), v2_note("pbfail"),
                         v2_note("nouser", user_id="")])
    zd = FakeZendesk()
    zd.behaviour["productboard:bad"] = "fail"
    pb.fail_mark.add("pbfail")
    stats = run(zcfg, pb, zd)
    slack = "\n".join(notify.zendesk_lines(stats))
    with db.connect(zcfg.db_path) as conn:
        stored = json.dumps([dict(r) for r in conn.execute("SELECT * FROM zendesk_transfers")])
    for blob in (caplog.text, slack, stored):
        for secret in (SECRET_TOKEN, TITLE, "feilmelding", REQ_EMAIL, "Kari"):
            assert secret not in blob


# ─── the real clients, HTTP stubbed ───────────────────────────────────────────

def test_pb_mark_sent_body_shape(monkeypatch):
    c = PBClient(token="t", api_version="v2", patch_delay_seconds=0)
    seen = {}

    def fake(method, path, *, params=None, body=None):
        seen.update(method=method, path=path, body=body)
        return 200, {}
    monkeypatch.setattr(c, "_request", fake)
    c.mark_sent_to_zendesk("n1", owner_email=OWNER, add_tag="sent-to-zendesk",
                           remove_tag="send-to-zendesk")
    ops = seen["body"]["data"]["patch"]
    assert seen["method"] == "PATCH" and seen["path"] == "/v2/notes/n1"
    assert [o["op"] for o in ops] == ["addItems", "set", "set", "removeItems"]
    assert ops[1] == {"op": "set", "path": "owner", "value": {"email": OWNER}}
    assert ops[2] == {"op": "set", "path": "processed", "value": True}
    assert ops[3]["value"] == [{"name": "send-to-zendesk"}]


def test_pb_member_exists_pages(monkeypatch):
    c = PBClient(token="t", api_version="v2")
    pages = iter([
        (200, {"data": [{"fields": {"email": "a@aidn.no"}}], "links": {"next": "https://x/2"}}),
        (200, {"data": [{"email": "Torstein.Bore@aidn.no"}], "links": {}}),
    ])
    monkeypatch.setattr(c, "_request_url", lambda m, u, body=None: next(pages))
    monkeypatch.setattr("backend.pb_client.time.sleep", lambda s: None)
    assert c.member_exists(OWNER) is True


class _Resp:
    def __init__(self, payload, status=201):
        self._raw, self.status = json.dumps(payload).encode(), status

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_zendesk_client_create_request_and_auth(monkeypatch):
    calls = []

    def fake_urlopen(req, context=None, timeout=None):
        calls.append(req)
        return _Resp({"ticket": {"id": 42}})
    monkeypatch.setattr("backend.zendesk_client.urllib.request.urlopen", fake_urlopen)
    zc = ZendeskClient(base_url="https://aidnsupport.zendesk.com/", email="api@aidn.no",
                       api_token=SECRET_TOKEN)
    t = zc.create_ticket(subject="S", body="B", requester_email=REQ_EMAIL,
                         requester_name="Kari", external_id="productboard:n1",
                         tags=["from_productboard"], group_id=9)
    assert t["id"] == 42 and len(calls) == 1
    req = calls[0]
    assert req.full_url == "https://aidnsupport.zendesk.com/api/v2/tickets.json"
    assert req.get_header("Authorization").startswith("Basic ")
    sent = json.loads(req.data)["ticket"]
    assert sent["comment"] == {"body": "B", "public": True}
    assert sent["requester"] == {"email": REQ_EMAIL, "name": "Kari"}
    assert sent["external_id"] == "productboard:n1" and sent["group_id"] == 9
    assert zc.ticket_url(42) == "https://aidnsupport.zendesk.com/agent/tickets/42"


def test_zendesk_client_timeout_is_uncertain_and_not_retried(monkeypatch):
    calls = []

    def boom(req, context=None, timeout=None):
        calls.append(req)
        raise TimeoutError("slow")
    monkeypatch.setattr("backend.zendesk_client.urllib.request.urlopen", boom)
    zc = ZendeskClient(base_url="https://aidnsupport.zendesk.com", email="e", api_token="t")
    with pytest.raises(ZendeskError) as ei:
        zc.create_ticket(subject="S", body="B", requester_email="a@b.no", requester_name="a",
                         external_id="x", tags=[])
    assert ei.value.uncertain is True and len(calls) == 1


def test_zendesk_client_4xx_is_certain_and_carries_no_body(monkeypatch):
    def http_err(req, context=None, timeout=None):
        import io
        raise urllib.error.HTTPError(req.full_url, 422, "x", {},
                                     io.BytesIO(b'{"error":"RecordInvalid","details":"' +
                                                TITLE.encode() + b'"}'))
    monkeypatch.setattr("backend.zendesk_client.urllib.request.urlopen", http_err)
    zc = ZendeskClient(base_url="https://aidnsupport.zendesk.com", email="e", api_token="t")
    with pytest.raises(ZendeskError) as ei:
        zc.create_ticket(subject="S", body="B", requester_email="a@b.no", requester_name="a",
                         external_id="x", tags=[])
    assert ei.value.uncertain is False and ei.value.code == "RecordInvalid"
    assert TITLE not in str(ei.value)


def test_zendesk_client_finds_oldest_by_external_id(monkeypatch):
    monkeypatch.setattr(
        "backend.zendesk_client.urllib.request.urlopen",
        lambda req, context=None, timeout=None: _Resp({"tickets": [
            {"id": 9, "created_at": "2026-11-05T09:00:00Z"},
            {"id": 3, "created_at": "2026-11-05T08:00:00Z"}]}, 200))
    zc = ZendeskClient(base_url="https://aidnsupport.zendesk.com", email="e", api_token="t")
    assert zc.find_by_external_id("productboard:n1")["id"] == 3


# ─── OAuth client credentials + one-note runs (added 2026-11-05) ─────────────

CLIENT_SECRET = "oauth-client-secret-xyz"


def test_zendesk_client_oauth_fetches_token_once_and_uses_bearer(monkeypatch):
    calls = []

    def fake_urlopen(req, context=None, timeout=None):
        calls.append(req)
        if req.full_url.endswith("/oauth/tokens"):
            return _Resp({"access_token": "tok-1", "token_type": "bearer"}, 200)
        return _Resp({"tickets": []}, 200)
    monkeypatch.setattr("backend.zendesk_client.urllib.request.urlopen", fake_urlopen)
    zc = ZendeskClient(base_url="https://aidnsupport.zendesk.com",
                       oauth_client_id="cid", oauth_client_secret=CLIENT_SECRET)
    zc.find_by_external_id("productboard:a")
    zc.find_by_external_id("productboard:b")
    token_calls = [c for c in calls if c.full_url.endswith("/oauth/tokens")]
    assert len(token_calls) == 1, "token is cached between calls"
    sent = json.loads(token_calls[0].data)
    assert sent == {"grant_type": "client_credentials", "client_id": "cid",
                    "client_secret": CLIENT_SECRET, "scope": "read write"}
    api = [c for c in calls if "/api/v2/" in c.full_url]
    assert all(c.get_header("Authorization") == "Bearer tok-1" for c in api)


def test_zendesk_client_oauth_failure_is_certain_and_hides_secret(monkeypatch):
    def deny(req, context=None, timeout=None):
        import io
        raise urllib.error.HTTPError(req.full_url, 401, "x", {},
                                     io.BytesIO(b'{"error":"invalid_client"}'))
    monkeypatch.setattr("backend.zendesk_client.urllib.request.urlopen", deny)
    zc = ZendeskClient(base_url="https://aidnsupport.zendesk.com",
                       oauth_client_id="cid", oauth_client_secret=CLIENT_SECRET)
    with pytest.raises(ZendeskError) as ei:
        zc.create_ticket(subject="S", body="B", requester_email="a@b.no",
                         requester_name="a", external_id="x", tags=[])
    assert ei.value.uncertain is False and ei.value.status == 401
    assert CLIENT_SECRET not in str(ei.value)


def test_make_client_prefers_oauth_and_reads_env(monkeypatch, tmp_path):
    from backend.config import load_config
    monkeypatch.setenv("ZENDESK_OPERATOR_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ZENDESK_OPERATOR_OAUTH_CLIENT_SECRET", CLIENT_SECRET)
    cfg = load_config(tmp_path / "missing.toml")
    cfg.zendesk.email, cfg.zendesk.api_token = "e@aidn.no", "t"
    zc = zendesk_transfer.make_client(cfg)
    assert zc.uses_oauth and zc.oauth_client_secret == CLIENT_SECRET and zc.api_token == ""
    assert zendesk_transfer.make_client(Config()) is None


def test_only_restricts_to_one_tagged_note(zcfg):
    pb = FakeTransferPB([v2_note("a"), v2_note("b"), v2_note("c", tags=())])
    zd = FakeZendesk()
    stats = run(zcfg, pb, zd, only={"b", "c"})
    assert stats["found"] == 1 and [c["external_id"] for c in zd.creates] == ["productboard:b"]
    assert pb.tags("a") == ["send-to-zendesk"]


def test_cli_one_note_runs_while_disabled_but_full_live_run_does_not(zcfg, monkeypatch):
    from backend import cli
    zcfg.zendesk.enabled = False
    pb, zd = FakeTransferPB([v2_note("a"), v2_note("b")]), FakeZendesk()
    monkeypatch.setattr(cli, "load_config", lambda path=None: zcfg)
    monkeypatch.setattr(cli, "_pb_or_die", lambda cfg: pb)
    monkeypatch.setattr(zendesk_transfer, "make_client", lambda cfg: zd)
    assert cli.main(["zendesk-transfer"]) == 1 and zd.creates == []
    assert cli.main(["zendesk-transfer", "--only", "b"]) == 0
    assert [c["external_id"] for c in zd.creates] == ["productboard:b"]
    assert pb.tags("a") == ["send-to-zendesk"] and pb.tags("b") == ["sent-to-zendesk"]
