# Productboard API v1 → v2 migration plan

**Status:** Phase 0 complete. Migration unblocked. Awaiting go-ahead for Phase 1.
**Deadline:** Productboard API v1 is sunset on **8 July 2026**. After that date, the app stops working unless migrated.

---

## Phase 0 findings (verified 2026-04-22 on Cecilie's Mac)

Confirmed against live Aidn workspace using `scripts/v2_scope_check.py` with the existing admin token:

- ✅ **v2 endpoints reachable** — HTTP 200 on `GET /v2/notes`
- ✅ **PII scope present** — `fields.owner.email` returns real PM emails (not `[redacted]`). 42 of 50 scanned notes had owners with readable emails. Insights, Training, and verify-map paths won't break.
- ✅ **Pagination confirmed** — `response.links.next` contains a full next-page URL (cursor embedded). v1's `pageCursor` string is replaced with link-following.
- ⚠️ **`owner[email]` GET filter inconclusive** — filtered and unfiltered pageLimit=50 calls both returned 50 notes. Either Jens Malm genuinely owns 50+ (plausible, he's back-office PM), or the GET filter is silently ignored. Workaround options: use `POST /v2/notes/search`, or filter client-side. Decide during Phase 1.

### Confirmed v2 note shape

```
{
  id, type, createdAt, updatedAt,
  fields: { name, content, tags[], owner, creator, archived, processed },
  links: { self },
  relationships: { data[], links: { next } },
  metadata: { source: { system, recordId } }
}
```

Maps from v1 → v2 that `flatten_note()` must handle:

| v1 path | v2 path |
|---|---|
| `title` | `fields.name` |
| `content` | `fields.content` |
| `tags` | `fields.tags` |
| `owner` | `fields.owner` |
| `displayUrl` | `links.self` |
| `createdAt` | `createdAt` (unchanged) |
| `source.system` | `metadata.source.system` |
| `company.name` | somewhere in `relationships.data[]` — verify during Phase 1 |

---

## Executive summary

Good news first: all of PB AutoAssigner's Productboard traffic goes through **one file** — `backend/pb_client.py` — and we only touch **two endpoints** (list notes, update note). The migration is small and contained.

There's one non-obvious gotcha that needs to be checked **before we write code**: in v2, filtering notes by owner email and even *seeing* owner emails in responses requires a specific OAuth scope (`members:pii:read`). The app's entire classification + training + insights pipelines depend on fetching notes by PM email. If the existing admin API token doesn't carry that scope, we need a new token before any migration work is useful.

Recommended approach below: a 3-step plan starting with a 10-minute scope check on Cecilie's Mac.

---

## What v2 actually changes for us

### 1. Base URL

All paths pick up a `/v2` prefix.

| v1 | v2 |
|---|---|
| `https://api.productboard.com/notes` | `https://api.productboard.com/v2/notes` |
| `https://api.productboard.com/notes/{id}` | `https://api.productboard.com/v2/notes/{id}` |

### 2. Auth header

`X-Version: 1` is v1-specific. In v2 it goes away. The `Authorization: Bearer <token>` header stays.

### 3. `GET /v2/notes` — list notes

**Query parameter renames:**

| v1 | v2 |
|---|---|
| `ownerEmail=...` | `owner[email]=...` (bracket notation) |
| `pageLimit`, `pageCursor` | `pageLimit`, `pageCursor` (unchanged) |

**New filters** we may want to use (optional, but could simplify code):

- `archived=true|false`
- `processed=true|false` — likely the v2 equivalent of "triaged / has been seen". Worth checking whether "unassigned" == "not processed" in Aidn's workspace.
- `createdFrom`, `createdTo`, `updatedFrom`, `updatedTo` (ISO-8601)
- `fields=` — pick which fields come back, for response size

**PII scope requirement (IMPORTANT):**
> Without the `members:pii:read` scope, owner and creator email fields are returned as `[redacted]`. Filtering by `owner[email]` or `creator[email]` requires the `members:pii:read` scope.

This is the single biggest risk. Our app:
- Filters by `owner_email` in `insights.py`, `train.py`, and `cli.py verify-map`
- Reads `note.owner` to detect unassigned notes in `fetch_unassigned()`

If the scope is missing, **all of those break silently** (or with 403s). Must verify before migrating.

### 4. `PATCH /v2/notes/{id}` — update note / assign owner

Body format is **completely different**. v2 supports two styles; we can pick either.

**v1 (today):**
```json
PATCH /notes/{uuid}
{ "data": { "owner": { "email": "pm@aidn.no" } } }
```

**v2, field-update style:**
```json
PATCH /v2/notes/{id}
{ "fields": { "owner": { "email": "pm@aidn.no" } } }
```

**v2, patch-op style:**
```json
PATCH /v2/notes/{id}
{ "patch": [ { "op": "set", "field": "owner", "value": { "email": "pm@aidn.no" } } ] }
```

v2 also supports `clear`, `addItems`, `removeItems`. The `tags` field supports all four ops — directly relevant if we later add the "autoreply-sent" tag for the feedback-reply automation.

**Note on response:** v1 returns 201 on success (a PB quirk we handle in `pipeline.assign_note`). v2's behaviour isn't documented in what I read — need to confirm with a test call that it's still non-standard, or if it's now a normal 200/204.

### 5. Response envelope

The v1 response is `{data: [...], pageCursor: "...", totalResults: N}`. v2 uses cursor pagination too, but the exact wrapper keys aren't fully spelled out in the docs I could read without being logged in. Needs to be checked with a live test call, since `list_notes()` is coded against the v1 envelope.

### 6. Note object fields

The v1 fields we consume — `id`, `title`, `content`, `tags`, `company.name`, `source.system`, `displayUrl`, `createdAt`, `owner` — are likely still present but may be renamed or reshaped. Possibilities the docs hint at:
- `tags` might become array of IDs rather than array of `{name}` objects
- Field names may switch from camelCase (`displayUrl`, `createdAt`) to snake_case or bracket paths
- `owner` may now just be `{email}` or may be nested under a `member` object

This is the second thing that needs a live test call to confirm before we write mapping code.

---

## Call sites in our code

All concentrated in `backend/pb_client.py`. Consumers:

| Caller | Method used | What would break |
|---|---|---|
| `pipeline.fetch_and_classify` | `client.fetch_unassigned()` → `list_notes()` no filter | Queue never populates |
| `pipeline.assign_note` | `client.assign(uuid, email)` → PATCH | Assign button does nothing in PB |
| `insights.py` | `client.list_notes(owner_email=...)` | Insights tab produces empty results |
| `train.py` | `client.list_notes(owner_email=...)` | Training proposes no edits |
| `cli.py verify-map` | `verify_owner_emails()` | Email verification fails silently |

There are no direct `urllib`/`requests` calls to Productboard anywhere else in the codebase — confirmed by grep.

---

## Recommended plan

### Phase 0 — Scope check (10 minutes, before any coding)

On Cecilie's Mac, run two curl commands against the live PB workspace to confirm the existing admin token works for v2 and has `members:pii:read`. This validates or blocks the whole migration.

```bash
# Test 1: Can we read notes at all on v2?
curl -sS -H "Authorization: Bearer $PB_TOKEN" \
  "https://api.productboard.com/v2/notes?pageLimit=1" | head -c 2000

# Test 2: Can we filter by owner email (requires the PII scope)?
curl -sS -H "Authorization: Bearer $PB_TOKEN" \
  "https://api.productboard.com/v2/notes?pageLimit=1&owner%5Bemail%5D=jens.malm@aidn.no" | head -c 2000
```

**Expected outcomes:**
- Test 1 succeeds → v2 endpoints are reachable with the current token. Good.
- Test 2 returns results **with** the owner email filled in → `members:pii:read` scope is present. Proceed.
- Test 2 returns results but with `owner.email` as `[redacted]`, or 403s → need to generate a new v2 API token with the right scopes via PB's admin UI before we can migrate.

This also gives us one real response body to map fields from, which resolves unknowns in section 5 and 6 above.

### Phase 1 — Rewrite `pb_client.py` to v2 (~2 hours of work)

Changes concentrated in one file:

1. Add `API_VERSION` constant (so `v1` vs. `v2` is toggleable for safety — see below).
2. Update `PB_API` base or path prefixes.
3. Remove `X-Version: 1` header.
4. Change `ownerEmail` → `owner[email]` in query params.
5. Change PATCH body shape in `assign()`.
6. Adjust `flatten_note()` for any renamed fields found during phase 0.
7. Adjust `list_notes()` for any envelope changes found during phase 0.
8. Keep the v1 code path alive behind a feature flag so we can A/B test a single call before cutting over.

No changes needed in `pipeline.py`, `insights.py`, `train.py`, `cli.py`, or any frontend code. The surface area stops at `pb_client`.

### Phase 2 — Test on Cecilie's Mac (~30 minutes of her time)

1. Click Fetch notes → check queue populates with real notes.
2. Assign one test note → verify it shows assigned in PB web UI.
3. Insights tab → pick a PM, generate insights → verify results show up.
4. Training tab → propose update for one PM → verify diff appears.
5. CLI: `python -m backend.cli verify-map` → verify no PMs show up as "0 notes".

If any of these fail, we fall back to v1 via the feature flag while we fix, since v1 is still live until July 8.

### Phase 3 — Update CLAUDE.md docs

Once v2 works end-to-end, strip the "v1 quirks" section and replace with v2 notes. Commit the config change that removes the version flag fallback.

---

## Timing

- Phase 0 is the gate. Until the scope question is answered, everything else is speculative.
- Phases 1+2 are a half-day if the scope check passes cleanly.
- Total calendar time: **1 working day for Cecilie + me**, whenever we pick a slot.

No pressure on timing — v1 stays live until **8 July 2026**. My recommendation: do this in a quiet week in May, not the last week of June.

---

## Open questions for Cecilie before we start

1. Does the existing admin Productboard token have `members:pii:read` scope, or do we need to generate a new one? (Phase 0 answers this.)
2. Do you want me to keep v1 selectable via a config flag for a safety net during/after migration, or just cut over cleanly once tests pass?
3. Is it OK to run the Phase 0 curl tests the next time you're at your Mac, so I can produce an exact-shape code plan instead of a "best guess" one?

---

## Sources

- [PB Developer Portal v2 overview](https://developer.productboard.com/v2.0.0/reference/introduction)
- [v2 List notes](https://developer.productboard.com/v2.0.0/reference/listnotes)
- [v2 Update note](https://developer.productboard.com/v2.0.0/reference/updatenote)
- [v2 Search notes](https://developer.productboard.com/v2.0.0/reference/performnotessearch)
- [v2 Authentication / API token](https://developer.productboard.com/v2.0.0/reference/api-token)
- [v2 Pagination](https://developer.productboard.com/v2.0.0/reference/pagination)
- [PB Support: Developer Portal](https://support.productboard.com/hc/en-us/articles/23489122963859-Developer-Portal-API-Documentation)
