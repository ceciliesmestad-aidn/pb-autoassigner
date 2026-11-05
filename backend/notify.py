"""Slack notifications for scheduled pb-assigner runs.

Posts to a Slack incoming webhook (config: [slack] webhook_url, or the
SLACK_WEBHOOK_URL environment variable). Two message types:

  send_run_report()  — one digest per run: what was auto-assigned (per PM),
                       plus an alert section listing every note that could
                       NOT be auto-assigned and is waiting for a human.
  send_failure()     — fired when the run itself crashes, so silence in the
                       channel never means "broken", only "nothing to do".

All functions are best-effort: they log errors but never raise, so a Slack
hiccup can never break an assignment run.
"""
from __future__ import annotations

import json
import logging
import ssl
import urllib.request

from .config import Config

log = logging.getLogger(__name__)

# How many waiting notes to list in detail before truncating the message.
MAX_LISTED_NOTES = 15


def _post(cfg: Config, payload: dict) -> bool:
    """POST a payload to the configured webhook. Returns True on success."""
    url = cfg.slack.webhook_url
    if not cfg.slack.enabled or not url:
        log.info("slack: notifications disabled or webhook_url not set — skipping")
        return False

    ctx = None
    if not cfg.slack.ssl_verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            ok = 200 <= resp.status < 300
            if not ok:
                log.error("slack: webhook returned HTTP %s", resp.status)
            return ok
    except Exception as e:  # noqa: BLE001 — notifications must never crash a run
        log.error("slack: failed to post notification: %s", e)
        return False


def _note_line(n: dict) -> str:
    """One mrkdwn line for a note waiting for human review."""
    title = (n.get("title") or "(untitled)").strip()
    if len(title) > 90:
        title = title[:87] + "…"
    url = n.get("display_url") or ""
    label = f"<{url}|{title}>" if url else title

    pm = n.get("suggested_pm")
    conf = n.get("confidence")
    if pm and conf is not None:
        why = f"suggested: {pm} ({conf:.2f} — below threshold)"
    elif pm:
        why = f"suggested: {pm}"
    else:
        why = "no clear owner (leave open)"
    return f"• {label} — {why}"


def send_run_report(
    cfg: Config,
    *,
    ingest_stats: dict,
    classify_stats: dict,
    autopilot_stats: dict,
    needs_review: list[dict],
    zendesk_stats: dict | None = None,
) -> bool:
    """Post the per-run digest + alerts. One message per run."""
    assigned = autopilot_stats.get("assigned", 0)
    per_pm = autopilot_stats.get("per_pm", {}) or {}
    dry_run = autopilot_stats.get("dry_run", False)
    errors = autopilot_stats.get("errors", 0)
    cap_pm = autopilot_stats.get("queued_overflow_per_pm", 0)
    cap_total = autopilot_stats.get("queued_total_cap_exceeded", 0)
    new_notes = ingest_stats.get("inserted", 0)

    lines: list[str] = []
    tag = " *(DRY-RUN — nothing was actually assigned)*" if dry_run else ""
    lines.append(f":robot_face: *PB AutoAssigner — daily run*{tag}")

    if new_notes == 0 and assigned == 0 and not needs_review:
        lines.append("No new notes today. Everything is assigned. :palm_tree:")
    else:
        lines.append(
            f"Fetched *{new_notes}* new note(s), "
            f"classified {classify_stats.get('classified', 0)}."
        )
        if assigned:
            pm_bits = ", ".join(
                f"{email.split('@')[0].replace('.', ' ').title()}: {count}"
                for email, count in sorted(per_pm.items(), key=lambda kv: -kv[1])
            )
            lines.append(f":white_check_mark: Auto-assigned *{assigned}* note(s) — {pm_bits}")
        elif new_notes:
            lines.append(":white_check_mark: No notes reached the assignment threshold.")

    if needs_review:
        lines.append("")
        lines.append(
            f":warning: *{len(needs_review)} note(s) need a human* "
            f"— open the Reviewer tab when you're back:"
        )
        for n in needs_review[:MAX_LISTED_NOTES]:
            lines.append(_note_line(n))
        if len(needs_review) > MAX_LISTED_NOTES:
            lines.append(f"… and {len(needs_review) - MAX_LISTED_NOTES} more.")

    if cap_total:
        lines.append(
            f":rotating_light: *Total cap tripped* ({cap_total} notes held back). "
            "Something may be wrong with classification — the whole batch awaits manual review."
        )
    if cap_pm:
        lines.append(
            f":rotating_light: Per-PM cap reached — {cap_pm} note(s) queued for review."
        )
    if errors:
        lines.append(
            f":rotating_light: *{errors} assignment(s) FAILED* against Productboard — check the log."
        )

    zd_lines = zendesk_lines(zendesk_stats)
    if zd_lines:
        lines.append("")
        lines.extend(zd_lines)

    return _post(cfg, {"text": "\n".join(lines)})


def zendesk_lines(stats: dict | None) -> list[str]:
    """Slack lines for the PB → Zendesk transfer. Counts and links only —
    never note titles, text or requester emails. Empty when there's nothing
    worth saying."""
    if not stats:
        return []
    if stats.get("blocked"):
        return [f":rotating_light: *Zendesk transfer blocked* — {stats['blocked']}. "
                "Notes tagged send-to-zendesk are waiting."]
    done = stats.get("transferred", 0) + stats.get("completed_after_partial_failure", 0)
    review = stats.get("needs_review", 0)
    failed = stats.get("failed", 0)
    if not (done or review or failed or stats.get("already_transferred") or stats.get("skipped")):
        return []
    out = [f":incoming_envelope: *Zendesk:* {done} note(s) sent to support"
           + (f" ({stats['completed_after_partial_failure']} finished after an earlier failure)"
              if stats.get("completed_after_partial_failure") else "")
           + (f", {stats['already_transferred']} already sent" if stats.get("already_transferred") else "")
           + "."]
    for n in stats.get("notes") or []:
        if n.get("outcome") == "needs_review" and n.get("pb_url"):
            out.append(f"• <{n['pb_url']}|Note> has no submitter email — "
                       "add a customer with an email, or remove the tag")
    if review and not any(n.get("outcome") == "needs_review" and n.get("pb_url")
                          for n in stats.get("notes") or []):
        out.append(f"• {review} note(s) have no submitter email and stay tagged")
    if failed:
        out.append(f":warning: {failed} transfer(s) failed — they stay tagged and are "
                   "retried tomorrow. Check the log.")
    if stats.get("skipped"):
        out.append(f"• {stats['skipped']} note(s) held back by max_per_run")
    return out


def send_zendesk_report(cfg: Config, stats: dict) -> bool:
    """Standalone message for a manual `zendesk-transfer` run."""
    lines = zendesk_lines(stats)
    if not lines:
        return False
    return _post(cfg, {"text": "\n".join(lines)})


TENDER_BOARD_URL = (
    "https://aidn.productboard.com/prioritization/"
    "MTpQcmlvcml0aXphdGlvbkJvYXJkOjFkM2FhMDNmLTFmZDUtNDA3Mi05OGQ1LTM1NGRhYTgyOWVkMA=="
)

# How many findings of one kind to spell out before collapsing to a count.
MAX_LISTED_FINDINGS = 8


def send_tender_watchdog_report(cfg: Config, report, *, owner_mention: str = "") -> bool:
    """Post the tender commitment board coverage report.

    `report` is a backend.tender_watchdog.WatchdogReport. Posts to the same
    webhook as the assignment digest — currently #support-productboard, which
    is where the Notion routine already directs Productboard edge cases.

    Silence must never be ambiguous, so this posts even when everything is
    clean: an all-clear line is the signal that the check actually ran.
    """
    from .tender_watchdog import (
        FINDING_INCOMPLETE_PAIR, FINDING_MISSING_FIELD, FINDING_MISSING_OPTIONS,
        FINDING_NAMING_DRIFT, FINDING_PREFIX_ANOMALY,
    )

    lines: list[str] = [
        f":clipboard: *<{TENDER_BOARD_URL}|Tender Commitment board> — weekly coverage check*"
    ]

    if report.inconclusive:
        lines.append(
            f":rotating_light: *Check could not run:* {report.inconclusive}\n"
            "_Do not read this as 'all clear'._"
        )
        return _post(cfg, {"text": "\n".join(lines)})

    lines.append(
        f"{len(report.tenders_with_options)} tender(s) with requirements in PB · "
        f"{len(report.tenders_with_deadline_field)} with a committed-deadline field."
    )

    if report.ok:
        lines.append(
            ":white_check_mark: Every tender with requirements has a matching "
            "committed-deadline field, and the naming is consistent. :palm_tree:"
        )
        return _post(cfg, {"text": "\n".join(lines)})

    sections = [
        (FINDING_MISSING_FIELD,
         ":rotating_light: *Missing from the board* — no committed-deadline field, "
         "so the tender cannot show up as a column or a filter:"),
        (FINDING_MISSING_OPTIONS,
         ":warning: *Field exists but no requirements logged* — tender team to confirm:"),
        (FINDING_INCOMPLETE_PAIR,
         ":warning: *Only one of T / S present:*"),
        (FINDING_NAMING_DRIFT,
         ":mag: *Naming drift* (report only — renaming a field affects every board "
         "using it as a column):"),
        (FINDING_PREFIX_ANOMALY,
         ":mag: *Off-convention option names:*"),
    ]

    for kind, header in sections:
        group = [f for f in report.findings if f.kind == kind]
        if not group:
            continue
        lines.append("")
        lines.append(header)
        for f in group[:MAX_LISTED_FINDINGS]:
            lines.append(f"• *{f.tender}* — {f.detail}")
            if f.action and f.severity == "alert":
                lines.append(f"    ↳ _{f.action}_")
        if len(group) > MAX_LISTED_FINDINGS:
            lines.append(f"… and {len(group) - MAX_LISTED_FINDINGS} more.")

    if report.alerts:
        who = owner_mention or "_(no owner configured — set `[tender_watchdog].owner_mention`)_"
        lines.append("")
        lines.append(
            f"{who} — the three steps above are UI-only: Productboard's API cannot "
            "create a custom field, a board column, or a board filter."
        )

    return _post(cfg, {"text": "\n".join(lines)})


def send_failure(cfg: Config, error: str) -> bool:
    """Alert the channel that the scheduled run itself crashed."""
    text = (
        ":rotating_light: *PB AutoAssigner run FAILED* — "
        "notes are NOT being assigned until this is fixed.\n"
        f"```{error[:500]}```"
    )
    return _post(cfg, {"text": text})
