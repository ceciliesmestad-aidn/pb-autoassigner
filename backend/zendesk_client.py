"""Small Zendesk Support API client (added 2026-11-05).

Used only by backend/zendesk_transfer.py to turn Productboard notes tagged
`send-to-zendesk` into support tickets. Three calls, nothing else:

  find_by_external_id()  GET  /api/v2/tickets.json?external_id=...
                         Exact match, not the search index, so a ticket created
                         seconds ago is found (the Search API lags).
  create_ticket()        POST /api/v2/tickets.json
                         NEVER retried here. A timeout or 5xx means the outcome
                         is unknown; the caller must look the ticket up by
                         external_id before trying again.
  add_internal_note()    PUT  /api/v2/tickets/{id}.json with a private comment
                         (the Productboard link, which the requester must not see).

Auth: OAuth client credentials (env ZENDESK_OPERATOR_OAUTH_CLIENT_ID /
ZENDESK_OPERATOR_OAUTH_CLIENT_SECRET — what Aidn uses), or an API token
(env ZENDESK_EMAIL / ZENDESK_API_TOKEN). Never from code.

Errors never carry response bodies or ticket content in their message, so they
are safe to log and to store in the database.
"""
from __future__ import annotations

import base64
import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)


class ZendeskError(Exception):
    """A Zendesk call failed.

    `uncertain` is True when a write may still have gone through (timeout,
    connection drop, 5xx). For a create, that means: do NOT create again until
    find_by_external_id() has been checked.
    """

    def __init__(self, status: int | None, code: str = "", *, uncertain: bool = False):
        self.status = status
        self.code = code            # Zendesk's short error code, e.g. "RecordInvalid"
        self.uncertain = uncertain
        label = f"HTTP {status}" if status else "no response"
        super().__init__(f"Zendesk {label}{f' ({code})' if code else ''}"
                         f"{' — outcome unknown' if uncertain else ''}")


@dataclass
class ZendeskClient:
    """Two ways to authenticate (OAuth wins when both are set):

    - OAuth client credentials (machine-to-machine): `oauth_client_id` +
      `oauth_client_secret` are exchanged at POST /oauth/tokens for a bearer
      token, which is cached until shortly before it expires. Tickets are
      created as the user the OAuth client acts for (Aidn: the operator user).
    - API token: Basic auth as "<email>/token:<api_token>".
    """
    base_url: str                 # e.g. https://aidnsupport.zendesk.com
    email: str = ""               # API-token auth: agent / integration user email
    api_token: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_scope: str = "read write"
    ssl_verify: bool = True
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        self.base_url = (self.base_url or "").rstrip("/")
        if not self.base_url.startswith("https://"):
            raise ValueError("zendesk base_url must start with https://")
        self._access_token = ""
        self._token_expires_at = 0.0

    @property
    def uses_oauth(self) -> bool:
        return bool(self.oauth_client_id and self.oauth_client_secret)

    # ─── internals ────────────────────────────────────────────────────────────

    def _oauth_token(self) -> str:
        """Bearer token from the client-credentials grant, cached per client."""
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token
        body = {"grant_type": "client_credentials",
                "client_id": self.oauth_client_id,
                "client_secret": self.oauth_client_secret,
                "scope": self.oauth_scope}
        req = urllib.request.Request(
            self.base_url + "/oauth/tokens", data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, context=self._ssl_context(),
                                        timeout=self.timeout_seconds) as resp:
                payload = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            code = ""
            try:
                code = str((json.loads(e.read() or b"{}") or {}).get("error") or "")[:60]
            except Exception:  # noqa: BLE001
                pass
            # Getting a token creates nothing in Zendesk, so this is never uncertain.
            raise ZendeskError(e.code, f"oauth {code}".strip()) from None
        except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as e:
            raise ZendeskError(None, f"oauth {type(e).__name__}") from None
        token = payload.get("access_token") or ""
        if not token:
            raise ZendeskError(200, "oauth: no access_token in response")
        self._access_token = token
        # Zendesk tokens may or may not expire; default to one hour of reuse.
        self._token_expires_at = time.time() + float(payload.get("expires_in") or 3600)
        return token

    def _headers(self) -> dict[str, str]:
        if self.uses_oauth:
            auth = "Bearer " + self._oauth_token()
        else:
            raw = f"{self.email}/token:{self.api_token}".encode()
            auth = "Basic " + base64.b64encode(raw).decode()
        return {
            "Authorization": auth,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if not self.ssl_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _request(self, method: str, path: str, *, body: dict | None = None) -> dict:
        url = self.base_url + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=self._headers())
        try:
            with urllib.request.urlopen(req, context=self._ssl_context(),
                                        timeout=self.timeout_seconds) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            code = ""
            try:
                code = str((json.loads(e.read() or b"{}") or {}).get("error") or "")[:60]
            except Exception:  # noqa: BLE001 — the code is a nice-to-have
                pass
            # 5xx on a write: Zendesk may have saved it anyway.
            uncertain = method != "GET" and e.code >= 500
            raise ZendeskError(e.code, code, uncertain=uncertain) from None
        except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as e:
            log.warning("zendesk: %s %s failed (%s)", method, path.split("?")[0],
                        type(e).__name__)
            raise ZendeskError(None, type(e).__name__, uncertain=(method != "GET")) from None

    def ticket_url(self, ticket_id: int | str) -> str:
        """Agent-facing link to the ticket."""
        return f"{self.base_url}/agent/tickets/{ticket_id}"

    # ─── public ───────────────────────────────────────────────────────────────

    def find_by_external_id(self, external_id: str) -> dict | None:
        """The oldest ticket with this external_id, or None. Raises on failure.

        Zendesk does not enforce unique external_ids, so more than one hit is
        possible; we always pick the oldest so every run agrees on the same one.
        """
        q = urllib.parse.urlencode({"external_id": external_id})
        payload = self._request("GET", f"/api/v2/tickets.json?{q}")
        tickets = [t for t in (payload.get("tickets") or []) if isinstance(t, dict)]
        if not tickets:
            return None
        if len(tickets) > 1:
            log.warning("zendesk: %d tickets share external_id %s — using the oldest",
                        len(tickets), external_id)
        return sorted(tickets, key=lambda t: (t.get("created_at") or "", t.get("id") or 0))[0]

    def create_ticket(self, *, subject: str, body: str, requester_email: str,
                      requester_name: str, external_id: str, tags: list[str],
                      group_id: int | None = None) -> dict:
        """Create one ticket. Single attempt. Returns the ticket dict."""
        ticket: dict = {
            "subject": subject,
            "comment": {"body": body, "public": True},
            "requester": {"email": requester_email, "name": requester_name},
            "external_id": external_id,
            "tags": tags,
        }
        if group_id:
            ticket["group_id"] = int(group_id)
        payload = self._request("POST", "/api/v2/tickets.json", body={"ticket": ticket})
        created = payload.get("ticket") or {}
        if not created.get("id"):
            # 2xx without an id: we can't tell what happened.
            raise ZendeskError(200, "no ticket id in response", uncertain=True)
        return created

    def add_internal_note(self, ticket_id: int | str, body: str) -> None:
        """Private comment, visible to agents only."""
        self._request("PUT", f"/api/v2/tickets/{ticket_id}.json",
                      body={"ticket": {"comment": {"body": body, "public": False}}})
