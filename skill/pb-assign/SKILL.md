---
name: pb-assign
description: Productboard note-assigner wrapper. Use when the user wants to run the PB assignment pipeline (ingest + classify), view current suggestions, trigger training mode to refine PM scope docs, or open the reviewer UI. Works against a local FastAPI daemon; falls back to the `pb-assigner` CLI if the daemon isn't running.
---

# pb-assign skill

Thin wrapper around the PB_assignerV2 local tool. Picks the right surface (HTTP vs CLI) and surfaces results back to the user concisely.

## Project location

`/Users/jens-aidn/Documents/Koding/work/PB_assignerV2`

## When the user asks for…

| Request | Action |
|---|---|
| "Run PB assigner" / "pull new notes" | `POST http://127.0.0.1:8765/api/run` — or `pb-assigner run` if the daemon is down |
| "Show me today's suggestions" | `GET http://127.0.0.1:8765/api/suggestions` — summarise top 10, call out low-confidence ones |
| "Train the scopes" | `POST /api/train/propose` — show rationale per PM, ask which to apply, then `POST /api/train/apply` per approval |
| "Stats" / "dashboard" | `GET /api/dashboard` — report counts by state and per-PM assignment totals |
| "Verify the email map" | `pb-assigner verify-map` (CLI only — calls PB with each email) |
| "Open the UI" | Tell user to open `http://127.0.0.1:5173` (Vite dev) or `http://127.0.0.1:8765` (prod build) |

## Running the CLI

The backend lives in `backend/`. Always run from the project root with the venv active:

```bash
cd /Users/jens-aidn/Documents/Koding/work/PB_assignerV2
source .venv/bin/activate
python -m backend.cli <subcommand>
```

Subcommands: `init-db`, `ingest`, `classify`, `run`, `status`, `verify-map`, `train`, `serve`.

## Checking whether the daemon is up

```bash
curl -sf http://127.0.0.1:8765/api/health || echo "daemon down"
```

## Training flow (important — human in the loop)

Never auto-apply training proposals. The correct flow is:

1. Call `POST /api/train/propose` — returns a list of proposals, one per eligible PM.
2. For each proposal: show `pm_email`, `rationale_no`, `sample_size`, and whether `changed` is true.
3. Ask the user which proposals to apply.
4. For each approved proposal, `POST /api/train/apply` with `{pm_email, yaml_content, rationale_no, sample_size}`.

## Reporting conventions

When summarising suggestions, lead with **needs-attention** counts (confidence < 0.6) since those are where user attention is actually required. Then briefly list PM-level totals. Don't dump the full table unless asked.

## Assign / skip

The user can assign or skip individual notes through the skill:

- Assign: `POST /api/notes/{note_id}/assign` with `{"pm_email": "..."}`
- Skip: `POST /api/notes/{note_id}/skip`

Confirm the PM email matches a known route from `/api/pms` before posting.

## Hard rules

- Never PATCH Productboard directly from the skill — always go through the daemon so audit logs are recorded.
- Never edit `scopes/*.yaml` by hand from the skill; use `/api/train/apply` so the `scope_versions` history stays complete.
- Autopilot (auto-assigning high-confidence notes without user approval) is NOT in scope for this iteration. If asked, refuse and explain that autopilot is deferred until the daemon moves to a server.
