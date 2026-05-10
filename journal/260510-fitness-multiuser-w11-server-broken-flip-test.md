# W11 (server side) — worker-level broken-flip test

Date: 2026-05-10
Plan: `docs/fitness-multiuser-plan.md` §5 W11.

## What W11 says (recap)

> Pre-flight verification (per D3): the banner is only useful if sync workers actually
> flip `fitness_auth_state.auth_status` to `'broken'` on Garmin/Strava 401s and expired-token
> responses. Read each worker's error path and confirm the field is set (not just `error`
> written to `fitness_sync_runs`). If the flip is missing, add it as part of this unit —
> otherwise the banner stays green forever for users whose tokens silently expired.

## Verification

Confirmed by reading the existing code:

1. **Providers** raise `FitnessAuthError` subclasses on 401:
   - `providers/strava.py:144` — `(AccessUnauthorized, AuthError)` → `StravaAuthError`
   - `providers/garmin.py:183` — `GarminConnectAuthenticationError` → `GarminAuthError`
2. **Fetch service** catches `FitnessAuthError` and calls
   `repo.transition_auth(status="broken", ...)` at `services/fitness/fetch.py:181-196`.
   The notifier fires once per state transition (`transition_auth` returns False on a
   no-op).
3. **Existing fetch-service tests** assert the flip works end-to-end at the service
   layer: `tests/test_services/test_fitness/test_fetch.py::test_strava_auth_error_transitions_state_and_fires_once`
   and the parallel Garmin test.

The flip is already wired correctly. No production-code changes needed.

## What this commit adds

A new test class
`TestWorkerFlipsAuthStatusOn401` in
`tests/test_services/test_jobs/test_worker_fitness_sync.py` with two tests (Strava,
Garmin) that exercise the **worker → fetch service → repo** wiring end-to-end:

1. Build a real `FitnessRepository` against a migrated `db_conn`, seed a user and an
   `auth_status='ok'` row.
2. Construct a real `StravaFetchService` / `GarminFetchService` backed by a tiny inline
   fake provider that raises `StravaAuthError` / `GarminAuthError`.
3. Wire `fetch_strava=svc.run_sync` (or the Garmin equivalent) into the `WorkerContext`.
4. Run the worker (`run_fitness_sync_strava` / `run_fitness_sync_garmin`).
5. Assert three things:
   - The persisted `fitness_auth_state` row is `auth_status='broken'` with a non-null
     `auth_broken_since` (this is the bit the banner reads).
   - The notifier was called exactly once with `(user_id, source)` (fire-once contract).
   - The jobs row is `status='failed'` with an error message that mentions
     "authorization is broken" (so the UI can surface it consistently).

The existing fetch-service tests cover the same flip on their own, but they stub the
worker out. This new test guards the seam between the two layers: a future refactor
that, say, makes the worker call a different fetch path or swallow the auth error would
silently break the banner. Now it'd break this test instead.

## Why two near-duplicate tests (Strava and Garmin) rather than one parametrised one

The fakes are small but the auth-error raising lives on different methods (Strava raises
inside `list_activities`; Garmin raises inside `login`), so a parametrised version would
end up with a noisy `if source == "strava"` branch in the fake construction. Two
explicit tests read better.

## Test count

- Before: 2237 unit tests
- After: 2239 (+2)
- Lint: `uv run ruff check src/ tests/` — All checks passed
- Full suite: `uv run pytest -m "not integration"` — 2239 passed, 8 deselected.

## Companion change (webapp side)

The webapp half of W11 — changing `FitnessAuthBanner.vue`'s CTA from CLI command
guidance to a Reconnect button routed to `/settings#fitness` — ships as a separate
commit on the webapp repo. See `webapp/journal/260510-fitness-multiuser-w11-banner-copy.md`.

## CI note

Server `main` had a pre-existing flake in
`tests/test_api_jobs.py::TestEntityExtractionRoute::test_entry_id_path_goes_through_jobs`
that fails in GitHub Actions but passes locally and on previous commits before this
session. It is unrelated to W11 and was flagged to the user; the W11 commit may still
trip the same flake on CI. Tracking that fix as a follow-up.
