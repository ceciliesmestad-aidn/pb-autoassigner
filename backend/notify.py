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

    return _post(cfg, {"text": "\n".join(lines)})


def send_failure(cfg: Config, error: str) -> bool:
    """Alert the channel that the scheduled run itself crashed."""
    text = (
        ":rotating_light: *PB AutoAssigner run FAILED* — "
        "notes are NOT being assigned until this is fixed.\n"
        f"```{error[:500]}```"
    )
    return _post(cfg, {"text": text})
