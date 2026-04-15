# CLAUDE.md — PB_assignerV2

Notes for Claude Code sessions working on this project.

## Project one-liner

Productboard note assigner v2. Ingests unassigned PB notes, classifies them with Claude against per-PM YAML scope docs, and surfaces suggestions in a React review UI. Daily via launchd, manual via CLI or the `pb-assign` skill.

## Architecture at a glance

- **Backend** (`backend/`): Python 3.11, FastAPI, SQLite (WAL), Anthropic SDK.
- **Frontend** (`frontend/`): React 18 + Vite + TypeScript + Tailwind + TanStack Query. Dev on :5173 (proxies /api → :8765).
- **Scopes** (`scopes/*.yaml`): per-PM routing docs, Norwegian content. Edited by training mode, versioned in the `scope_versions` table.
- **Custom PMs** (`pms_custom.json`): PMs added via the UI — tracked in git alongside scopes.
- **Schedule**: `launchd/com.aidn.pb-assigner.plist` — daily 07:00.
- **Skill**: `skill/pb-assign/SKILL.md` — Claude Code interface, thin wrapper over HTTP + CLI.

## Important conventions

- UI, code, comments, docs: **English**.
- Note content and scope YAMLs: **Norwegian** (may contain English technical terms). The classifier prompt tells the model not to translate.
- Every DB mutation must go through `backend/pipeline.py` or the explicit helpers in `backend/db.py` so audit rows (assignments + scope_versions) stay complete. Don't bypass with raw SQL.
- Autopilot (auto-PATCH) is deferred. All assignments are human-in-the-loop in this iteration. Do not add autopilot code paths.

## Running

```bash
# First-time setup (or use launch.sh which does all of this)
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp config.example.toml config.toml   # fill in tokens (or use .env)
python -m backend.cli init-db

# Quickest start — sets up venv, installs deps, starts both servers
./launch.sh

# Backend only (with hot reload)
./launch.sh backend

# End-to-end pipeline run via CLI
python -m backend.cli run

# Dev servers separately
python -m backend.cli serve --reload   # FastAPI on :8765
cd frontend && npm run dev             # Vite on :5173
```

Tokens can live in `.env` (gitignored) as `PB_TOKEN=...` and `ANTHROPIC_API_KEY=...`; `launch.sh` loads them automatically.

## Persistence — what lives where

| Data | Location | In git? |
|------|----------|---------|
| PM registry (builtin) | `backend/owners.py` | ✅ Yes |
| PMs added via UI | `pms_custom.json` (project root) | ✅ Yes — **commit this** |
| Scope YAMLs | `scopes/*.yaml` | ✅ Yes — **commit these** |
| Scope version history | `data/pb_assigner.db` (SQLite) | ❌ No — runtime |
| Notes / suggestions / assignments | `data/pb_assigner.db` | ❌ No — runtime |
| Run history / logs | `data/backend.log`, `data/` | ❌ No — ephemeral |

If you re-clone, run `./launch.sh` once — the pipeline will re-ingest from PB and rebuild `data/` in one run. The scope YAMLs and custom PMs are the valuable persistent config; everything else is derivable.

## Corporate proxy / SSL

Both the PB client (urllib) and the Anthropic SDK (httpx) have `ssl_verify = false` set by default in `config.example.toml`. This bypasses Zscaler/Netskope TLS interception. The Anthropic client is built in `backend/classify.py:build_anthropic_client()` which passes `httpx.Client(verify=False)` when disabled. Toggle per-client in `config.toml` under `[productboard]` and `[anthropic]`.

## PM registry

Canonical source: `backend/owners.py` for builtin PMs, `pms_custom.json` for UI-added PMs.

Always use `owners.get_all()` — never `owners.PMS` directly — so custom PMs are included.
The `BY_EMAIL` dict and legacy `PMS` list remain for backward compat but only cover builtins.

Known quirks:
- Sandra: double-a (`otteraaen`)
- Sally Renshaw: PB-side email was flaky in April 2026 — run `python -m backend.cli verify-map` before a bulk assign.

Adding a PM via the UI (`+` button in Reviewer) writes to `pms_custom.json`, creates the scope YAML in `scopes/`, and records the initial `scope_versions` row. Commit both files afterwards.

## PB API quirks (inherited from v1)

- Base: `https://api.productboard.com`, header `X-Version: 1`
- Paginated `GET /notes?pageLimit=2000&pageCursor=...` — 1-min cursor expiry
- PATCH assign returns **201** on success, not 200/204 — handled in `pipeline.assign_note`
- Rate limit: 50 req/s; we sleep 0.3s between PATCHes by default
- No server-side "unassigned" filter — `fetch_unassigned()` fetches all and filters client-side

## Classification

- `backend/classify.py` — `Classifier` class, `build_anthropic_client()` factory.
- System block (prompt-cached, `cache_control: ephemeral`): global routing + all per-PM scope YAMLs concatenated. Loaded via `scopes_loader.load_all()` — uses `owners.get_all()` so custom PMs are included.
- User message: JSON array of notes (title, body, tags, company, note_id).
- Response: forced via a `classify_notes` tool (strict JSON schema — no freeform parsing).
- Escalation: notes with confidence < `anthropic.escalate_below` are re-classified individually on Sonnet. Log lines show `escalating note X (conf=Y.YY)`.
- Token cache hits show up as `cache_read=NNNN` in the INFO logs — expected after the first batch.

## Training mode

- Primary data source: **live PB notes owned by each PM** (`pb_client.list_notes(owner_email=…)`) filtered to the last `training.window_days` days (default 180 / ~6 months). Works on day one — no need to accumulate app assignments first.
- Secondary signal: override assignments from the local DB are grafted in as bonus context (these are the "the classifier was wrong" corrections — highest training value).
- Skips PMs with fewer than `training.min_notes_per_pm` (default 5) notes in the window.
- Sonnet proposes a full new YAML + a Norwegian rationale via the `propose_scope_update` tool.
- User approves per PM in the Training tab — only then does `scope_versions` get written and the file overwritten.
- `train.propose_scope_updates()` requires a `pb` argument (PBClient) to fetch live notes. Tests pass `pb=None` to fall back to the DB assignments path.

## Frontend pages

- **Reviewer** — note queue with PM filter, confidence filter, bulk-select, assign/override/skip per row. `+` button next to PM dropdown opens the Add PM modal.
- **Dashboard** — state counts, per-PM assignment bar charts (7d/30d), confidence histogram, weekly volume.
- **Training** — "Propose updates" triggers live PB fetch + Sonnet proposals; side-by-side YAML diff with approve button.
- **Console** — live log tail (`/api/logs/tail`, polls every 2s) + recent runs table (`/api/runs`, polls every 5s). Color-coded: red=error, amber=warn, green=our loggers. Link from the Run-failed toast.

## Key API endpoints

```
GET  /api/health
GET  /api/config                  safe config subset for frontend
GET  /api/pms                     PM list (builtin + custom)
POST /api/pms                     add new PM (creates scope file + pms_custom.json entry)
GET  /api/pms/scope-template      blank YAML template pre-filled with email/name/team
GET  /api/suggestions             reviewer queue (filter: pm, confidence range)
GET  /api/notes/{id}              full note + suggestion + assignment history
POST /api/notes/{id}/assign       body: {pm_email}
POST /api/notes/{id}/skip
POST /api/run                     trigger ingest + classify in-process
GET  /api/dashboard               aggregate stats
GET  /api/scopes                  list scope files + combined hash
GET  /api/scopes/{pm_email}       raw YAML + version history
POST /api/train/propose           fetch PB notes per PM → Sonnet proposals
GET  /api/train/readiness         per-PM assignment counts vs. min_notes_per_pm threshold
POST /api/train/apply             write approved YAML + scope_versions row
GET  /api/logs/tail?lines=N       last N lines of data/backend.log
GET  /api/runs?limit=N            recent run rows (kind, started/finished, stats)
```

## Tests / fixtures

`tests/test_smoke.py` exercises ingest → classify → assign → train using `FakePBClient` and `FakeAnthropicClient` (both in `tests/conftest.py`). Run: `pytest tests/`.

- Tests set `ssl_verify=True` in the Anthropic config fixture to skip the httpx client path (the fake lambda doesn't accept `http_client`).
- `FakePBClient` deep-copies `SAMPLE_PB_NOTES` on init to prevent cross-test mutation.

## Logging

`backend/app.py:_configure_logging()` installs a `RotatingFileHandler` on the root logger at startup, writing to `data/backend.log`. Uvicorn's loggers are forced to propagate to root so everything lands in one file. The Console tab tails this file live.

Log conventions (all `backend.*` loggers):
- `ingest:` prefix for pipeline ingest steps
- `classify:` prefix for classifier steps (batch N/M, escalation decisions, token cache hits)
- `training:` prefix for training proposals

## Where NOT to look for things

- There is no v1 code copied directly. v1 (`../PB_assigner`) is reference only. Domain knowledge from v1's `classifier.py` (routing principles, disambiguations, edge cases) has been moved into `scopes/*.yaml` + `scopes/_global.yaml`.
