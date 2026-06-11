# Summer autopilot — setup guide

**Goal:** notes get assigned automatically every day at 09:00, with a digest + alerts in
**#productboard-assignment-alerts**, running on GitHub's servers — your Mac can be off all summer.

**What Claude already did (2026-06-11):**
- Autopilot flipped from dry-run to **live** in `config.toml` (30 days of dry-run: 556 decisions, 1 override)
- New Slack notifier (`backend/notify.py`) — every run posts a digest; failures post a red alert
- GitHub Actions workflow (`.github/workflows/daily-run.yml`) + cloud config (`config.ci.toml`)

**What you need to do:** 4 steps, ~20 minutes. Then one verification run.

---

## Step 1 — Create the Slack webhook (~5 min)

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**
2. Name: `PB AutoAssigner`, workspace: Aidn → **Create App**
3. Left menu: **Incoming Webhooks** → toggle **On**
4. **Add New Webhook to Workspace** → choose channel **#productboard-assignment-alerts** → **Allow**
   - If Aidn requires admin approval for apps, you'll get a "request approval" screen — send it and wait for IT/Slack admin.
5. Copy the webhook URL (starts with `https://hooks.slack.com/services/…`)

Paste it into `config.toml` on your Mac (open the file in TextEdit, find the `[slack]` section):

```toml
[slack]
webhook_url = "https://hooks.slack.com/services/PASTE-HERE"
```

⚠️ The webhook URL is a secret — anyone with it can post to the channel. Don't share it in Slack/email.

## Step 2 — Test one live run on your Mac (~5 min)

The app was also migrated to Productboard's **v2 API** (v1 dies 8 July). This step verifies both that and the Slack digest. In Terminal:

```bash
cd ~/Documents/Claude/Projects/PB/PB_assignerV2
.venv/bin/python -m backend.cli verify-map
.venv/bin/python -m backend.cli run
```

Check:

1. `verify-map` lists the PMs with note counts > 0 (proves v2 + email filter works)
2. A digest appears in #productboard-assignment-alerts
3. Any assigned notes show the new owner in Productboard — **this run assigns for real**

If something errors, tell Claude — or as an emergency fallback open `config.toml` and set `api_version = "v2"` back to `"v1"` (works until 8 July).

## Step 3 — Put the repo on GitHub (~5 min)

You need a GitHub account (github.com — free, sign up with your Aidn email if you don't have one).

1. On github.com: **+** (top right) → **New repository**
   - Name: `pb-autoassigner` — Visibility: **Private** — don't add README/gitignore
2. In Terminal (replace `YOUR-USERNAME`) — everything is already committed, you only push:

```bash
cd ~/Documents/Claude/Projects/PB/PB_assignerV2
git remote add origin https://github.com/YOUR-USERNAME/pb-autoassigner.git
git push -u origin master
```

(If git asks you to log in, follow the browser prompt.)

Secrets are safe: `config.toml` (your keys) is gitignored and never uploaded.

## Step 4 — Add the three secrets on GitHub (~5 min)

In your new repo: **Settings → Secrets and variables → Actions → New repository secret**. Add these three, exactly these names:

| Name | Value |
|---|---|
| `PB_TOKEN` | the Productboard token from `config.toml` |
| `ANTHROPIC_API_KEY` | the Anthropic key from `config.toml` |
| `SLACK_WEBHOOK_URL` | the webhook URL from Step 1 |

---

## Verify (the trust check)

1. Repo → **Actions** tab → **Daily PB AutoAssigner run** → **Run workflow** → green **Run workflow** button
2. Wait 1–3 min. The run should go green and a digest should land in Slack.
3. From now on it runs **every day at 09:00 Norwegian time** automatically — no computer needed.

**Then switch off the old Mac job** so you don't get double runs/digests:

```bash
launchctl unload ~/Library/LaunchAgents/com.aidn.pb-assigner.plist
```

(Turn back on after the holiday if you ever prefer local runs: same command with `load`.)

## How you'll know it's working while away

- **Every day**: one digest in #productboard-assignment-alerts — assigned counts per PM, plus a ⚠️ list of notes that need a human (low confidence / no clear owner). Those wait safely in the Reviewer tab.
- **If a run fails**: a 🚨 message in the channel ("notater blir IKKE tildelt") + GitHub emails you.
- **No message at all for a day** = something is wrong with Slack or GitHub — check the Actions tab.
- Safety caps still apply: max 20 notes/PM/run, max 200 total — beyond that, everything is held for review and alarmed.

---

## ⚠️ Important: Productboard API v1 shuts down 8 July 2026

The app still uses PB's v1 API. **On 8 July the daily run will start failing** (you'll see the 🚨 alert in Slack) unless the app is migrated to v2 first. The migration plan is ready (`docs/v2_migration_plan.md`, Phase 0 verified) — **do this with Claude before you leave.** It's a contained change in one file (`backend/pb_client.py`).

## If something goes wrong while you're away

Worst case the app just stops assigning — nothing breaks in Productboard, notes simply stay unassigned and the 🚨 alert tells the channel. A colleague can assign manually in PB as before; the app reconciles automatically when it's back.
