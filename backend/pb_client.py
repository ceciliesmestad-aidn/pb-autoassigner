"""Productboard API client.

Ported from v1 `pb_fetcher.py` with additions:
- PATCH assign wrapped as a first-class method
- Owner-map verification helper
- Uses stdlib urllib to avoid extra deps; SSL verification is controlled by config
  (default off, matching v1's corporate-proxy workaround)

PB API reference (as of April 2026):
  Base:       https://api.productboard.com
  Auth:       Authorization: Bearer <token>, X-Version: 1
  List notes: GET  /notes?pageLimit=2000&pageCursor=...&ownerEmail=...
              Response: {data: [...], pageCursor: "...", totalResults: N}
              Cursor-based pagination, 1-minute cursor expiry, max 2000 per page.
  Assign:     PATCH /notes/{uuid}   body: {data: {owner: {email: "..."}}}
              Returns 201 on success (not 200/204).
  Rate limit: 50 req/s.
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
PAGE_LIMIT = 2000
PAGINATION_DELAY = 0.2  # seconds between paginated GETs


@dataclass
class PBClient:
    token: str
    ssl_verify: bool = False
    patch_delay_seconds: float = 0.3
    timeout_seconds: int = 30

    def _ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if not self.ssl_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "X-Version": "1",
        }

    # ─── low-level ────────────────────────────────────────────────────────────

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 body: dict | None = None) -> tuple[int, dict | None]:
        url = PB_API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
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

    # ─── high-level ───────────────────────────────────────────────────────────

    def list_notes(self, *, owner_email: str | None = None) -> Iterator[dict]:
        """Paginated iterator over notes.

        `owner_email` filters to a specific assignee. Omit to fetch all notes.
        (PB has no native 'unassigned' filter — use fetch_unassigned() for that.)
        """
        params: dict[str, str | int] = {"pageLimit": PAGE_LIMIT}
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

    def fetch_unassigned(self) -> list[dict]:
        """Return every note in the workspace with no owner.

        PB API has no server-side unassigned filter; we filter client-side.
        """
        all_notes = list(self.list_notes())
        return [n for n in all_notes if not n.get("owner")]

    def assign(self, note_uuid: str, owner_email: str) -> int:
        """PATCH a note to set its owner. Returns HTTP status (201 on success)."""
        status, _ = self._request(
            "PATCH",
            f"/notes/{note_uuid}",
            body={"data": {"owner": {"email": owner_email}}},
        )
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


def flatten_note(raw: dict) -> dict:
    """Convert the PB note JSON into the shape db.upsert_note() expects."""
    title = raw.get("title") or ""
    content_raw = raw.get("content") or ""
    content_clean = strip_html(content_raw)

    tags = []
    for t in raw.get("tags") or []:
        if isinstance(t, str):
            tags.append(t)
        elif isinstance(t, dict):
            tags.append(t.get("name") or "")
    tags = [t for t in tags if t]

    company = ((raw.get("company") or {}).get("name")) or ""
    source = ((raw.get("source") or {}).get("system")) or ""
    display_url = raw.get("display_url") or ""
    pb_created_at = raw.get("created_at") or ""

    content_hash = hashlib.sha256(
        f"{title}\n---\n{content_clean}".encode("utf-8")
    ).hexdigest()

    return {
        "pb_uuid": raw.get("id") or "",
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
