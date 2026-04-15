# PB_assignerV2

Productboard note assigner. Ingests unassigned notes, classifies them via Claude against per-PM scope docs, and surfaces suggestions in a React review UI. Daily via launchd, manual via CLI or Claude Code skill.

## Quick start

```bash
# Backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp config.example.toml config.toml   # fill in tokens

# Initialise db
python -m backend.cli init-db

# First sync (ingest unassigned notes, classify, store suggestions)
python -m backend.cli run

# Frontend dev server
cd frontend && npm install && npm run dev
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md). Key pieces:

- `backend/pb_client.py` — Productboard API (paginated GET, PATCH assign)
- `backend/classify.py` — Claude API call, prompt-cached scope docs
- `backend/train.py` — reads assigned corpus, proposes scope YAML diffs
- `backend/app.py` — FastAPI serving `/api/*` + the React build
- `scopes/*.yaml` — per-PM scope definitions, edited in place (Norwegian)
- `data/pb_assigner.db` — SQLite: notes, suggestions, assignments, audit
- `frontend/` — React + Vite + TypeScript review UI (English)

## Modes

- `pb-assigner run` — ingest → classify → store suggestions (no PATCH)
- `pb-assigner train` — propose scope-doc updates from recently-assigned notes
- `pb-assigner status` — summary counts
- `pb-assigner verify-map` — sanity-check owner→email map against PB

Autopilot (auto-PATCH high-confidence notes) is **deferred** until the daemon runs on a server. Current flow is always human-in-the-loop via the UI.
