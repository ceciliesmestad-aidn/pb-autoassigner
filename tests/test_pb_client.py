"""Unit tests for the PBClient v1/v2 toggle and shared flatten_note helper.

These tests stub out the single HTTP entry point on PBClient
(`_request_url`) so they exercise routing, body shape, pagination, and
flatten logic without hitting real Productboard.

Rationale for the split:
  - `flatten_note` is pure — no stubbing needed.
  - `_list_notes_v{1,2}` / `assign()` / `fetch_unassigned()` call
    `_request_url` once per HTTP hit. We replace that method with a
    canned-response recorder and assert on URLs, headers, and bodies.

Keep this file in lockstep with backend/pb_client.py — the v1 suite
guards against regressions during the migration, the v2 suite is the
spec the v2 implementation has to meet.
"""
from __future__ import annotations

import json
import urllib.parse

import pytest

from backend import pb_client
from backend.pb_client import PBClient, PBError, flatten_note, strip_html


# ─── sample PB responses ──────────────────────────────────────────────────────

V1_NOTE_RAW = {
    "id": "v1-uuid-1",
    "title": "Revurdering feiler",
    "content": "<p>Saksbehandler får <strong>feil</strong> ved revurdering.</p>",
    "tags": [{"name": "feedback"}, {"name": "bug"}],
    "company": {"name": "Oslo kommune"},
    "source": {"system": "slack"},
    "displayUrl": "https://aidn.productboard.com/notes/1",
    "createdAt": "2026-04-10T08:15:00Z",
    "owner": None,
}

V2_NOTE_RAW = {
    "id": "v2-note-1",
    "type": "note",
    "createdAt": "2026-04-10T08:15:00Z",
    "updatedAt": "2026-04-10T08:15:00Z",
    "fields": {
        "name": "Revurdering feiler",
        "content": "<p>Saksbehandler får <strong>feil</strong> ved revurdering.</p>",
        "tags": ["feedback", "bug"],
        "owner": None,
        "creator": {"email": "someone@aidn.no"},
        "archived": False,
        "processed": True,
    },
    "links": {"self": "https://api.productboard.com/v2/notes/v2-note-1"},
    "relationships": {"data": [], "links": {"next": None}},
    "metadata": {"source": {"system": "slack", "recordId": "abc123"}},
}


# ─── HTTP stub ────────────────────────────────────────────────────────────────


class StubHTTP:
    """Records every call made via PBClient._request_url and returns canned
    responses. Queue is FIFO; each response is (status, body_dict)."""

    def __init__(self, responses):
        self._queue = list(responses)
        self.calls: list[dict] = []

    def bind(self, client: PBClient) -> None:
        """Replace the client's _request_url with our recorder."""
        client._request_url = self._handle  # type: ignore[method-assign]

    def _handle(self, method: str, url: str, *, body=None):
        self.calls.append({"method": method, "url": url, "body": body})
        if not self._queue:
            raise AssertionError(f"StubHTTP ran out of responses on {method} {url}")
        return self._queue.pop(0)


# ─── flatten_note: auto-detection & shape parity ──────────────────────────────


def test_flatten_note_detects_v1():
    flat = flatten_note(V1_NOTE_RAW)
    assert flat["pb_uuid"] == "v1-uuid-1"
    assert flat["title"] == "Revurdering feiler"
    assert flat["content"] == "Saksbehandler får feil ved revurdering."
    assert flat["tags"] == ["feedback", "bug"]
    assert flat["company"] == "Oslo kommune"
    assert flat["source"] == "slack"
    assert flat["display_url"] == "https://aidn.productboard.com/notes/1"
    assert flat["pb_created_at"] == "2026-04-10T08:15:00Z"
    assert flat["raw"] is V1_NOTE_RAW


def test_flatten_note_detects_v2():
    flat = flatten_note(V2_NOTE_RAW)
    assert flat["pb_uuid"] == "v2-note-1"
    assert flat["title"] == "Revurdering feiler"
    assert flat["content"] == "Saksbehandler får feil ved revurdering."
    assert flat["tags"] == ["feedback", "bug"]
    # v2 does not embed company name — see TODO in _flatten_v2.
    assert flat["company"] == ""
    assert flat["source"] == "slack"
    assert flat["display_url"] == "https://api.productboard.com/v2/notes/v2-note-1"
    assert flat["pb_created_at"] == "2026-04-10T08:15:00Z"
    assert flat["raw"] is V2_NOTE_RAW


def test_flatten_note_produces_identical_shape_across_versions():
    """downstream code must not branch on api version — both produce the
    same key set and the same content hash when the underlying text matches."""
    v1 = flatten_note(V1_NOTE_RAW)
    v2 = flatten_note(V2_NOTE_RAW)
    assert set(v1.keys()) == set(v2.keys())
    # title + content are identical, so the content_hash should match.
    assert v1["content_hash"] == v2["content_hash"]


def test_flatten_note_handles_missing_fields():
    """Any field can be absent — all nullable without crashing."""
    sparse_v2 = {"id": "x", "fields": {}}
    flat = flatten_note(sparse_v2)
    assert flat["pb_uuid"] == "x"
    assert flat["title"] == ""
    assert flat["content"] == ""
    assert flat["tags"] == []
    assert flat["display_url"] == ""


def test_strip_html_unescapes_entities():
    assert strip_html("<p>A &amp; B</p>") == "A & B"
    assert strip_html("<br/>line1<br/>line2") == "line1 line2"
    assert strip_html("") == ""


# ─── PBClient construction ────────────────────────────────────────────────────


def test_client_rejects_unknown_api_version():
    with pytest.raises(ValueError, match="api_version"):
        PBClient(token="t", api_version="v3")


def test_headers_v1_includes_xversion():
    c = PBClient(token="t", api_version="v1")
    h = c._headers()
    assert h["Authorization"] == "Bearer t"
    assert h["X-Version"] == "1"


def test_headers_v2_omits_xversion():
    c = PBClient(token="t", api_version="v2")
    h = c._headers()
    assert h["Authorization"] == "Bearer t"
    assert "X-Version" not in h


# ─── list_notes: pagination ───────────────────────────────────────────────────


def test_list_notes_v1_follows_page_cursor():
    c = PBClient(token="t", api_version="v1", patch_delay_seconds=0)
    stub = StubHTTP([
        (200, {"data": [V1_NOTE_RAW], "pageCursor": "abc", "totalResults": 2}),
        (200, {"data": [dict(V1_NOTE_RAW, id="v1-uuid-2")], "pageCursor": None}),
    ])
    stub.bind(c)

    # Patch sleep so tests don't actually wait between pages.
    import backend.pb_client as mod
    original_sleep = mod.time.sleep
    mod.time.sleep = lambda _s: None
    try:
        got = list(c.list_notes())
    finally:
        mod.time.sleep = original_sleep

    assert [n["id"] for n in got] == ["v1-uuid-1", "v1-uuid-2"]
    # 2 GET calls, paged via pageCursor query param
    assert len(stub.calls) == 2
    assert "pageCursor" not in stub.calls[0]["url"]
    assert "pageCursor=abc" in stub.calls[1]["url"]
    # v1 path, no /v2 prefix
    assert stub.calls[0]["url"].startswith("https://api.productboard.com/notes?")


def test_list_notes_v2_follows_links_next():
    c = PBClient(token="t", api_version="v2", patch_delay_seconds=0)
    next_url = "https://api.productboard.com/v2/notes?pageLimit=100&cursor=xyz"
    stub = StubHTTP([
        (200, {"data": [V2_NOTE_RAW], "links": {"next": next_url}}),
        (200, {"data": [dict(V2_NOTE_RAW, id="v2-note-2")], "links": {"next": None}}),
    ])
    stub.bind(c)

    import backend.pb_client as mod
    original_sleep = mod.time.sleep
    mod.time.sleep = lambda _s: None
    try:
        got = list(c.list_notes())
    finally:
        mod.time.sleep = original_sleep

    assert [n["id"] for n in got] == ["v2-note-1", "v2-note-2"]
    assert len(stub.calls) == 2
    # First call builds URL with bracket params and fields=all.
    first_url = stub.calls[0]["url"]
    assert first_url.startswith("https://api.productboard.com/v2/notes?")
    assert "fields=all" in first_url
    # Second call follows links.next as-is (no re-encoding).
    assert stub.calls[1]["url"] == next_url


def test_list_notes_v2_encodes_owner_bracket_param():
    c = PBClient(token="t", api_version="v2", patch_delay_seconds=0)
    stub = StubHTTP([(200, {"data": [], "links": {}})])
    stub.bind(c)

    list(c.list_notes(owner_email="pm@aidn.no"))
    url = stub.calls[0]["url"]
    # urlencode escapes [ and ] — accept either literal or %-encoded form.
    assert ("owner[email]=pm%40aidn.no" in url
            or "owner%5Bemail%5D=pm%40aidn.no" in url)


def test_list_notes_v1_encodes_owner_email_param():
    c = PBClient(token="t", api_version="v1", patch_delay_seconds=0)
    stub = StubHTTP([(200, {"data": [], "pageCursor": None})])
    stub.bind(c)

    list(c.list_notes(owner_email="pm@aidn.no"))
    url = stub.calls[0]["url"]
    assert "ownerEmail=pm%40aidn.no" in url


# ─── fetch_unassigned: version-aware owner detection ──────────────────────────


def test_fetch_unassigned_v1_filters_on_toplevel_owner():
    c = PBClient(token="t", api_version="v1", patch_delay_seconds=0)
    stub = StubHTTP([(200, {
        "data": [
            V1_NOTE_RAW,  # owner=None → unassigned
            dict(V1_NOTE_RAW, id="v1-owned", owner={"email": "x@aidn.no"}),
        ],
        "pageCursor": None,
    })])
    stub.bind(c)

    unassigned = c.fetch_unassigned()
    assert [n["id"] for n in unassigned] == ["v1-uuid-1"]


def test_fetch_unassigned_v2_filters_on_fields_owner():
    c = PBClient(token="t", api_version="v2", patch_delay_seconds=0)
    owned = json.loads(json.dumps(V2_NOTE_RAW))  # deep copy
    owned["id"] = "v2-owned"
    owned["fields"]["owner"] = {"email": "x@aidn.no"}

    stub = StubHTTP([(200, {"data": [V2_NOTE_RAW, owned], "links": {}})])
    stub.bind(c)

    unassigned = c.fetch_unassigned()
    assert [n["id"] for n in unassigned] == ["v2-note-1"]


# ─── assign: URL + body dispatch ──────────────────────────────────────────────


def test_assign_v1_sends_data_wrapped_body_to_notes_path():
    c = PBClient(token="t", api_version="v1", patch_delay_seconds=0)
    stub = StubHTTP([(201, None)])
    stub.bind(c)

    status = c.assign("v1-uuid-1", "pm@aidn.no")
    assert status == 201

    call = stub.calls[0]
    assert call["method"] == "PATCH"
    assert call["url"] == "https://api.productboard.com/notes/v1-uuid-1"
    assert call["body"] == {"data": {"owner": {"email": "pm@aidn.no"}}}


def test_assign_v2_sends_fields_wrapped_body_to_v2_notes_path():
    c = PBClient(token="t", api_version="v2", patch_delay_seconds=0)
    stub = StubHTTP([(200, {"data": {"id": "v2-note-1"}})])
    stub.bind(c)

    status = c.assign("v2-note-1", "pm@aidn.no")
    assert status == 200

    call = stub.calls[0]
    assert call["method"] == "PATCH"
    assert call["url"] == "https://api.productboard.com/v2/notes/v2-note-1"
    assert call["body"] == {"data": {"fields": {"owner": {"email": "pm@aidn.no"}}}}


# ─── verify_owner_emails: exercise both versions ──────────────────────────────


def test_verify_owner_emails_counts_per_email():
    c = PBClient(token="t", api_version="v2", patch_delay_seconds=0)
    # Three notes owned by pm@aidn.no, zero for other@aidn.no
    stub = StubHTTP([
        (200, {"data": [V2_NOTE_RAW, V2_NOTE_RAW, V2_NOTE_RAW], "links": {}}),
        (200, {"data": [], "links": {}}),
    ])
    stub.bind(c)

    counts = pb_client.verify_owner_emails(c, ["pm@aidn.no", "other@aidn.no"])
    assert counts == {"pm@aidn.no": 3, "other@aidn.no": 0}


# ─── company resolution (v2) ──────────────────────────────────────────────────


def _v2_note_with_company(company_id="comp-9"):
    import copy
    raw = copy.deepcopy(V2_NOTE_RAW)
    raw["relationships"] = {"data": [{"target": {"type": "company", "id": company_id}}]}
    return raw


def test_company_names_fetches_and_caches_v2():
    c = PBClient(token="t", api_version="v2", patch_delay_seconds=0)
    stub = StubHTTP([
        (200, {"data": [{"id": "comp-9", "fields": {"name": "Bergen kommune"}}],
               "links": {"next": None}}),
    ])
    stub.bind(c)
    assert c.company_names() == {"comp-9": "Bergen kommune"}
    # Second call must hit the cache, not the (now empty) stub queue.
    assert c.company_names() == {"comp-9": "Bergen kommune"}
    assert len(stub.calls) == 1
    assert "/v2/companies" in stub.calls[0]["url"]


def test_company_names_is_empty_on_v1_without_requests():
    c = PBClient(token="t", api_version="v1", patch_delay_seconds=0)
    stub = StubHTTP([])  # any request would raise
    stub.bind(c)
    assert c.company_names() == {}
    assert stub.calls == []


def test_company_names_degrades_to_empty_on_api_error():
    c = PBClient(token="t", api_version="v2", patch_delay_seconds=0)

    def boom(method, url, *, body=None):
        raise pb_client.PBError(403, "missing scope", url=url)
    c._request_url = boom  # type: ignore[method-assign]
    assert c.company_names() == {}


def test_flatten_v2_resolves_company_from_relationships():
    names = {"comp-9": "Bergen kommune"}
    flat = pb_client.flatten_note(_v2_note_with_company(), names)
    assert flat["company"] == "Bergen kommune"


def test_flatten_v2_accepts_alternative_relationship_shapes():
    names = {"comp-9": "Bergen kommune"}
    raw_flat_shape = _v2_note_with_company()
    raw_flat_shape["relationships"] = {"data": [{"type": "company", "id": "comp-9"}]}
    assert pb_client.flatten_note(raw_flat_shape, names)["company"] == "Bergen kommune"

    raw_data_shape = _v2_note_with_company()
    raw_data_shape["relationships"] = {"data": [{"data": {"type": "Company", "id": "comp-9"}}]}
    assert pb_client.flatten_note(raw_data_shape, names)["company"] == "Bergen kommune"


def test_flatten_v2_company_empty_without_map_or_relationship():
    assert pb_client.flatten_note(_v2_note_with_company())["company"] == ""
    assert pb_client.flatten_note(V2_NOTE_RAW, {"comp-9": "X"})["company"] == ""


def test_company_names_falls_back_to_v1_when_v2_endpoint_missing():
    c = PBClient(token="t", api_version="v2", patch_delay_seconds=0)

    def handler(method, url, *, body=None):
        if "/v2/companies" in url:
            raise pb_client.PBError(404, "not found", url=url)
        # v1 fallback path
        assert "/companies" in url and "/v2/" not in url
        return 200, {"data": [{"id": "c1", "name": "Oslo kommune"}], "pageCursor": None}

    c._request_url = handler  # type: ignore[method-assign]
    # The v1 sibling client created inside the fallback needs stubbing too —
    # patch the class-level _request_url used by new instances.
    orig = PBClient._request_url
    PBClient._request_url = handler  # type: ignore[method-assign]
    try:
        assert c.company_names() == {"c1": "Oslo kommune"}
    finally:
        PBClient._request_url = orig  # type: ignore[method-assign]
