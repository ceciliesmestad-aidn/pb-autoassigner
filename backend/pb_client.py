"""Productboard API client.

Speaks either v1 or v2 of PB's REST API, selected via the `api_version`
attribute. v1 is the historical default; v2 is the migration target.
v1 sunset date: 2026-07-08 (see docs/v2_migration_plan.md).

PB API reference (as of April 2026):

v1:
  Base:        https://api.productboard.com
  Auth:        Authorization: Bearer <token>, X-Version: 1
  List notes:  GET  /notes?pageLimit=2000&pageCursor=...&ownerEmail=...
               Response envelope: {data: [...], pageCursor: "...", totalResults: N}
  Assign:      PATCH /notes/{uuid}   body: {data: {owner: {email: "..."}}}
               Returns 201 on success (quirky — not 200/204).
  Note shape:  {id, title, content, tags, company.name, source.system,
                displayUrl, createdAt, owner}

v2:
  Base:        https://api.productboard.com/v2
  Auth:        Authorization: Bearer <token>   (no X-Version header)
  List notes:  GET  /v2/notes?pageLimit=100&owner[email]=...&fields=all
               Response envelope: {data: [...], links: {next: "<full URL>"}}
               Pagination: follow links.next URL as-is until null.
               Filtering by owner[email] requires `members:pii:read` scope.
  Assign:      PATCH /v2/notes/{id}   body: {fields: {owner: {email: "..."}}}
  Note shape:  {id, type, createdAt, updatedAt,
                fields: {name, content, tags[], owner, creator, archived, processed},
                links: {self},
                relationships: {data: [...], links: {next}},
                metadata: {source: {system, recordId}}}

Both versions use the same rate limit (50 req/s) and the same Bearer token.
"""
from __future__ import annotations

import hashlib
import html as htmlmod
import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Iterator

log = logging.getLogger(__name__)

PB_API = "https://api.productboard.com"
V1_PAGE_LIMIT = 2000       # v1 accepted large page sizes; kept for backwards compat
V2_PAGE_LIMIT = 100        # v2 docs note 100 is the documented default; stay conservative
PAGINATION_DELAY = 0.2     # seconds between paginated GETs


@dataclass
class PBClient:
    token: str
    ssl_verify: bool = False
    patch_delay_seconds: float = 0.3
    # v2 caps pages at 50 notes, so a full scan is ~60 requests — a single slow
    # response behind the corporate proxy must not kill the run. Generous
    # timeout + GET retries (see _request_url) handle that.
    timeout_seconds: int = 90
    api_version: str = "v1"  # "v1" or "v2"

    def __post_init__(self) -> None:
        if self.api_version not in ("v1", "v2"):
            raise ValueError(f"unsupported api_version: {self.api_version!r} (expected 'v1' or 'v2')")
        self._company_names_cache: dict[str, str] | None = None

    # ─── internals ────────────────────────────────────────────────────────────

    def _ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if not self.ssl_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _headers(self) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        if self.api_version == "v1":
            h["X-Version"] = "1"
        return h

    def _notes_path(self) -> str:
        """Path prefix for the notes collection — differs per API version."""
        return "/v2/notes" if self.api_version == "v2" else "/notes"

    def _request_url(self, method: str, url: str, *, body: dict | None = None) -> tuple[int, dict | None]:
        """Fire a request at an absolute URL. Used for v2 pagination where the
        next-page URL is returned in `links.next` and should be followed as-is.

        GETs are retried up to 3 times on network timeouts/hiccups — a v2 full
        scan is dozens of small pages and one slow response (corporate proxy,
        PB blip) must not abort the whole run. Writes (PATCH/POST) are never
        retried: they are not idempotent-safe.
        """
        data = json.dumps(body).encode() if body is not None else None
        attempts = 3 if method == "GET" else 1
        for attempt in range(1, attempts + 1):
            req = urllib.request.Request(url, data=data, method=method, headers=self._headers())
            try:
                with urllib.request.urlopen(req, context=self._ssl_context(),
                                             timeout=self.timeout_seconds) as resp:
                    raw = resp.read()
                    parsed = json.loads(raw) if raw else None
                    return resp.status, parsed
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")
                raise PBError(e.code, err_body, url=url) from e
            except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as e:
                if attempt == attempts:
                    raise
                wait = 3 * attempt
                log.warning("PB %s %s failed (%s: %s) — retry %d/%d in %ds",
                            method, url.split("?")[0], type(e).__name__, e,
                            attempt, attempts - 1, wait)
                time.sleep(wait)
        raise AssertionError("unreachable")  # for the type checker

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 body: dict | None = None) -> tuple[int, dict | None]:
        url = PB_API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self._request_url(method, url, body=body)

    # ─── high-level ───────────────────────────────────────────────────────────

    def list_notes(self, *, owner_email: str | None = None) -> Iterator[dict]:
        """Paginated iterator over notes.

        `owner_email` filters to a specific assignee. Omit to fetch all notes.
        (PB has no native 'unassigned' filter — use fetch_unassigned() for that.)
        """
        if self.api_version == "v2":
            yield from self._list_notes_v2(owner_email=owner_email)
        else:
            yield from self._list_notes_v1(owner_email=owner_email)

    def _list_notes_v1(self, *, owner_email: str | None) -> Iterator[dict]:
        params: dict[str, str | int] = {"pageLimit": V1_PAGE_LIMIT}
        if owner_email:
            params["ownerEmail"] = owner_email

        total_fetched = 0
        while True:
            status, payload = self._request("GET", "/notes", params=params)
            if payload is None:
                break
            batch = payload.get("data", []) or []
            total = payload.get("totalResults", "?")
            total_fetched += len(batch)
            for n in batch:
                yield n

            cursor = payload.get("pageCursor")
            if not cursor:
                break
            log.info("fetched %d/%s notes, continuing…", total_fetched, total)
            params["pageCursor"] = cursor
            time.sleep(PAGINATION_DELAY)

    def _list_notes_v2(self, *, owner_email: str | None) -> Iterator[dict]:
        # v2 uses bracket-notation query params and link-following pagination.
        # We pass fields=all so null fields (e.g. owner on unassigned notes) are
        # present with an explicit null — that lets fetch_unassigned() detect them.
        # archived=false matches v1's default behaviour — without it, v2 also
        # returns archived notes (e.g. abandoned "New note" drafts), which
        # flooded the review queue when we first switched to v2.
        params: dict[str, str | int] = {
            "pageLimit": V2_PAGE_LIMIT,
            "fields": "all",
            "archived": "false",
        }
        if owner_email:
            params["owner[email]"] = owner_email

        url = PB_API + "/v2/notes?" + urllib.parse.urlencode(params)
        total_fetched = 0
        while True:
            status, payload = self._request_url("GET", url)
            if payload is None:
                break
            batch = payload.get("data", []) or []
            total_fetched += len(batch)
            for n in batch:
                yield n

            next_url = ((payload.get("links") or {}).get("next")) or None
            if not next_url:
                break
            log.info("fetched %d notes, continuing…", total_fetched)
            url = next_url
            time.sleep(PAGINATION_DELAY)

    def company_names(self) -> dict[str, str]:
        """id → company name map, fetched once per client instance.

        v2 notes no longer embed the company name — they reference a company id
        under `relationships`. This map lets flatten_note() resolve the name.
        Returns {} on v1 (name is embedded there) and on any API error, so the
        Insights municipality stats degrade gracefully instead of crashing a run.
        """
        if self.api_version != "v2":
            return {}
        if self._company_names_cache is not None:
            return self._company_names_cache

        names: dict[str, str] = {}
        try:
            url = PB_API + "/v2/companies?" + urllib.parse.urlencode(
                {"pageLimit": V2_PAGE_LIMIT})
            while True:
                _, payload = self._request_url("GET", url)
                if payload is None:
                    break
                for c in payload.get("data", []) or []:
                    cid = c.get("id") or ""
                    # Accept both flat ({name}) and enveloped ({fields:{name}}) shapes.
                    name = ((c.get("fields") or {}).get("name")) or c.get("name") or ""
                    if cid and name:
                        names[str(cid)] = name
                next_url = ((payload.get("links") or {}).get("next")) or None
                if not next_url:
                    break
                url = next_url
                time.sleep(PAGINATION_DELAY)
        except Exception as e:  # noqa: BLE001 — company names are nice-to-have
            log.warning("company_names: could not fetch /v2/companies (%s) — "
                        "municipality stats will be empty this run", e)
            names = {}

        self._company_names_cache = names
        log.info("company_names: resolved %d companies", len(names))
        return names

    def fetch_unassigned(self) -> list[dict]:
        """Return every note in the workspace with no owner.

        Neither v1 nor v2 has a server-side unassigned filter for notes — we
        filter client-side on the owner field, which differs per version.
        """
        if self.api_version == "v2":
            # v2: fields.owner is either a dict or null. Belt-and-braces: also
            # drop archived notes client-side in case the server-side
            # archived=false filter is ever ignored.
            return [n for n in self.list_notes()
                    if not (n.get("fields") or {}).get("owner")
                    and not (n.get("fields") or {}).get("archived")]
        # v1: owner is top-level
        return [n for n in self.list_notes() if not n.get("owner")]

    def assign(self, note_uuid: str, owner_email: str) -> int:
        """PATCH a note to set its owner. Returns HTTP status."""
        if self.api_version == "v2":
            # v2 supports two PATCH styles; we use the `fields` form — simpler,
            # parallels v1's `data`-wrapped body. Alternative is patch-op style:
            #   {"patch": [{"op": "set", "field": "owner", "value": {"email": ...}}]}
            body = {"fields": {"owner": {"email": owner_email}}}
            path = f"/v2/notes/{note_uuid}"
        else:
            body = {"data": {"owner": {"email": owner_email}}}
            path = f"/notes/{note_uuid}"

        status, _ = self._request("PATCH", path, body=body)
        time.sleep(self.patch_delay_seconds)
        return status


class PBError(Exception):
    def __init__(self, status: int, body: str, *, url: str = ""):
        super().__init__(f"PB API error {status} for {url}: {body}")
        self.status = status
        self.body = body
        self.url = url


# ─── flatten + hash helpers ───────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = htmlmod.unescape(text)
    return " ".join(text.split())


def flatten_note(raw: dict, company_names: dict[str, str] | None = None) -> dict:
    """Convert a PB note JSON into the flat shape db.upsert_note() expects.

    Auto-detects v1 vs v2 by looking for the v2-specific `fields` envelope.
    Both versions produce the same output dict shape so downstream code
    (pipeline, insights, train) doesn't need to care which API emitted the raw.

    `company_names` (id → name, from PBClient.company_names()) is only used on
    v2, where the company is a relationship reference instead of an embedded
    name. Omit it and `company` is simply empty for v2 notes.
    """
    if isinstance(raw.get("fields"), dict):
        return _flatten_v2(raw, company_names or {})
    return _flatten_v1(raw)


def _flatten_v1(raw: dict) -> dict:
    title = raw.get("title") or ""
    content_raw = raw.get("content") or ""
    content_clean = strip_html(content_raw)

    tags = _normalize_tags(raw.get("tags") or [])

    company = ((raw.get("company") or {}).get("name")) or ""
    source = ((raw.get("source") or {}).get("system")) or ""
    # PB API v1 uses camelCase; accept both spellings for robustness.
    display_url = raw.get("displayUrl") or raw.get("display_url") or ""
    pb_created_at = raw.get("createdAt") or raw.get("created_at") or ""

    return _assemble_flat(
        pb_uuid=raw.get("id") or "",
        title=title,
        content_clean=content_clean,
        tags=tags,
        company=company,
        source=source,
        display_url=display_url,
        pb_created_at=pb_created_at,
        raw=raw,
    )


def _company_id_from_relationships(raw: dict) -> str:
    """Pull the company/customer reference id out of a v2 note, defensively.

    The exact relationship item shape was not pinned down during Phase 0, so we
    accept the plausible variants: {type: "company", id}, {target: {type:
    "company", id}}, and {data: {type: "company", id}}. Returns "" when no
    company reference exists.
    """
    rel = raw.get("relationships") or {}
    items = rel.get("data") or []
    if isinstance(items, dict):  # single-item shape
        items = [items]
    for item in items:
        if not isinstance(item, dict):
            continue
        for node in (item, item.get("target") or {}, item.get("data") or {}):
            if isinstance(node, dict) and "company" in str(node.get("type", "")).lower():
                return str(node.get("id") or "")
    return ""


def _flatten_v2(raw: dict, company_names: dict[str, str]) -> dict:
    fields = raw.get("fields") or {}
    links = raw.get("links") or {}
    metadata = raw.get("metadata") or {}

    title = fields.get("name") or ""
    content_raw = fields.get("content") or ""
    content_clean = strip_html(content_raw)

    tags = _normalize_tags(fields.get("tags") or [])

    # v2 references the company by id under `relationships`; resolve the name
    # via the id→name map from PBClient.company_names(). Also accept an
    # embedded company name if PB ever returns one in `fields`.
    company = ((fields.get("company") or {}).get("name")
               if isinstance(fields.get("company"), dict) else "") or ""
    if not company:
        company = company_names.get(_company_id_from_relationships(raw), "")
    source = ((metadata.get("source") or {}).get("system")) or ""
    display_url = links.get("self") or ""
    pb_created_at = raw.get("createdAt") or ""

    return _assemble_flat(
        pb_uuid=raw.get("id") or "",
        title=title,
        content_clean=content_clean,
        tags=tags,
        company=company,
        source=source,
        display_url=display_url,
        pb_created_at=pb_created_at,
        raw=raw,
    )


def _normalize_tags(tags_raw: list) -> list[str]:
    """Both v1 and v2 accept array of strings or array of {name: ...} dicts."""
    out: list[str] = []
    for t in tags_raw:
        if isinstance(t, str):
            out.append(t)
        elif isinstance(t, dict):
            out.append(t.get("name") or "")
    return [t for t in out if t]


def _assemble_flat(*, pb_uuid: str, title: str, content_clean: str,
                   tags: list[str], company: str, source: str,
                   display_url: str, pb_created_at: str, raw: dict) -> dict:
    content_hash = hashlib.sha256(
        f"{title}\n---\n{content_clean}".encode("utf-8")
    ).hexdigest()
    return {
        "pb_uuid": pb_uuid,
        "content_hash": content_hash,
        "title": title,
        "content": content_clean,
        "tags": tags,
        "company": company,
        "source": source,
        "display_url": display_url,
        "pb_created_at": pb_created_at,
        "raw": raw,
    }


# ─── sanity check for owner→email map ─────────────────────────────────────────

def verify_owner_emails(client: PBClient, emails: list[str]) -> dict[str, int]:
    """For each email, call `list_notes(owner_email=...)` and count results.

    Zero results could mean: (a) the email is wrong, or (b) the PM genuinely has
    no notes. Either way, it flags suspicious entries. Stops after a handful of
    pages per owner to avoid scanning the whole workspace.
    """
    counts: dict[str, int] = {}
    for email in emails:
        try:
            n = 0
            for i, _ in enumerate(client.list_notes(owner_email=email)):
                n += 1
                if i >= 50:  # we only need proof-of-life, not exhaustive counts
                    break
            counts[email] = n
        except PBError as e:
            log.warning("verify: %s → %s", email, e)
            counts[email] = -1
    return counts
