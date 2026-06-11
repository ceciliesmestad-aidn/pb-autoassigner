# CLAUDE.md — PB AutoAssigner

This file is read by Claude Code at the start of every session. It covers everything needed to run, use, configure, and develop this app — so Claude can answer any question about it directly.

---

## What this app does

Pulls unassigned notes from Productboard, uses Claude (Haiku + Sonnet) to suggest which PM should own each note based on per-PM scope documents, and shows the suggestions in a review UI. A human confirms each assignment before it's pushed back to Productboard. No autopilot — every assignment is intentional.

**PMs currently in the system:** Line Adde (CPR), Sandra Otteraaen (Treatment), Kristin Shovick (Case Handling), Hanne Linaae (Messaging), Erik Story (Patient), Jens Malm (Back Office), Abraham Guzman (IAM), Ashild Herdlevaer (Collaboration), Sally Renshaw (Design System), Therese Borter (Navigator), Viktor Ernholm (Mobile App), Fredrik Behn (OpenAIdn).

---

## Starting the app

**Easiest way — double-click `Start PB AutoAssigner.command` in Finder.**
macOS will ask to confirm the first time; click Open. Terminal opens, the app starts, and the browser opens automatically at `http://localhost:5173`.

**From a terminal:**
```bash
./launch.sh
```
This sets up the Python venv, installs deps, initialises the database, and starts both servers. Takes ~30 seconds on first run, a few seconds after that.

**If the browser doesn't open automatically:** go to `http://localhost:5173`

**To stop:** press Ctrl+C in the Terminal window.

---

## First-time setup (API keys)

If the app starts but nothing works, the API keys are missing.

1. Open the app → click the **Config** tab
2. Enter the **Productboard token** (found in PB → Settings → Integrations → API keys; starts with `pb_live_…`)
3. Enter the **Anthropic API key** (found at console.anthropic.com → API keys; starts with `sk-ant-…`)
4. Click **Test** next to each to confirm they work
5. Click **Save** — the app reloads automatically, no restart needed

Keys are stored in `config.toml` on disk and never leave the machine.

---

## Day-to-day usage

### Reviewing and assigning notes (Reviewer tab)

1. Click **Fetch notes** — pulls the latest unassigned notes from Productboard and classifies them. Notes that were manually assigned in PB since the last fetch are automatically removed from the queue.
2. Review each suggestion. The confidence score and reasoning are shown.
3. **Assign** to confirm the suggestion, **override** to pick a different PM, or **skip** to leave it open.
4. Notes assigned here are immediately PATCHed to Productboard.

### Adding a new PM

Click the **+** button next to the PM dropdown in the Reviewer tab. Fill in email, name, and team. An initial scope YAML is generated automatically. After saving, commit `pms_custom.json` and the new file in `scopes/` to git.

### Training the classifier (Training tab)

The classifier is driven by scope YAML files in `scopes/`. Training improves them based on real PB data.

1. Select a PM from the list on the left
2. Choose a lookback window (1 / 3 / 6 months)
3. Click **Propose update** — fetches that PM's recent PB notes and asks Claude to suggest edits to their scope YAML. Takes ~30 seconds.
4. Review the diff (current vs. proposed) and the rationale
5. Click **Apply update** if it looks right — writes the file and records the version

After approving updates, commit the changed YAML files in `scopes/`.

### Analysing feedback content (Insights tab)

Per-PM content analysis, independent of classification. Useful for PMs skimming what their users have been saying.

1. Select a PM
2. Pick a time window (1 week / 1 month / 3 months / 6 months)
3. Click **Generate insights** — fetches that PM's PB notes and runs them through Claude once to (a) categorise each note (tender / feedback / bug / feature_request / question / other) and (b) write a Norwegian summary of the non-tender content.

Output: KPI cards (total / feedback / tender / municipalities), a frequency chart (day/week/month buckets depending on window), a note-type breakdown, a top-municipalities list, and the generated summary. Takes ~20–40 seconds depending on note volume.

### Seeing what's happening (Console tab)

Live log tail from the backend. Colour-coded: red = error, amber = warning. Useful when Fetch notes is slow or something fails.

---

## Scope documents

`scopes/*.yaml` — one file per PM, Norwegian content. These are the core of the classifier. They describe what each PM owns, what to exclude, strong keywords, and disambiguation rules.

`scopes/_global.yaml` — cross-PM routing principles (e.g. "domain beats technology", "anbud/tender is not an exclusion reason").

**To fix a wrong assignment:** edit the relevant `scopes/*.yaml` file directly, or use Training mode to propose a data-driven update. After editing, click Fetch notes to re-classify the queue with the new scopes.

**Known email quirks:**
- Sandra Otteraaen: double-a in `otteraaen`
- Kristin Shovick: email is `kristin.shovick@aidn.no` (not `hoiaas` — the old address caused 422 errors on PATCH)

---

## Architecture

- **Backend** (`backend/`): Python 3.11, FastAPI on :8765, SQLite (WAL mode), Anthropic SDK
- **Frontend** (`frontend/`): React 18 + Vite on :5173, TypeScript, Tailwind, TanStack Query. Dev proxies `/api` → :8765.
- **Scopes** (`scopes/*.yaml`): per-PM routing docs, versioned in `scope_versions` DB table
- **Custom PMs** (`pms_custom.json`): PMs added via UI, tracked in git

### What lives where

| Data | Location | In git? |
|------|----------|---------|
| PM registry (builtin) | `backend/owners.py` | ✅ Yes |
| PMs added via UI | `pms_custom.json` | ✅ Yes — commit this |
| Scope YAMLs | `scopes/*.yaml` | ✅ Yes — commit these |
| API keys | `config.toml` | ❌ Gitignored |
| Notes / suggestions / assignments | `data/pb_assigner.db` | ❌ Runtime only |
| Logs | `data/backend.log` | ❌ Ephemeral |

If you re-clone: run `./launch.sh` once, enter keys in Config tab, click Fetch notes — everything rebuilds from PB.

---

## Configuration file

`config.toml` (gitignored, created from `config.example.toml`). The Config tab in the UI writes directly to this file. Key sections:

```toml
[productboard]
token = "pb_live_..."
ssl_verify = false          # keep false behind Aidn's corporate proxy (Zscaler)

[anthropic]
api_key = "sk-ant-..."
ssl_verify = false          # same — needed for corporate proxy
model_default = "claude-haiku-4-5-20251001"
model_escalate = "claude-sonnet-4-6"
escalate_below = 0.6        # re-classify on Sonnet when confidence < this

[training]
window_days = 180           # how far back to look for each PM's notes
min_notes_per_pm = 5        # skip PMs with fewer notes than this
```

---

## Development conventions

- UI, code, comments: **English**
- Note content, scope YAMLs: **Norwegian** (may contain English terms)
- Every DB mutation goes through `backend/pipeline.py` or helpers in `backend/db.py` — keeps audit rows complete
- No autopilot code paths — all assignments are human-in-the-loop

### Running tests

```bash
source .venv/bin/activate
pytest tests/
```

Tests use `FakePBClient` and `FakeAnthropicClient` (in `tests/conftest.py`) — no real API calls.

### Adding a PM in code (vs. UI)

Edit `_BUILTIN_PMS` in `backend/owners.py` and create a matching `scopes/<scope_file>.yaml`. The scope filename is derived from the email local part with dots replaced by underscores.

---

## API endpoints (quick reference)

```
GET  /api/health
GET  /api/setup/status          are keys configured? (masked)
POST /api/setup/save            write keys to config.toml + hot-reload
POST /api/setup/test?service=   probe productboard or anthropic
GET  /api/config                frontend config subset
GET  /api/pms                   PM list (builtin + custom)
POST /api/pms                   add new PM
GET  /api/suggestions           reviewer queue
POST /api/notes/{id}/assign     body: {pm_email}
POST /api/notes/{id}/skip
POST /api/run                   ingest + classify
POST /api/insights?pm_email=&window_days=   per-PM content analysis (categorises notes + Norwegian summary)
GET  /api/scopes/{pm_email}     raw YAML + version history
POST /api/train/propose?pm_email=&window_days=
POST /api/train/apply
GET  /api/logs/tail?lines=N
GET  /api/runs?limit=N
```

---

## Troubleshooting

**App won't start / browser shows "can't connect":**
Use `http://localhost:5173` (not `127.0.0.1`). If that also fails, the servers aren't running — open Terminal and run `./launch.sh`.

**"Productboard token not configured" error:**
Go to Config tab and enter the PB token.

**Assignment fails with 422:**
The PM's email in `backend/owners.py` doesn't match what Productboard has on file. Run `python -m backend.cli verify-map` to check. Known issue: Kristin's email was wrong (fixed to `kristin.shovick@aidn.no`).

**Fetch notes returns 0 new notes:**
All current PB notes are already in the queue or were assigned. This is normal.

**Note assigned to wrong PM repeatedly:**
Edit `scopes/<pm>.yaml` to strengthen or add keywords. Or use Training mode to let Claude propose an update based on real data.

**Rate limit error during training (429):**
The org has a 30k token/minute limit. Train one PM at a time — use the PM selector in the Training tab.

**Corporate proxy / SSL errors:**
Both `ssl_verify = false` flags in `config.toml` (under `[productboard]` and `[anthropic]`) must be set. The Config tab doesn't expose these — edit `config.toml` directly if needed.

---

## Slack notifications & cloud runs (added 2026-06-11)

- **Autopilot is LIVE** (`autopilot_dry_run = false`). High-confidence (≥0.7) suggestions are PATCHed to PB automatically; the rest wait in the Reviewer tab.
- **Slack**: `backend/notify.py` posts a per-run digest + alerts (Norwegian) to **#productboard-assignment-alerts** via an incoming webhook. Config: `[slack]` in `config.toml`, env override `SLACK_WEBHOOK_URL`. Run failures post a 🚨 message. Notifications fire only from `pb-assigner run` (CLI/scheduled), not from UI-triggered runs.
- **Cloud schedule**: `.github/workflows/daily-run.yml` runs daily 07:00 UTC (09:00 CEST) on GitHub Actions using `config.ci.toml` (committed, no secrets) + repo secrets `PB_TOKEN`, `ANTHROPIC_API_KEY`, `SLACK_WEBHOOK_URL`. The SQLite DB is carried between runs via the Actions cache. When Actions is active, the local launchd job should be unloaded to avoid double digests.
- Setup steps: `docs/summer_autopilot_setup.md`.
- **PB API v2 migration done (2026-06-11)**: `api_version = "v2"` in `config.toml` and `config.ci.toml`. v2 uses link-following pagination, `owner[email]` filters, `{fields: {owner: {email}}}` PATCH bodies, and company names resolved via `PBClient.company_names()` (cached id→name map from `/v2/companies`). Fallback: set `api_version = "v1"` (works until the v1 sunset on 2026-07-08). Pending: one live end-to-end verification on Cecilie's Mac (`verify-map` + `run`).
