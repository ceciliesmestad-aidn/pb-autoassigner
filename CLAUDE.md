# CLAUDE.md — PB_assignerV2

Notes for Claude Code sessions working on this project.

## Project one-liner

Productboard note assigner v2. Ingests unassigned PB notes, classifies them with Claude against per-PM YAML scope docs, and surfaces suggestions in a React review UI. Daily via launchd, manual via CLI or the `pb-assign` skill.

## Architecture at a glance

- **Backend** (`backend/`): Python 3.11, FastAPI, SQLite (WAL), Anthropic SDK.
- **Frontend** (`frontend/`): React 18 + Vite + TypeScript + Tailwind + TanStack Query. Dev on :5173 (proxies /api → :8765).
- **Scopes** (`scopes/*.yaml`): per-PM routing docs, Norwegian content. Edited by training mode, versioned in the `scope_versions` table.
- **Schedule**: `launchd/com.aidn.pb-assigner.plist` — daily 07:00.
- **Skill**: `skill/pb-assign/SKILL.md` — Claude Code interface, thin wrapper over HTTP + CLI.

## Important conventions

- UI, code, comments, docs: **English**.
- Note content and scope YAMLs: **Norwegian** (may contain English technical terms). The classifier prompt tells the model not to translate.
- Every DB mutation must go through `backend/pipeline.py` or the explicit helpers in `backend/db.py` so audit rows (assignments + scope_versions) stay complete. Don't bypass with raw SQL.
- Autopilot (auto-PATCH) is deferred. All assignments are human-in-the-loop in this iteration. Do not add autopilot code paths.

## Running

```bash
# First-time setup
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp config.example.toml config.toml       # fill in PB + Anthropic tokens
python -m backend.cli init-db

# End-to-end run (ingest + classify)
python -m backend.cli run

# Dev servers
python -m backend.cli serve --reload     # FastAPI on :8765
cd frontend && npm install && npm run dev  # Vite on :5173
```

## PB API quirks (inherited from v1)

- Base: `https://api.productboard.com`, header `X-Version: 1`
- Paginated `GET /notes?pageLimit=2000&pageCursor=...` — 1-min cursor expiry
- PATCH assign returns **201** on success, not 200/204 — handled in `pipeline.assign_note`
- Rate limit: 50 req/s; we sleep 0.3s between PATCHes by default
- SSL verification disabled by default (corporate proxy) — toggle via `[productboard].ssl_verify`

## Owner→email map

Canonical source: `backend/owners.py`. Mismatches produce 422 errors on PATCH.
Known quirks:
- Sandra: double-a (`otteraaen`)
- Sally Renshaw: PB-side email was flaky in April 2026 — run `pb-assigner verify-map` before a bulk assign.

## Classification prompt

- System block (cached, `cache_control: ephemeral`): global routing + every per-PM scope YAML concatenated.
- User message: JSON array of notes (title, body, tags, company) + a note-id.
- Response: forced via a `classify_notes` tool with strict schema.
- Escalation: any suggestion with confidence < `anthropic.escalate_below` is re-classified one-at-a-time on the Sonnet model.

## Training mode

- Reads `assignments` table for last `training.window_days` (default 90) per PM.
- Skips PMs with fewer than `training.min_notes_per_pm` (default 5) notes.
- Sonnet proposes a full new YAML + a Norwegian rationale.
- User approves per PM — only then does `scope_versions` get written and the file overwritten.

## Tests / fixtures

`tests/test_smoke.py` exercises ingest → classify → assign using mocked PB + mocked Anthropic. Run: `pytest tests/`.

## Where NOT to look for things

- There is no v1 code copied directly. v1 (`../PB_assigner`) is reference only. Domain knowledge from v1's `classifier.py` (routing principles, disambiguations, edge cases) has been moved into `scopes/*.yaml` + `scopes/_global.yaml`.
