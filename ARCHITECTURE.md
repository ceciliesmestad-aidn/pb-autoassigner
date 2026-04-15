# Architecture

## Data flow

```
                       ┌──────────────────────────────────────────────┐
                       │  Productboard API                            │
                       └──────────────▲─────────────────────▲─────────┘
                                      │ GET /notes          │ PATCH /notes/{id}
                                      │                     │
   ┌──────────────────────┐    ┌──────┴────────┐    ┌───────┴──────┐
   │  launchd (daily)     │───▶│  pb_client.py │    │  pb_client.py │
   │  or CLI / skill      │    └──────┬────────┘    └───────▲───────┘
   └──────────────────────┘           │                     │
                                      ▼                     │
                              ┌───────────────┐             │
                              │    db.py      │◀────────────┤
                              │  SQLite       │             │
                              └──────┬────────┘             │
                                     │                      │
                     ┌───────────────┴─────────┐            │
                     ▼                         ▼            │
              ┌──────────────┐          ┌──────────────┐    │
              │ classify.py  │──────────│  scopes/*.yaml│   │
              │ (Claude API) │          └──────────────┘    │
              └──────┬───────┘                              │
                     │                                      │
                     ▼                                      │
              ┌──────────────┐                              │
              │    db.py     │                              │
              │ suggestions  │                              │
              └──────┬───────┘                              │
                     │                                      │
                     ▼                                      │
              ┌──────────────┐      ┌──────────────────┐    │
              │   app.py     │◀─────│  React frontend  │────┘
              │  (FastAPI)   │      │  reviewer / dash │
              └──────────────┘      │  / training      │
                                    └──────────────────┘
```

## Tables (SQLite)

- `notes` — raw PB notes, deduped by content hash
- `suggestions` — classifier output per note (versioned per classify run)
- `assignments` — audit log of PATCH outcomes
- `scope_versions` — historical per-PM scope YAML, for diffing

## Classification prompt structure

Cached system block (stable between calls):
1. Instructions in English: you classify Productboard notes into PM routes
2. The PM → team map
3. Routing principles (ported from v1: domain > tech, reporting → Jens, etc.)
4. Concatenated scope YAMLs, one per PM, written in Norwegian

Per-call user message:
- One or more notes (title + stripped content + tags + company)
- Response schema: `[{note_id, pm_email, confidence (0-1), reasoning (short, Norwegian or English)}...]`

Escalation: if top suggestion's confidence < `escalate_below`, re-run that single note on Sonnet.

## Training loop

For each PM with ≥ N assigned notes in the last window_days:
1. Load current scope YAML
2. Ask Claude: "Here are the last N notes routed to this PM and the current scope doc. Propose a minimal diff to the YAML that better describes this PM's actual scope. Output unified diff or full replacement YAML + rationale."
3. Show diff in UI → approve → write new `scope_versions` row + overwrite `scopes/<pm>.yaml`

No model weights. No fine-tuning. The "memory" of past classifications lives in the scope YAMLs themselves.

## Why local-first

- Laptop-only 24h cadence is acceptable for manual-review workflow — missing a day is fine
- Daemon is already HTTP; moving to a server = deploy the same FastAPI app behind a public URL
- SQLite is single-file, easy to copy to a server later
- Prompt caching requires the system block to stay identical across calls — that's easier when one process owns it
