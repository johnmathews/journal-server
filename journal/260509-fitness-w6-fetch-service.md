# W6 — Fetch service + sync-run state machine + alerting

**Date:** 2026-05-09. **Plan:** [docs/fitness-tier-plan.md](../docs/fitness-tier-plan.md) §W6.

## What shipped

1. **`src/journal/services/fitness/errors.py`** — `FitnessError` /
   `FitnessAuthError` / `FitnessTransientError` / `FitnessNormalizeDrift`. Workers
   (W8), API (W9), and MCP tools (W10) depend only on this module — the fetch
   service catches `StravaAuthError` / `GarminAuthError` and re-raises them as
   `FitnessAuthError` so the SDK type names never leak to downstream callers.
2. **`src/journal/services/fitness/fetch.py`** — `StravaFetchService` and
   `GarminFetchService`, sharing a private `_FetchServiceBase` for the
   lifecycle (single-run guard → load auth → start_run → fetch → classify →
   transition auth → notify → finish_run). Adds `FitnessSyncResult` dataclass
   and `FitnessNotifier` Protocol so the worker can serialise outcomes via
   `dataclasses.asdict` and the notification dependency stays narrow.
3. **`src/journal/services/notifications.py`** — added
   `notify_fitness_auth_broken(user_id, source)` and
   `notify_fitness_sync_failure(user_id, source, attempts)` mirroring the
   `notify_health_alert` pattern. `PushoverNotificationService` structurally
   satisfies `FitnessNotifier`.
4. **W4 follow-up: `StravaAuthError`** added to `providers/strava.py` (parity
   with W5's `GarminAuthError`). `list_activities`, `get_activity_detail`, and
   `refresh_token_if_needed` each translate `stravalib.exc.AccessUnauthorized` /
   `AuthError` to the typed contract. 3 new strava tests (16 total in the file).
5. **W5 follow-up: `raw_payloads_per_endpoint["training_load"]`** — the key was
   originally `"training_status"`, but the `fitness_raw_garmin.endpoint` CHECK
   constraint from W2 only allows `"training_load"`. Renamed in
   `providers/garmin.py` and the W5 tests + fixture README. The schema's
   terminology takes precedence because it's load-bearing for `insert_raw`.
6. **`tests/test_services/test_fitness/test_fetch.py`** — 12 tests, including
   the seven plan scenarios (Strava happy path, Garmin happy path, auth-broken
   fire-once, transient threshold fire-on-Nth, idempotent re-run, auth recovery
   silent + `auth_broken_since` cleared, unknown-exception classified as
   transient_failure) plus single-run guard short-circuit, missing-auth-state
   silent path, Garmin auth path symmetry with Strava, dataclass serialisability,
   and a regression check that `Strava/GarminAuthError` are NOT subclasses of
   `FitnessAuthError`. Plus 4 new notification-service tests for the two new
   notify methods.

Total: 1987 passing (1971 prior + 12 fetch + 4 notify; 22 garmin and 16 strava
tests still green after the W4/W5 follow-ups).

## Decisions worth recording

1. **Threshold counter via `list_recent_sync_runs(limit=N+1)` join, not
   `extras_json`.** Zero extra writes per failure; the status streak is
   computed by walking the head of the most-recent list and stopping at the
   first non-`transient_failure`. Fires only when the streak length is *exactly*
   N — a 4th consecutive failure with N=3 doesn't re-fire. See
   `_consecutive_transient_streak` in `fetch.py`.
2. **Single-run guard returns `status="running"`, not `"success"`.** The
   `FitnessSyncResult` status enum gains a fourth state beyond the three
   terminal sync-run statuses (`success`, `auth_broken`, `transient_failure`)
   so callers can distinguish "this run was skipped because another is in
   flight" from "this run succeeded with zero rows." Workers (W8) will need
   to handle this fourth state explicitly.
3. **Missing auth state is silent auth_broken.** A user who has never
   connected has `fitness_auth_state` row absent; the fetch service records
   `auth_broken` with `error_class="MissingAuthState"` but does NOT fire
   Pushover. Recovery from "never connected" is just "first connect" and
   doesn't deserve a re-auth alert. Caught by
   `test_missing_auth_state_returns_auth_broken_silently`.
4. **All non-auth exceptions classified as transient_failure.** The plan
   distinguishes "known transient" (network, 5xx, 429) from "unknown" but
   both produce identical sync-run rows (`status='transient_failure'`,
   `error_class` set). The only difference is logging verbosity: a single
   `log.warning(..., exc_info=True)` covers both, leaving stack traces in
   the log without crashing the worker. Plan test #7 verified.
5. **Endpoint enums are schema-canonical, not provider-canonical.**
   `fitness_raw_strava.endpoint` accepts `('activities', 'activity_detail',
   'athlete')`; `fitness_raw_garmin.endpoint` accepts `('sleep', 'hrv',
   'body_battery', 'training_load', 'training_readiness', 'stress',
   'activities', 'activity_detail')`. The fetch service uses `'activities'`
   for both list-fetched activity rows. Single-day Garmin metrics use the
   six wellness keys verbatim; the W5 provider's
   `raw_payloads_per_endpoint` was renamed to match.
6. **No `since`/`until` derived → backfill anchor uses
   `fitness_backfill_start` (config) when `last_successful_sync_at` is None.**
   Implementation: `since = max(last_ok, backfill_start)` parses both as UTC
   datetimes; the backfill string is `"YYYY-MM-DD"` format which becomes
   midnight UTC. The W13 CLI backfill passes explicit values; routine
   `fitness-sync` (W8) lets the service derive.
7. **Per-source `provider_factory` injected via constructor**, not built
   from `Config`. Tests pass fakes directly; production wiring lives in W8
   (worker setup). Keeps the fetch service trivially testable without HTTP
   mocking and means swapping the provider implementation only touches the
   wiring file.
8. **`_FetchServiceBase` is private.** External callers go through
   `StravaFetchService` / `GarminFetchService`. The base class encodes
   shared lifecycle but isn't part of the public API, so future split into
   per-source services with diverging lifecycles stays unconstrained.

## What's not done yet

1. **Normalize is W7's job.** `FitnessSyncResult.rows_normalized` is always
   0 from W6. The fetch service writes raw rows only; W7 will read them out
   of `fitness_raw_strava` / `fitness_raw_garmin` and produce
   `fitness_activities` / `fitness_daily` rows.
2. **Worker integration is W8.** No `JobRunner` registration in this commit;
   the fetch service is just a class. W8 wires it into a `fitness_sync_*`
   job_type that the runner can dispatch.
3. **REST + MCP surface is W9 / W10.** `services/fitness/fetch.py` is the
   only entry point right now. Manual exercise via `python -c "..."` works;
   integrated user-facing flows come later.
4. **No live HTTP test.** All W6 tests use injected fake providers — no
   `stravalib.Client` or `garminconnect.Garmin` constructed. W13 is the
   first time any of this hits a real API.

## Pinned

- No new dependencies; W6 is pure orchestration on top of W2/W3/W4/W5.

## Tests

- 1987 passed (1971 prior baseline + 12 fetch + 4 notify), 0 failed.
- Lint clean (ruff). Two `# noqa: N818` annotations: `FitnessNormalizeDrift`
  (named per the plan, will be raised by W7) and `_SomethingExotic` in the
  test for the unknown-exception path (deliberately weird name).
