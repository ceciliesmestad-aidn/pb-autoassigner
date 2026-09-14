# CLAUDE.md — PB AutoAssigner

This file is read by Claude Code at the start of every session. It covers everything needed to run, use, configure, and develop this app — so Claude can answer any question about it directly.

---

## What this app does

Pulls unassigned notes from Productboard, uses Claude (Haiku + Sonnet) to suggest which PM should own each note based on per-PM scope documents, and shows the suggestions in a review UI. A human confirms each assignment before it's pushed back to Productboard. No autopilot — every assignment is intentional.

**PMs currently in the system (updated 2026-09-14):** Line Adde (CPR), Sandra Otteraaen (Treatment), Kristin Shovick (Case Handling), Hanne Linaae (Messaging), Erik Story (AI & Automation), Fredrik Pedersen (Patient), Jens Malm (Back Office — economy/plassadmin/hjelpemidler only), Abraham Guzman (IAM), Ashild Herdlevaer (Collaboration), Sally Renshaw (Design System), Therese Borter (Navigator), Viktor Ernholm (Mobile App), Fredrik Behn (OpenAIdn), plus custom PMs in `pms_custom.json`: Cathrine Stenstadvold (Product Leadership — notatblokk/egne notater/huskelapp), Séamus Beirne (Data & Analytics — all rapportering/analyse/statistikk/KOSTRA/Aidn Analytics, moved from Jens 2026-08-12), Adil Rashid (Health Station — helsestasjon/skolehelsetjeneste, piloting), Knut Ole Sjøli (Infrastructure — devbox / smoke test / test+Zendesk noise during the Zendesk rollout, added 2026-09-14), Magne Davidsen (Data Migrations — legacy-EPJ onboarding migration + ongoing migration tooling/data quality, also takes test+migrering feedback that isn't devbox/Zendesk, added 2026-09-14).

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

### Zendesk rollout testing noise → Knut Ole (added 2026-09-14)

While Zendesk is being integrated as the new support tool, a lot of test/devbox/smoke-test
notes are landing in Productboard. These are routed to **Knut Ole Sjøli** (`knut.sjoli@aidn.no`,
Team Infrastructure, `scopes/knut_ole_sjoli.yaml`) rather than to a domain PM or left open:

- Any note mentioning **devbox**
- Any note mentioning **smoke test** / **smoketest**
- Any note mentioning **both "test" and "zendesk"** (anywhere in title/body, any order)

This is a deliberate exception to the "domain beats technology" principle in `_global.yaml` —
it's keyword/technology-based routing, not domain-based. It also takes priority over the
existing `_global.yaml` exclude_rule that otherwise leaves Aidn-internal "test" notes
unassigned (see the `unless:` note added to that rule) — a note matching devbox/smoke
test/test+zendesk goes to Knut Ole instead of being left open.

Temporary in nature: revisit once the Zendesk rollout stabilizes and this stops being
a meaningful chunk of volume.

**Priority when a note mentions migration-related testing too (added 2026-09-14):**
Magne Davidsen (`magne.davidsen@aidn.no`, Team Data Migrations, `scopes/magne_davidsen.yaml`)
owns data-migration testing feedback that does **not** mention devbox or Zendesk:

1. Note mentions **devbox** or **zendesk** → Knut Ole, regardless of migration wording.
2. Note mentions **test + migrering/datamigrering**, but no devbox/zendesk → Magne.
3. Neither → normal domain classification / the global "leave open" test exclusion.

Magne also owns the normal domain scope for Team Data Migrations: legacy-EPJ data
migration at municipality onboarding (CosDoc/Gerica/Profil/SFM → Aidn) and ongoing
migration tooling/data-quality work, independent of the test-routing rule above.

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

## Tender commitment board watchdog

Added 2026-09-07 after Pål found municipalities missing from the Tender Commitment board's filter and columns.

**The problem it solves.** The documented routine ([Managing tender requirements as insights in Productboard](https://app.notion.com/p/328a4942fc99809baf7ed5f671692932), Notion → Ways of Working) has four steps: tender team logs requirements as Notes → AutoAssigner routes to the PM → PM links the insight to a feature → Product Director fills in the Committed Deadline. Step 4 assumes a `<Tender> - Committed deadline` DATE field already exists and that the board has a matching column and filter. **No step and no owner creates those.** That's why tenders quietly fall off the board.

**What is and isn't automatable.** Productboard API v2 has no board/prioritization endpoints at all, and no endpoint for creating a custom *field definition* (only for creating *values* of an existing select field). So:

| Step | Automatable |
|------|-------------|
| Requirements in as insights | ✅ notes API |
| Commitment date on the feature | ✅ entities API, *if the field exists* |
| Add board filter | ❌ UI only |
| Add board column | ❌ UI only — and the field itself can't be created via API either |

The watchdog therefore **never writes to Productboard**. It reads, compares, and posts the exact click-path to Slack. A test (`test_watchdog_never_writes_to_productboard`) enforces this structurally.

**Running it:**

```bash
source .venv/bin/activate
python -m backend.cli tender-watchdog              # check + post to Slack
python -m backend.cli tender-watchdog --no-slack   # check, print only
python -m backend.cli tender-watchdog --raw        # dump PB's raw field config
```

**Cloud schedule:** `.github/workflows/tender-watchdog.yml`, Mondays 06:00 UTC (08:00 CEST), one hour after the daily assignment run. Reuses the existing `PB_TOKEN` and `SLACK_WEBHOOK_URL` secrets — no Anthropic key, no database. Posts to whatever channel the webhook points at (currently **#support-productboard**, per the decision on 2026-09-07; the Notion routine already directs PB edge cases there). Findings exit 0; only an inconclusive run exits non-zero, so the badge going red always means "the check itself broke".

**Findings it produces:**

| Kind | Meaning |
|------|---------|
| `MISSING_DEADLINE_FIELD` | Tender has requirements but no date field → the real board gap. Alert. |
| `MISSING_TENDER_OPTIONS` | Date field exists but no `T -`/`S -` option → tender team hasn't logged it. Alert. |
| `NAMING_DRIFT` | Matched only via the alias table, or field is off-convention. **Report only** — renaming a field affects every board using it as a column. |
| `PREFIX_ANOMALY` | Option prefix is neither `T` nor `S`. Info. |
| `INCOMPLETE_PAIR` | `T` without `S` or vice versa — the soft-commitment gap raised 2026-08-28. |

**Naming conventions it assumes:** field `<Tender> - Committed deadline`; options `T - <Tender>` (hard/krav) and `S - <Tender>` (soft). Known legitimate name differences live in `TENDER_ALIASES` in `backend/tender_watchdog.py` — add an entry there when a tender is deliberately named differently on the two sides, otherwise it shows up as a false alert.

**State on 2026-09-07** (from the live workspace, 15 tenders with options vs 13 date fields):

- Missing a committed-deadline field entirely: **Bergen** (BAFO folder created 2026-08-20), **Hadsel**, **Bø, Øksnes, Askøy, Løddingen**
- Date field with no tender options: **Romerike & Follo** (field is also misnamed `- Committed date`)
- Naming drift: `Innlandet`↔`Digi Innlandet`, `Kongsberg`↔`Kongsbergregionen`, `Lofoten`↔`Lofoten IKT`, `Ullenvang` (typo)↔`Ullensvang`, `Ulvik og Eidsfjord`↔`Ulvik & Eidfjord`
- Prefix anomalies: `Bergen` (no prefix), `B - Bodø` (should be `T -`), which also leaves Bodø with no hard-requirement option

**⚠️ Pending one live verification.** The sandbox this was written in cannot reach `api.productboard.com`, so `feature_field_config()` and `select_field_values()` were written against the documented v2 shape with defensive unwrapping (`_unwrap_fields` / `_unwrap_options` in `pb_client.py`, each covering four payload variants). Run `python -m backend.cli tender-watchdog --raw` once on Cecilie's Mac and confirm the field descriptors come back with `name` and `type`/`fieldType`; tighten the unwrappers if the shape differs. Until that's done, treat an empty report as unverified rather than clean.

**Not yet built (phase 2):** the BAFO-to-PB skill — point it at the tender's Drive folder, it extracts requirements, proposes notes + feature links + commitment dates, and writes only what a human confirms. Decided 2026-09-07 that requirement→feature linking is **always reviewed**, never auto-written. Note that the Drive side can't run in GitHub Actions (no Google auth there) — that half belongs in Cowork with the Drive connector.

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
- **Team tag on assignment (2026-08-12)**: every assignment (autopilot and Reviewer) also adds the PM's team name as a tag on the PB note (e.g. "Team Data & Analytics"), via v2 patch op `addItems` (appends, never overwrites existing tags; PB dedupes). Tag names reuse the existing PB workspace tags — registry names that differ are mapped in `_PB_TEAM_TAG_OVERRIDES` in `backend/owners.py` (Team CPR → "Team Core Patient Record", Team AI & Automation → "Team Automation", Team OpenAIdn → "Team Open Aidn", Design System → "Team Design System"). Config: `tag_team_on_assign` under `[classifier]` (default true). Best-effort — a failed tag call is logged as a warning but never breaks the assignment.
- **Tender commitment board watchdog (added 2026-09-07)**: weekly read-only check that every tender with requirements in PB also has a `<Tender> - Committed deadline` field — and therefore *can* appear on the [Tender Commitment board](https://aidn.productboard.com/prioritization/MTpQcmlvcml0aXphdGlvbkJvYXJkOjFkM2FhMDNmLTFmZDUtNDA3Mi05OGQ1LTM1NGRhYTgyOWVkMA==). See "Tender commitment board watchdog" below.
- **Slack deep links fixed (2026-07-02)**: note deep links now come straight from the v2 response — PB added `links.html` (exact UI URL, `…/insights/feedback?d=notes%2F<numeric-id>`) to note responses on 2026-04-30. The old workaround (`display_urls_from_v1`, a v1 scan to recover numeric IDs) was removed; it had stopped resolving URLs and v1 sunsets 2026-07-08 anyway. Fallback chain if `links.html` is ever missing: workspace All-Feedback page → `links.self`. `workspace = "aidn"` added to `config.toml`/`config.example.toml` (was previously only in `config.ci.toml`). Queue notes get their URLs refreshed automatically on the next run (db.py always refreshes `display_url`).
