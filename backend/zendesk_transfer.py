"""Productboard → Zendesk transfer (added 2026-11-05).

Some "suggestions" in Productboard are really support requests. A human adds
the PB tag `send-to-zendesk` (config: [zendesk].trigger_tag) — that tag IS the
approval; there is no AI deciding what counts as support. The scheduled run then:

  1. finds every non-archived PB note with the trigger tag — assigned and
     processed notes included, so this is a separate scan from the normal
     unassigned-note flow;
  2. creates ONE Zendesk ticket: note title as subject, note text as the first
     (public) comment, the PB submitter as requester, external_id
     `productboard:<uuid>`, Zendesk tag `from_productboard`. The PB link goes in
     a private internal note, because the requester sees the public comment;
  3. only after Zendesk confirms the ticket: stores the mapping, then in one PB
     PATCH adds `sent-to-zendesk`, assigns the note to the support owner, marks
     it processed and removes `send-to-zendesk`.

If anything fails, the trigger tag stays and the next run picks it up again.

Duplicate guard, in order:
  - the local zendesk_transfers row (unique per PB note) — if it has a ticket,
    that ticket is reused and only the PB updates are retried;
  - otherwise Zendesk is asked for the external_id BEFORE any create. That also
    covers the GitHub Actions cache (which holds the DB) being evicted;
  - a create that timed out / got a 5xx is never blindly retried: it is looked
    up by external_id first, in this run and the next.

Never logged or stored: note titles, note text, requester emails, secrets.
"""
from __future__ import annotations

import html as htmlmod
import logging
import re
import sqlite3

from . import acknowledge, db, pb_client
from .config import Config
from .zendesk_client import ZendeskClient, ZendeskError

log = logging.getLogger(__name__)

EXTERNAL_ID_PREFIX = "productboard:"
FALLBACK_SUBJECT = "Henvendelse fra Productboard"
INTERNAL_NOTE_TEMPLATE = "Opprettet automatisk fra Productboard-notat: {url}"

OUTCOMES = ("transferred", "already_transferred", "completed_after_partial_failure",
            "needs_review", "skipped", "failed")


def external_id(pb_uuid: str) -> str:
    return f"{EXTERNAL_ID_PREFIX}{pb_uuid}"


def has_tag(raw: dict, tag: str) -> bool:
    flat_tags = (raw.get("fields") or {}).get("tags") if isinstance(raw.get("fields"), dict) \
        else raw.get("tags")
    wanted = (tag or "").strip().lower()
    return bool(wanted) and any(t.strip().lower() == wanted
                                for t in pb_client._normalize_tags(flat_tags or []))


def find_tagged_notes(client, tag: str) -> list[dict]:
    """Every non-archived note carrying `tag`, whoever owns it."""
    out = []
    for raw in client.list_notes():
        fields = raw.get("fields") if isinstance(raw.get("fields"), dict) else {}
        if fields.get("archived"):
            continue
        if has_tag(raw, tag):
            out.append(raw)
    return out


_BLOCK_END_RE = re.compile(r"(?i)<br\s*/?>|</p>|</li>|</div>|</h[1-6]>")


def plain_text(raw_content) -> str:
    """Note HTML → readable plain text, keeping paragraph breaks."""
    text = pb_client._coerce_content(raw_content)
    text = _BLOCK_END_RE.sub("\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = htmlmod.unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def ticket_fields(raw: dict) -> tuple[str, str]:
    """(subject, first comment) for the ticket."""
    fields = raw.get("fields") if isinstance(raw.get("fields"), dict) else raw
    subject = (fields.get("name") or fields.get("title") or "").strip() or FALLBACK_SUBJECT
    body = plain_text(fields.get("content")) or subject
    return subject[:250], body


def resolve_requester(client, raw: dict) -> tuple[str, str]:
    """(email, name) of the note's submitter, or ("", "") when there is none.

    Same lookup as the confirmation emails (acknowledge.py): the note's
    customer must be a PB *user*; its email comes from /v2/entities/{id}.
    Never invents an address.
    """
    uid = acknowledge.customer_user_id(raw)
    if not uid:
        return "", ""
    try:
        _, payload = client._request("GET", f"/v2/entities/{uid}")
    except Exception as e:  # noqa: BLE001
        log.warning("zendesk: requester lookup failed for user %s (%s)", uid, type(e).__name__)
        return "", ""
    email = acknowledge._email_from_entity(payload)
    if not email:
        return "", ""
    name = ""
    for node in ((payload or {}).get("data"), payload):
        if isinstance(node, dict):
            f = node.get("fields") if isinstance(node.get("fields"), dict) else {}
            name = (f.get("name") or node.get("name") or "").strip()
            if name:
                break
    return email, name or email.split("@", 1)[0]


def _ok(status) -> bool:
    return isinstance(status, int) and 200 <= status < 300


def make_client(cfg: Config) -> ZendeskClient | None:
    z = cfg.zendesk
    if not (z.base_url and z.email and z.api_token):
        return None
    return ZendeskClient(base_url=z.base_url, email=z.email, api_token=z.api_token,
                         ssl_verify=z.ssl_verify, timeout_seconds=z.timeout_seconds)


# ─── one note ─────────────────────────────────────────────────────────────────

def transfer_note(conn: sqlite3.Connection, pb, zd: ZendeskClient | None,
                  cfg: Config, raw: dict, *, dry_run: bool = False) -> dict:
    """Move one tagged note. Returns {pb_uuid, outcome, ticket_id, error, ...}.

    In dry-run nothing is written anywhere (no Zendesk create, no PB PATCH, no
    DB row); Zendesk is only read, and only if credentials are set.
    """
    z = cfg.zendesk
    uuid = raw.get("id") or ""
    ext = external_id(uuid)
    flat = pb_client.flatten_note(raw, workspace=getattr(pb, "workspace", ""))
    result = {"pb_uuid": uuid, "pb_url": flat.get("display_url") or "",
              "outcome": "", "ticket_id": None, "error": ""}

    def save(**kw):
        if not dry_run:
            with db.transaction(conn):
                db.upsert_zendesk_transfer(conn, uuid, **kw)

    row = db.zendesk_transfer(conn, uuid)
    ticket_id = row["zendesk_ticket_id"] if row else None
    ticket_url = row["zendesk_ticket_url"] if row else ""
    note_added = bool(row and row["internal_note_added"])

    if ticket_id and row["status"] in ("ticket_created", "completed"):
        # Known ticket: never create another one. Only PB updates remain.
        history = "already" if row["status"] == "completed" else "partial"
    else:
        # No ticket known locally. Ask Zendesk before creating anything —
        # covers an earlier uncertain create and a lost local database.
        found = None
        if zd is not None:
            try:
                found = zd.find_by_external_id(ext)
            except ZendeskError as e:
                result.update(outcome="failed", error=f"lookup: {e}")
                save(status=row["status"] if row else "failed", last_error=f"lookup: {e}")
                return result
        if found:
            ticket_id, ticket_url = int(found["id"]), zd.ticket_url(found["id"])
            history = "partial"
            save(status="ticket_created", zendesk_ticket_id=ticket_id,
                 zendesk_ticket_url=ticket_url, last_error="")
        else:
            email, name = resolve_requester(pb, raw)
            if not email:
                result.update(outcome="needs_review", error="no requester email")
                save(status="needs_review", last_error="no requester email")
                return result
            result["requester"] = acknowledge.mask_email(email)
            if dry_run:
                result["outcome"] = "would_transfer"
                return result
            if zd is None:
                result.update(outcome="failed", error="zendesk credentials missing")
                return result

            subject, body = ticket_fields(raw)
            # Record the attempt BEFORE the call: if the process dies mid-call,
            # the row says "pending" and the next run looks the ticket up first.
            save(status="pending", count_attempt=True, last_error="")
            try:
                ticket = zd.create_ticket(
                    subject=subject, body=body, requester_email=email,
                    requester_name=name, external_id=ext,
                    tags=[z.ticket_tag] if z.ticket_tag else [],
                    group_id=z.group_id or None,
                )
            except ZendeskError as e:
                if not e.uncertain:
                    result.update(outcome="failed", error=f"create: {e}")
                    save(status="failed", last_error=f"create: {e}")
                    return result
                # Outcome unknown → look it up, never create again blindly.
                ticket = None
                try:
                    ticket = zd.find_by_external_id(ext)
                except ZendeskError:
                    pass
                if not ticket:
                    result.update(outcome="failed", error=f"create: {e}")
                    save(status="pending", last_error=f"create: {e}")
                    return result
            ticket_id, ticket_url = int(ticket["id"]), zd.ticket_url(ticket["id"])
            history = "new"
            save(status="ticket_created", zendesk_ticket_id=ticket_id,
                 zendesk_ticket_url=ticket_url, last_error="")

    result["ticket_id"] = ticket_id

    if dry_run:
        result["outcome"] = "would_update_pb"
        return result

    # PB link as a private note — best-effort, retried on later runs.
    if not note_added and zd is not None and result["pb_url"]:
        try:
            zd.add_internal_note(ticket_id, INTERNAL_NOTE_TEMPLATE.format(url=result["pb_url"]))
            save(status="ticket_created" if history != "already" else "completed",
                 internal_note_added=True)
        except ZendeskError as e:
            log.warning("zendesk: internal note failed on ticket %s (%s) — continuing",
                        ticket_id, e)

    # Productboard side, only now that the ticket exists.
    try:
        status = pb.mark_sent_to_zendesk(uuid, owner_email=z.pb_owner_email,
                                         add_tag=z.completed_tag,
                                         remove_tag=z.trigger_tag)
        if not _ok(status):
            raise pb_client.PBError(status or 0, "", url="")
    except pb_client.PBError as e:
        err = f"pb update: HTTP {e.status}"
        result.update(outcome="failed", error=err)
        save(status="ticket_created" if history != "already" else "completed",
             last_error=err)
        return result

    save(status="completed", last_error="")
    result["outcome"] = {"new": "transferred", "already": "already_transferred"}.get(
        history, "completed_after_partial_failure")
    return result


# ─── the scan ─────────────────────────────────────────────────────────────────

def run_transfers(conn: sqlite3.Connection, pb, cfg: Config, *,
                  zd: ZendeskClient | None = None, dry_run: bool = False) -> dict:
    """Scan PB for the trigger tag and transfer each note. Never raises for a
    single note — one failure does not stop the others."""
    z = cfg.zendesk
    stats: dict = {k: 0 for k in OUTCOMES}
    stats.update(dry_run=dry_run, found=0, notes=[], blocked="")
    if zd is None:
        zd = make_client(cfg)

    # Preconditions for a live run.
    if not z.pb_owner_email:
        stats["blocked"] = "pb_owner_email not set"
    else:
        try:
            owner_ok = pb.member_exists(z.pb_owner_email)
        except Exception as e:  # noqa: BLE001 — unknown counts as not verified
            log.warning("zendesk: could not verify PB owner (%s)", type(e).__name__)
            owner_ok = False
        stats["owner_verified"] = owner_ok
        if not owner_ok:
            stats["blocked"] = "pb_owner_email is not a verified Productboard member"
    if not stats["blocked"] and zd is None and not dry_run:
        stats["blocked"] = "zendesk credentials missing"
    if stats["blocked"] and not dry_run:
        log.error("zendesk: transfers blocked — %s", stats["blocked"])
        return stats

    try:
        tagged = find_tagged_notes(pb, z.trigger_tag)
    except Exception as e:  # noqa: BLE001
        log.error("zendesk: PB scan failed (%s)", type(e).__name__)
        stats["blocked"] = f"pb scan failed: {type(e).__name__}"
        return stats
    stats["found"] = len(tagged)
    log.info("zendesk: %d note(s) tagged %r", len(tagged), z.trigger_tag)

    for i, raw in enumerate(tagged):
        if i >= z.max_per_run:
            stats["skipped"] += 1
            stats["notes"].append({"pb_uuid": raw.get("id"), "outcome": "skipped",
                                   "error": "max_per_run reached"})
            continue
        try:
            res = transfer_note(conn, pb, zd, cfg, raw, dry_run=dry_run)
        except Exception as e:  # noqa: BLE001 — isolate failures per note
            res = {"pb_uuid": raw.get("id"), "outcome": "failed",
                   "error": f"unexpected: {type(e).__name__}"}
        outcome = res.get("outcome") or "failed"
        if outcome in stats:
            stats[outcome] += 1
        else:  # dry-run outcomes
            stats[outcome] = stats.get(outcome, 0) + 1
        stats["notes"].append(res)
        log.info("zendesk: note %s → %s%s%s", res.get("pb_uuid"), outcome,
                 f" (ticket {res['ticket_id']})" if res.get("ticket_id") else "",
                 f" [{res['error']}]" if res.get("error") else "")
    return stats
