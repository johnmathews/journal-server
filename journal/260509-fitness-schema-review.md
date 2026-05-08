# Fitness schema review — quality pass before implementation

Reviewed `docs/fitness-schema.md` (created earlier today by a separate session) for
correctness and design quality before the implementation phase begins. Verified every cited
file/line reference, cross-checked the schema against the master plan
(`fitness-integration-plan.md`), and read each SQL block looking for correctness issues.

## Citations — all verified

All inline citations against the existing migration set check out:

- `migrations/0006_jobs.sql:18` — `result_json` column
- `migrations/0011_multi_tenant.sql:27` — `user_sessions` table
- `migrations/0011_multi_tenant.sql:150` — `entities.entity_type` CHECK enum
- `migrations/0001_initial_schema.sql:21` — `mood_scores.score` CHECK
- `services/notifications.py:47` — TOPICS list
- `services/notifications.py:91` — `notif_job_success_entity_reembed` (default-off precedent)
- `0023` is the next free migration slot (current latest is `0022`)
- Mood dimension keys (`joy_sadness`, `energy_fatigue`, `frustration`) exist in
  `config/mood-dimensions.toml`; the doc's note that "frustration is the closest proxy to
  stress" is correct (no `stress` dimension exists)

## Issues investigated

### 1. Q3 parameterized window frame — withdrawn after testing

Initially flagged `ROWS BETWEEN (:window - 1) PRECEDING` as a SQLite limitation. Tested
against SQLite 3.50.4 (the project's version); bind parameters and arithmetic in window
frame bounds work fine. No change needed. The doc is correct.

### 2. Q2 `strftime('%Y-%W')` — real bug, fixed

`%W` is "week 00–53, Monday as first day of week" — not ISO 8601. A natural Mon–Sun week
spanning Dec/Jan splits into a `%Y-52`/`%Y-53` partial bucket plus a `%Y-00` bucket,
silently halving the weekly distance for that week. Verified concretely:

| Date         | dow | `%Y-%W` |
|--------------|-----|---------|
| 2025-12-31   | 3   | 2025-52 |
| 2026-01-01   | 4   | 2026-00 |
| 2026-01-04   | 0   | 2026-00 |
| 2026-01-05   | 1   | 2026-01 |

Replaced both CTEs with Monday-of-week date arithmetic
(`date(d, '-' || ((strftime('%w', d) + 6) % 7) || ' days')`). Verified that all of
2025-12-29 through 2026-01-04 bucket to `2025-12-29`, with 2026-01-05 starting the next
bucket.

### 3. `activity_type` enum coarseness — added `source_subtype`

Fetched the current Strava `sport_type` enum from
[https://developers.strava.com/docs/uploads/](https://developers.strava.com/docs/uploads/) —
54 values. The proposed 7-value `activity_type` collapses ~35 into `'other'` (Rowing,
Yoga, AlpineSki, RockClimbing, NordicSki, etc.) and silently loses the
Run/TrailRun/VirtualRun and Ride/GravelRide/MountainBikeRide/EBikeRide distinctions with no
defined normalize-time mapping.

Fix: added `source_subtype TEXT NOT NULL` (verbatim source string) alongside the coarse
`activity_type`, plus a normalization-mapping table. Coarse `activity_type` still serves
the locked-in correlation queries (Q2 filters on `'run'` which now explicitly includes
TrailRun and VirtualRun); `source_subtype` preserves fidelity for any future query that
needs the distinction.

### 4. Daily authoritativeness rule — added

`fitness_daily.raw_ref_ids_json` referenced raw rows but the doc didn't specify what to do
when the same `(user_id, endpoint, source_id)` had multiple raw rows from Garmin
re-publishing a corrected payload. Added a paragraph specifying largest-`fetched_at`-wins
per `(endpoint, source_id)`, with `raw_ref_ids_json` holding only the authoritative row
per endpoint.

### 5. Source-aware integrity check — added

The integrity-check sketch in §6 said "LEFT JOIN against the source-appropriate raw table"
but glossed the fact that `fitness_raw_strava` and `fitness_raw_garmin` have independent
`AUTOINCREMENT` sequences — a join that ignores `source` could silently match the wrong
table by `id` collision. Replaced with two explicit source-scoped queries.

## Cosmetic edits applied

- `fitness_daily.source` enum reordered to `('strava', 'garmin')` for consistency with
  the other tables.
- `fitness_sync_runs.started_at` got a `DEFAULT (strftime(...))` to match house style.
- Added a paragraph noting that `fitness_auth_state.updated_at` is app-managed (no
  triggers in this schema).
- Added a §5 paragraph cross-linking to master plan D5 explaining the two new
  notification topics extend rather than contradict the plan.
- Added a §9 entry noting `fitness_sync_runs` is intentionally unbounded at personal scale.

## Outcome

Doc grew from 466 to 545 lines. Section structure preserved (§§ 1–9). No changes proposed
to the underlying decisions — all edits either fix concrete correctness issues (Q2 week
bug, source-blind integrity join) or add detail that was implicit (subtype preservation,
authoritativeness rule, app-managed `updated_at`).

The doc is ready for implementation. The first migration (`0023_fitness_auth_and_sync.sql`)
is the next concrete step.
