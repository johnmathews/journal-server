# W7 — Normalize service (raw → activities/daily, idempotent)

**Date:** 2026-05-09. **Plan:** [docs/fitness-tier-plan.md](../docs/fitness-tier-plan.md) §W7.

## What shipped

1. **`src/journal/services/fitness/normalize.py`** — two free-function entry
   points (`normalize_strava`, `normalize_garmin`) that read raw rows from the
   per-source archive, project them into `fitness_activities` /
   `fitness_daily`, and `INSERT OR REPLACE` so re-runs are idempotent. Plus
   `NormalizeResult` (dataclass) and `NormalizeDriftNotifier` Protocol.
2. **`src/journal/services/fitness/_activity_type_map.py`** — single
   source-of-truth tables for Strava `sport_type` and Garmin `typeKey` →
   coarse `FitnessActivityType` (`run`/`ride`/`swim`/`walk`/`hike`/`strength`/
   `other`). Strava table is verbatim from `fitness-schema.md` §3; Garmin
   table is maintained in code per the schema doc's note ("Garmin's enum is
   less stable than Strava's"). Unknown enum values fall through to `other`.
3. **`src/journal/services/notifications.py`** — added
   `notify_fitness_normalize_drift(source, drift_count)` (admin-only, fires
   ONCE per batch). Mirrors the `notify_health_alert` admin pattern.
4. **`tests/test_services/test_fitness/test_normalize.py`** — 38 tests
   covering all seven plan scenarios (Strava activity normalize across all
   coarse types via parametrize; Garmin daily 6→1 fan-in;
   re-publish authoritativeness; idempotent re-run for both sources;
   drift-skips-row-records-sync-run-fires-once; activity-type mapping edge
   cases for both sources; the dirty-state prod-shaped fixture exercising
   valid + drift + duplicates + re-publish in one pass) plus avg_pace
   derivation, watermark resume, NormalizeResult serialisability, raw_ref_id
   wiring, datetime canonicalisation, drift-without-notifier silent-record,
   and a couple of empty/no-op cases.

Total: 2027 passing (1987 prior + 38 normalize + 2 drift-notify). Lint clean.

## Decisions worth recording

1. **Free functions, not a class.** The plan example shows
   `def normalize_strava(repo, ...)` and `def normalize_garmin(repo, ...)`.
   They are stateless — they take the repo and an optional notifier on each
   call. A service class would add ceremony without buying anything; W8's
   worker will just call the functions.
2. **Drift is internal sentinel `_Drift`, not a public exception.** The W6
   `FitnessNormalizeDrift` (defined in `services/fitness/errors.py`) is the
   *external* contract — but normalize never raises it out of the function.
   Internal `_Drift` is caught by the entry-point loop, counted, and surfaced
   only via `NormalizeResult.drift_count` plus the optional notifier callback.
   Drift never aborts a batch.
3. **`raw_ref_ids_json` lists exactly the contributing-row ids, not the
   whole history.** When Garmin re-publishes a day, normalize picks the row
   with the largest `fetched_at` per `(endpoint, source_id)` and only that
   row's id appears in the daily's `raw_ref_ids_json`. The older row stays in
   raw as audit. Verified by `test_garmin_republish_uses_newest_fetched_at`.
4. **Garmin uses two watermarks.** `max_normalized_fetched_at(kind="daily")`
   and `max_normalized_fetched_at(kind="activities")` are computed
   separately because they project into different normalized tables and
   advance independently. The Strava path uses just one (only activities).
5. **avg_pace derivation lives in normalize, not in the provider.** Strava's
   raw payload doesn't carry pace; the schema needs it for the locked-in
   correlation queries. Normalize derives `seconds_per_km` from
   `moving_time` (or `elapsed_time` fallback) divided by `distance_m / 1000`,
   *only* for `run`/`walk`/`hike`. Cycling/swimming/strength get `None`
   because pace-per-km isn't a meaningful unit there.
6. **One drift sync_run row per batch, one Pushover per batch.** A batch
   with 12 drifts produces a single `fitness_sync_runs` row with
   `notes_json={"drift_count": 12}` and a single Pushover. The plan's fire-
   once semantics. The notifier is optional: passing `None` records the
   sync_run row but doesn't page anyone (the W13 backfill script will use
   this — drift in a backfill is expected and not actionable in real-time).
7. **Required fields per source documented in `_strava_raw_to_activity`
   and `_garmin_raw_to_activity`.** Strava: `id`, `sport_type` or `type`,
   `start_date`, `start_date_local`, `elapsed_time`. Garmin: `activityId`,
   `startTimeGMT` *or* `startTimeLocal`, `duration`. Anything else is
   nullable. The required set is encoded inline because divorcing it into a
   schema would just be ceremony — it's a single function with explicit
   raises.

## What's not done yet

1. **W8 worker integration.** Normalize is just two functions right now;
   nothing in `JobRunner` calls them. W8 wires both fetch and normalize into
   a `fitness_sync_*` job that runs end-to-end.
2. **No live exercise.** Tests use hand-shaped fixtures matching the
   `stravalib.SummaryActivity.model_dump` shape and the documented Garmin
   endpoint shapes. W13 (first live smoke test) is when this code first
   touches real provider responses; any divergence becomes a real bug then.
3. **`extras_json` is empty.** The schema allows source-specific spillover
   fields; we leave it `{}` for now. W13 might surface useful fields to add
   (Strava's `achievement_count`, Garmin's `vo2Max`, etc.) — additive change
   when the need is concrete.

## Tests

- 2027 passed (1987 prior + 38 normalize + 2 drift-notify). 0 failed.
- Lint clean (ruff). Two `# noqa: N818` annotations: `_Drift` (internal
  sentinel) and `_SomethingExotic` (W6 carryover, test fixture for the
  unknown-exception path).

## Pinned

- No new dependencies. Pure orchestration on top of W2 (repo) and the W6
  fetch service's raw rows.
