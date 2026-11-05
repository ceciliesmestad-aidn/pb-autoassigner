"""Confirmation emails to municipality submitters (added 2026-10-28).

When the AutoAssigner assigns a note, the submitter gets a short confirmation:
thanks, a 2–3 sentence summary of what they asked for, and a neutral "a product
team will assess and prioritise it" — no team names, no promises.

TEST PHASE — DRAFTS ONLY. This module has no sending code path. Drafts are
stored in the `ack_drafts` table and rendered to an HTML preview. Sending (via
Zendesk, from a no-reply address, tag `forbedringsforslag`) is a separate,
later step that needs explicit approval.

Token cost: one batched Haiku call per `batch_size` notes, only for notes that
actually qualify (kommune email). Everything except the summary is fixed text.

Who qualifies:
  - the note has a customer of type user in PB, and
  - that user's email domain ends with `kommune_suffix` (default ".kommune.no").
Everything else is recorded as skipped with a reason, so nothing is retried.
"""
from __future__ import annotations

import html
import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from . import db
from .config import Config

log = logging.getLogger(__name__)


# ─── Fixed email text (approved by Cecilie 2026-10-28, Aidn stil og tone) ────

SUBJECT_TEMPLATE = "Vi har mottatt forbedringsforslaget ditt: «{title}»"

BODY_TEMPLATE = """Hei,

Takk for at du tok deg tid til å sende oss et forbedringsforslag. Innspill fra dere som bruker Aidn hver dag, hjelper oss å gjøre løsningen bedre.

Slik har vi forstått forslaget ditt
{summary}

Hva skjer videre?
Vi har sendt forslaget til produktteamet som har ansvar for dette området. De vurderer og prioriterer innspillet. Når flere kommuner melder om samme behov, veier det tyngre i prioriteringen. Vi kan dessverre ikke love om eller når vi prioriterer forslaget ditt.

Vil du utdype forslaget, eller har du flere innspill? Send inn et nytt forslag fra Aidn.

Obs: Du kan ikke svare på denne e-posten.

Vennlig hilsen
Produktteamene i Aidn"""


def render_subject(title: str) -> str:
    t = " ".join((title or "").split()) or "Forbedringsforslag"
    if len(t) > 90:
        t = t[:87].rstrip() + "…"
    return SUBJECT_TEMPLATE.format(title=t)


def render_body(summary: str) -> str:
    return BODY_TEMPLATE.format(summary=summary.strip())


# ─── Who qualifies ───────────────────────────────────────────────────────────

def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].strip().lower() if "@" in (email or "") else ""


def is_kommune_email(email: str, suffix: str = ".kommune.no") -> bool:
    dom = email_domain(email)
    return bool(dom) and dom.endswith(suffix.lower())


def customer_user_id(raw: dict) -> str:
    """The id of the note's customer, when that customer is a *user* (person).

    v2 shape (seen in live data 2026-10): relationships.data[] =
    {type: "customer", target: {type: "user", id}}. Company-only customers
    have no email and return "".
    """
    items = ((raw.get("relationships") or {}).get("data")) or []
    if isinstance(items, dict):
        items = [items]
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "customer":
            continue
        target = item.get("target") or {}
        if target.get("type") == "user" and target.get("id"):
            return str(target["id"])
    return ""


def _email_from_entity(payload: Any) -> str:
    """Pull an email out of a /v2/entities/{id} response, defensively.

    ⚠️ Shape not yet verified against the live API (the sandbox can't reach
    api.productboard.com). Accepts {data:{fields:{email}}}, {fields:{email}},
    {data:{email}} and {email}.
    """
    if not isinstance(payload, dict):
        return ""
    for node in (payload.get("data"), payload):
        if not isinstance(node, dict):
            continue
        fields = node.get("fields") if isinstance(node.get("fields"), dict) else {}
        for cand in (fields.get("email"), node.get("email")):
            if isinstance(cand, str) and "@" in cand:
                return cand.strip().lower()
    return ""


def lookup_user_email(client, user_id: str) -> str:
    """Read-only GET of the customer user entity. Returns "" on any failure."""
    try:
        _, payload = client._request("GET", f"/v2/entities/{user_id}")
    except Exception as e:  # noqa: BLE001 — a lookup failure just skips the draft
        log.warning("ack: user lookup failed for %s (%s)", user_id, e)
        return ""
    return _email_from_entity(payload)


# ─── Duplicates: one confirmation per suggestion, even if submitted twice ────

DUPLICATE_SIMILARITY = 0.9


def _normalize(text: str) -> str:
    """Lowercase, letters/digits only, single spaces — so trivial differences
    (extra spaces, punctuation, line breaks) don't make a resubmission look new."""
    out = "".join(ch if ch.isalnum() else " " for ch in (text or "").lower())
    return " ".join(out.split())


def is_duplicate(title: str, content: str,
                 prior: list[tuple[str, str]]) -> bool:
    """True when (title, content) is the same suggestion as one already
    confirmed to this recipient: identical after normalising, or the same
    title with ≥90 % similar text (small edits on resubmission)."""
    from difflib import SequenceMatcher
    t, c = _normalize(title), _normalize(content)
    for pt, pc in prior:
        pt, pc = _normalize(pt), _normalize(pc)
        if c and c == pc:
            return True
        if t == pt and (c == pc or SequenceMatcher(None, c, pc).ratio() >= DUPLICATE_SIMILARITY):
            return True
    return False


# ─── Summary (the only KI part) ──────────────────────────────────────────────

SUMMARY_SYSTEM = """Du oppsummerer forbedringsforslag som kommuner har sendt til Aidn (et norsk journalsystem for helse- og omsorgstjenester). Oppsummeringen settes inn i en bekreftelse på e-post til den som sendte forslaget.

Skriv 1–3 korte setninger på bokmål, i Aidns stil:
- Klart: det viktigste først, korte og aktive setninger, hverdagslige ord.
- Snakk til avsenderen med «du»: start gjerne med «Du foreslår …» eller «Du ønsker …».
- Gjengi bare hva de ønsker og hvorfor. Ikke vurder forslaget.

Ikke ta med:
- navn på personer, pasientopplysninger eller andre personopplysninger
- påstander om helsetap, pasientskade, avvik eller alvorlige hendelser
- løfter, tidspunkter eller hva Aidn vil gjøre
- navn på produktteam eller ansatte i Aidn
- «AI» (skriv «KI» om det trengs)

Maks 350 tegn per oppsummering."""

SUMMARY_TOOL = {
    "name": "summaries",
    "description": "Return one summary per input note.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summaries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "note_id": {"type": "string"},
                        "summary": {"type": "string", "maxLength": 450},
                    },
                    "required": ["note_id", "summary"],
                },
            }
        },
        "required": ["summaries"],
    },
}


class Summarizer:
    def __init__(self, cfg: Config, client=None):
        self.model = cfg.acknowledge.model
        self.batch_size = max(1, cfg.acknowledge.batch_size)
        if client is None:
            from .classify import build_anthropic_client
            client = build_anthropic_client(cfg.anthropic)
        self.client = client

    def summarize(self, notes: list[dict]) -> dict[str, str]:
        """notes: [{note_id, title, body}] → {note_id: summary}"""
        out: dict[str, str] = {}
        for i in range(0, len(notes), self.batch_size):
            out.update(self._call(notes[i:i + self.batch_size]))
        return out

    def _call(self, chunk: list[dict]) -> dict[str, str]:
        payload = {"notes": [
            {"note_id": n["note_id"], "title": n["title"],
             "body": (n["body"] or "")[:3000]}
            for n in chunk
        ]}
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=200 * len(chunk) + 200,
            system=SUMMARY_SYSTEM,
            tools=[SUMMARY_TOOL],
            tool_choice={"type": "tool", "name": "summaries"},
            messages=[{"role": "user", "content": [{
                "type": "text",
                "text": "Oppsummer forslagene under.\n\n"
                        + json.dumps(payload, ensure_ascii=False, indent=2),
            }]}],
        )
        block = next((b for b in resp.content
                      if getattr(b, "type", None) == "tool_use"), None)
        if block is None:
            log.warning("ack: summarizer returned no tool_use block")
            return {}
        result: dict[str, str] = {}
        for item in (block.input or {}).get("summaries", []) or []:
            nid = str(item.get("note_id", ""))
            s = " ".join(str(item.get("summary") or "").split())
            if nid and s:
                result[nid] = s
        return result


# ─── Orchestration ───────────────────────────────────────────────────────────

@dataclass
class Candidate:
    note_id: str
    title: str
    body: str
    email: str = ""
    skip_reason: str = ""


def resolve_recipient(raw: dict, client, suffix: str) -> tuple[str, str]:
    """→ (email, skip_reason). Exactly one of them is non-empty."""
    uid = customer_user_id(raw)
    if not uid:
        return "", "no_customer"
    email = lookup_user_email(client, uid)
    if not email:
        return "", "lookup_failed"
    if not is_kommune_email(email, suffix):
        return email, "not_kommune"
    return email, ""


def draft_acknowledgments(conn: sqlite3.Connection, client, cfg: Config,
                          *, summarizer: Summarizer | None = None) -> dict:
    """Create drafts for newly assigned notes. Never sends anything."""
    ack = cfg.acknowledge
    rows = db.notes_needing_ack(conn, since=ack.since, limit=ack.max_per_run)
    stats = {"considered": len(rows), "drafted": 0, "skipped": {}}
    if not rows:
        log.info("ack: no newly assigned notes to draft")
        return stats

    to_summarize: list[tuple[sqlite3.Row, str]] = []
    # Per recipient: earlier drafts (DB) + notes accepted earlier in this run.
    prior: dict[str, list[tuple[str, str]]] = {}
    with db.transaction(conn):
        for r in rows:
            try:
                raw = json.loads(r["raw_json"] or "{}")
            except json.JSONDecodeError:
                raw = {}
            email, reason = resolve_recipient(raw, client, ack.kommune_suffix)
            if not reason:
                if email not in prior:
                    prior[email] = db.ack_prior_notes_for_recipient(conn, email)
                if is_duplicate(r["title"], r["content"], prior[email]):
                    reason = "duplicate"
                else:
                    prior[email].append((r["title"] or "", r["content"] or ""))
            if reason:
                db.insert_ack_draft(conn, note_id=r["id"], status="skipped",
                                    skip_reason=reason,
                                    recipient_domain=email_domain(email))
                stats["skipped"][reason] = stats["skipped"].get(reason, 0) + 1
            else:
                to_summarize.append((r, email))

    if not to_summarize:
        return stats

    summarizer = summarizer or Summarizer(cfg)
    summaries = summarizer.summarize([
        {"note_id": str(r["id"]), "title": r["title"], "body": r["content"]}
        for r, _ in to_summarize
    ])

    with db.transaction(conn):
        for r, email in to_summarize:
            summary = summaries.get(str(r["id"]), "")
            if not summary:
                # Leave it without a row so the next run retries the summary.
                stats["skipped"]["no_summary"] = stats["skipped"].get("no_summary", 0) + 1
                continue
            db.insert_ack_draft(
                conn, note_id=r["id"], status="draft",
                recipient_email=email, recipient_domain=email_domain(email),
                subject=render_subject(r["title"]), summary=summary,
                body=render_body(summary), model=summarizer.model,
            )
            stats["drafted"] += 1

    log.info("ack: done — %s", stats)
    return stats


# ─── Preview ─────────────────────────────────────────────────────────────────

SKIP_LABELS = {
    "no_customer": "Ingen avsender på notatet",
    "not_kommune": "Avsender har ikke kommune-e-post",
    "lookup_failed": "Fant ikke avsenderens e-post",
    "duplicate": "Samme forslag sendt flere ganger",
    "no_summary": "KI ga ingen oppsummering",
}


def mask_email(email: str) -> str:
    """kate.malin@kongsberg.kommune.no → k***@kongsberg.kommune.no"""
    if "@" not in (email or ""):
        return ""
    local, dom = email.split("@", 1)
    return f"{local[:1]}***@{dom}"


def render_preview_html(drafts: list[dict], skipped: list[dict]) -> str:
    """Self-contained HTML preview. Recipient addresses are masked."""
    e = html.escape
    cards = []
    for d in drafts:
        link = (f'<a href="{e(d.get("display_url") or "")}">Åpne i Productboard</a>'
                if d.get("display_url") else "")
        cards.append(f"""
<article class="card">
  <div class="meta">Til: {e(mask_email(d.get("recipient_email", "")))} · {link}</div>
  <div class="subject">{e(d.get("subject", ""))}</div>
  <pre>{e(d.get("body", ""))}</pre>
  <details><summary>Originalt forslag</summary><pre class="orig">{e(d.get("content") or "")}</pre></details>
</article>""")
    skip_rows = "".join(
        f"<tr><td>{e(s.get('title') or '')}</td>"
        f"<td>{e(SKIP_LABELS.get(s.get('skip_reason', ''), s.get('skip_reason', '')))}</td>"
        f"<td>{e(s.get('recipient_domain') or '')}</td></tr>"
        for s in skipped
    )
    return f"""<!doctype html>
<html lang="no"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kvitteringer – testutkast</title>
<style>
:root {{ --bg:#fdf6f0; --card:#fff; --ink:#1f1a2e; --muted:#6b6478; --brand:#5b3fd6; --line:#eadfd6; }}
body {{ background:var(--bg); color:var(--ink); font:15px/1.5 Inter,system-ui,sans-serif; margin:0; padding:24px 16px; }}
main {{ max-width:760px; margin:0 auto; }}
h1 {{ font-size:22px; margin:0 0 4px; }}
.lead {{ color:var(--muted); margin:0 0 24px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px 20px; margin:0 0 16px; }}
.meta {{ color:var(--muted); font-size:13px; }}
.meta a {{ color:var(--brand); }}
.subject {{ font-weight:600; margin:6px 0 10px; }}
pre {{ white-space:pre-wrap; font:inherit; margin:0; }}
pre.orig {{ color:var(--muted); font-size:13px; margin-top:8px; }}
details {{ margin-top:12px; font-size:13px; }}
table {{ width:100%; border-collapse:collapse; background:var(--card); border-radius:12px; overflow:hidden; }}
td,th {{ text-align:left; padding:8px 12px; border-bottom:1px solid var(--line); font-size:13px; }}
</style></head><body><main>
<h1>Kvitteringer – testutkast</h1>
<p class="lead">{len(drafts)} utkast · {len(skipped)} hoppet over · Ingenting er sendt.</p>
{''.join(cards) or '<p>Ingen utkast.</p>'}
<h2>Hoppet over</h2>
<table><tr><th>Forslag</th><th>Grunn</th><th>Domene</th></tr>{skip_rows}</table>
</main></body></html>"""


# ─── One-off local test (no DB): specific PB note ids → preview ──────────────

def preview_for_note_ids(client, cfg: Config, note_uuids: list[str],
                         *, summarizer: Summarizer | None = None) -> str:
    """Fetch specific notes straight from PB (read-only), draft, return HTML.
    Writes nothing — not to PB, not to the DB."""
    from .pb_client import flatten_note
    drafts: list[dict] = []
    skipped: list[dict] = []
    pending: list[tuple[dict, str]] = []
    prior: dict[str, list[tuple[str, str]]] = {}
    for uuid in note_uuids:
        _, payload = client._request("GET", f"/v2/notes/{uuid}")
        raw = (payload or {}).get("data") or payload or {}
        flat = flatten_note(raw, workspace=client.workspace)
        email, reason = resolve_recipient(raw, client, cfg.acknowledge.kommune_suffix)
        if not reason:
            seen = prior.setdefault(email, [])
            if is_duplicate(flat["title"], flat["content"], seen):
                reason = "duplicate"
            else:
                seen.append((flat["title"], flat["content"]))
        if reason:
            skipped.append({"title": flat["title"], "skip_reason": reason,
                            "recipient_domain": email_domain(email)})
        else:
            pending.append((flat, email))

    if pending:
        summarizer = summarizer or Summarizer(cfg)
        sums = summarizer.summarize([
            {"note_id": f["pb_uuid"], "title": f["title"], "body": f["content"]}
            for f, _ in pending
        ])
        for f, email in pending:
            s = sums.get(f["pb_uuid"], "")
            if not s:
                skipped.append({"title": f["title"], "skip_reason": "no_summary"})
                continue
            drafts.append({"recipient_email": email, "display_url": f["display_url"],
                           "subject": render_subject(f["title"]),
                           "body": render_body(s), "content": f["content"]})
    return render_preview_html(drafts, skipped)
