# Autopilot rollout plan

**Status:** Backend + frontend built and tested. Awaiting Phase B start (dry-run on Cecilie's Mac).
**Goal:** Move from "every assignment reviewed by hand" to "high-confidence assignments handled overnight, only edge cases need review."

---

## What changed

The app gained an autopilot pipeline that runs after the daily classify step. It picks notes whose suggestion confidence is at or above `autopilot_min_confidence` and PATCHes them straight to Productboard with `assigned_by='autopilot'` in the audit table. Two safety caps protect against runaway misclassification (per-PM cap and total cap). Everything that doesn't auto-assign — mid-confidence, low-confidence, leave-opens — stays on the Reviewer tab exactly as before.

A new "Recent autopilot" tab in the UI shows what was decided in the last 24 h / 3 d / 1 wk so you can spot-check overnight decisions and override anything that looks wrong.

The Manual / Autopilot toggle in the header now writes through to `config.toml` — flipping it actually changes what the next launchd run does.

---

## Defaults

These live in `config.toml` under `[classifier]`. The values shipped are conservative:

```toml
autopilot_enabled         = false   # master switch — flip via UI toggle
autopilot_min_confidence  = 0.9     # only ≥0.90 auto-assigns
autopilot_per_pm_cap      = 20      # max notes auto-assigned to ONE PM per run
autopilot_total_cap       = 200     # max notes auto-assigned across ALL PMs per run
```

**Per-PM cap = 20** — if a single run wants to give one PM more than 20 notes, the first 20 go through and the rest stay queued for review. Catches scope-rule mistakes that route everything to one person.

**Total cap = 200** — if a single run wants to auto-assign more than 200 notes overall, the entire batch is held back and a warning is logged. This is the "something is very wrong" tripwire — for example if classification confidence got skewed and everything looks high-confidence.

Both caps are configurable in `config.toml` if volume changes.

---

## Three-phase rollout

### Phase A — already running

Daily launchd job at 09:00 fetches new PB notes and classifies them. `autopilot_enabled=false`. Every note shows up on the Reviewer tab for manual confirmation. **Nothing changes here yet.**

### Phase B — dry-run (1 week minimum)

Goal: see what autopilot *would* do without it actually touching Productboard. Audit rows are written so the Recent Autopilot tab fills up, but every row shows a `dry-run` badge and `pb_status` is null.

Steps:

1. **Pull the latest code on your Mac:**
   ```bash
   cd ~/Documents/Claude/Projects/PB/PB_assignerV2
   git pull
   ```

2. **Reinstall dependencies (only needed once after the new code lands):**
   ```bash
   ./launch.sh
   ```
   Wait for the browser to open. Confirm the new "Recent autopilot" tab is in the nav. Stop the app with Ctrl+C.

3. **Turn autopilot on in `config.toml`:**
   Open the app (`./launch.sh`), click the **Autopilot** side of the toggle in the header. The toggle writes `autopilot_enabled = true` to `config.toml`. Stop the app again with Ctrl+C.

4. **Edit the launchd plist to add `--dry-run`:**
   Open `launchd/com.aidn.pb-assigner.plist` and change the `ProgramArguments` block from:
   ```xml
   <string>backend.cli</string>
   <string>run</string>
   ```
   to:
   ```xml
   <string>backend.cli</string>
   <string>run</string>
   <string>--dry-run</string>
   ```

5. **Reinstall the launchd job:**
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.aidn.pb-assigner.plist 2>/dev/null
   cp launchd/com.aidn.pb-assigner.plist ~/Library/LaunchAgents/
   launchctl load   ~/Library/LaunchAgents/com.aidn.pb-assigner.plist
   ```

6. **Trigger one run manually so you don't have to wait until 09:00:**
   ```bash
   launchctl start com.aidn.pb-assigner
   tail -f ~/Library/Logs/pb-assigner.log
   ```
   Press Ctrl+C when you see the run finish.

7. **Open the app and check the Recent Autopilot tab.** You should see rows with the amber `dry-run` badge. Productboard should be untouched (open one of the notes in PB to confirm it's still unassigned).

**For the next ~7 days, every morning:**
- Open the app
- Recent Autopilot tab → "Last 24 h"
- Read down the list. For each row, ask: "would I have assigned this the same way?"
- If yes — do nothing. If no — use the Reassign dropdown to pick the right PM (this creates a real assignment that overrides the dry-run row).
- Note any patterns where autopilot was wrong → those go into Training mode to fix the scope YAMLs.

**You're ready for Phase C when** a full week has passed AND zero rows in the last 7 days needed reassignment AND no scope YAML edits were triggered by autopilot mistakes.

### Phase C — live autopilot

Same as Phase B but with `--dry-run` removed from the plist.

1. **Edit the plist again, remove the `<string>--dry-run</string>` line.**
2. **Reinstall:**
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.aidn.pb-assigner.plist
   cp launchd/com.aidn.pb-assigner.plist ~/Library/LaunchAgents/
   launchctl load   ~/Library/LaunchAgents/com.aidn.pb-assigner.plist
   ```
3. **Optional: trigger one run** to confirm a real PATCH lands in PB:
   ```bash
   launchctl start com.aidn.pb-assigner
   ```

After this, every morning's Recent Autopilot tab shows green `patched` badges instead of amber `dry-run`. The workflow stays the same — skim the list, override anything wrong.

---

## Rolling back

If autopilot does something you don't trust, flip the **Manual** side of the toggle in the header. That writes `autopilot_enabled = false` to `config.toml`, and the next launchd run is back to classify-and-queue (no PATCHes). Notes already on the Reviewer tab are unaffected. No data is lost — every autopilot decision is in `assignments` with `assigned_by='autopilot'`, joinable to `notes` for full context.

To roll back a *single* autopilot decision after the fact, open Recent Autopilot and use the Reassign dropdown — this fires a fresh PATCH with the new PM, which Productboard treats as a normal reassignment.

---

## What to watch for during dry-run

Things that should look right:
- Rows are evenly spread across PMs, not 90% to one person.
- Confidence scores all ≥0.90.
- Reasoning text mentions concrete keywords from the scope YAML.

Red flags:
- Same PM getting 15+ rows in a row → scope YAML is too greedy, narrow it via Training mode.
- Confidence is 0.90 but the reasoning is vague ("matches general scope") → tighten the scope.
- A note that should be a leave-open got auto-assigned → confirm `pm_email` was actually returned (not None) and either tighten the disambiguation rules or raise `autopilot_min_confidence` to 0.92.

---

## Where each piece lives

| Concern | Location |
|---|---|
| Master switch + thresholds | `config.toml` → `[classifier]` |
| Autopilot pipeline | `backend/pipeline.py` → `auto_assign_high_confidence` |
| Audit query for the UI | `backend/db.py` → `recent_autopilot_assignments` |
| API endpoints | `backend/app.py` → `/api/setup/set-autopilot`, `/api/recent-autopilot` |
| CLI flags | `backend/cli.py run [--dry-run] [--no-autopilot]` |
| Daily schedule | `launchd/com.aidn.pb-assigner.plist` |
| Frontend tab | `frontend/src/pages/RecentAutopilot.tsx` |
| Mode toggle | `frontend/src/App.tsx` → `ModeToggle` |
| Tests | `tests/test_autopilot.py` (7 cases — circuit breakers, dry-run, leave-opens) |

---

## Future work (out of scope for this rollout)

- **Slack notifications** — deferred to Phase 2. Idea: post a daily summary to a Slack channel ("autopilot assigned 14 notes this morning, 0 errors, 2 needed your review").
- **Per-PM thresholds** — if one PM's classifications are reliably tighter than others, allow per-PM `autopilot_min_confidence`. Not needed yet.
- **Self-healing on PB 422** — if a PATCH fails because PB doesn't recognize the email, queue the note for manual review automatically and surface a banner. Today it just logs.
