# W8 — Job workers + JobRunner wiring (`fitness_sync_*`)

**Date:** 2026-05-09. **Plan:** [docs/fitness-tier-plan.md](../docs/fitness-tier-plan.md) §W8.

## What shipped

1. **`src/journal/services/jobs/workers/fitness_sync_strava.py` and
   `fitness_sync_garmin.py`** — two free-function workers, same
   shape as `entity_reembed.py`. Each calls
   `ctx.fetch_<source>(user_id=...)`, branches on
   `FitnessSyncResult.status`, then calls
   `ctx.normalize_<source>(user_id=...)` only on the success path.
   Auth-broken / transient-failure short-circuit to `mark_failed`
   without running normalize; `running` (single-run guard hit)
   short-circuits to `mark_succeeded` with a `skipped: true` flag.
2. **`src/journal/services/jobs/workers/__init__.py`** — extended
   `WorkerContext` with four optional callable fields
   (`fetch_strava`, `fetch_garmin`, `normalize_strava`,
   `normalize_garmin`). Optional so existing tests and a server
   booted without fitness creds still construct a valid context;
   the workers themselves fail-loudly when None.
3. **`src/journal/services/jobs/runner.py`** — extended
   `JobRunner.__init__` with four new keyword-only callable
   parameters and added `submit_fitness_sync_strava` /
   `submit_fitness_sync_garmin`. Both raise `RuntimeError` at
   submit time if their callable pair is unconfigured, so an
   unconfigured server never queues a guaranteed-to-fail job.
4. **`src/journal/services/jobs/validation.py`** — added
   `FITNESS_SYNC_KEYS = {"user_id": int}`. Source is encoded in
   the job_type, not in params, mirroring `mood_score_entry`.
5. **`src/journal/models.py`** — extended the `JobType` Literal
   with `"fitness_sync_strava"` and `"fitness_sync_garmin"`.
6. **`src/journal/mcp_server/bootstrap.py`** — added
   `_build_fitness_callables(...)` which constructs
   `FitnessRepository` + `Stravalib`/`GarminConnect` provider
   factories + `Strava`/`GarminFetchService` and returns a dict of
   the four callables. Strava is wired only when
   `STRAVA_CLIENT_ID`+`STRAVA_CLIENT_SECRET` are set; Garmin only
   when `GARMIN_USERNAME`+`GARMIN_PASSWORD` are set. Either source
   can boot independently.
7. **`tests/test_services/test_jobs/test_worker_fitness_sync.py`**
   — 14 worker tests (7 per source: success, auth-broken short-
   circuit, transient-failure short-circuit, already-running
   short-circuit, drift recorded in result, terminal-state guard
   on bare `RuntimeError`, success notification carrying the right
   `job_type` for `_SUCCESS_TOPIC_MAP` routing).
8. **`tests/test_services/test_jobs_runner.py`** — 5 new
   `TestFitnessSync` runner-integration tests: submit-strava,
   submit-garmin, submit-strava-when-unconfigured-raises,
   submit-garmin-when-unconfigured-raises, validates user_id type.
9. **`docs/fitness-tier-plan.md` §W8** — corrected the worker
   body sketch and trimmed three planned edits that turned out
   already-done or unnecessary (see "Plan corrections" below).

Total: 2046 passing (2027 prior + 14 worker + 5 runner). Lint clean.

## Plan corrections (the W8 plan was written before W6/W7 shipped)

1. **Worker body branches on `FitnessSyncResult.status`, not
   exceptions.** W6 decision #4 swallows every non-auth exception
   inside `_FetchServiceBase.run_sync` and surfaces them as
   `status="transient_failure"`. Auth failures are caught and
   converted to `status="auth_broken"`, with the
   `transition_auth` + `notif_fitness_auth_broken` Pushover
   already fired inside the fetch service. So the worker never
   sees `FitnessAuthError` from a healthy `run_sync`. The plan's
   `try/except FitnessAuthError/Exception` sketch was rewritten
   as four explicit `if status == ...` branches.
2. **`is_transient` / `friendly_error` extension dropped.** The
   plan asked for stravalib / garminconnect transient patterns to
   be added to the existing classifier. But because fetch
   classifies internally, the worker's `is_transient` path is
   unreachable for fitness errors. Adding patterns there would be
   dead code today. (If a future change re-raises transient
   errors out of `run_sync`, this can come back.)
3. **`FitnessNormalizeDrift` catch dropped.** W7 decision #2
   keeps `_Drift` as an internal sentinel — `normalize_*` never
   raises `FitnessNormalizeDrift` out. Drift is reported via
   `NormalizeResult.drift_count` and an admin-only Pushover
   fire-once inside normalize itself. The worker only writes
   `drift_count` into the job's result JSON.
4. **`_SUCCESS_TOPIC_MAP` and `_JOB_TYPE_LABELS` already
   populated.** W3 added the entries; the plan's "AND update both
   maps" instruction was stale. Verified at
   `notifications.py:150` and `:172`.

## Decisions worth recording

1. **Optional WorkerContext fields rather than required.** The
   four fitness callables default to `None` so existing tests
   (e.g. `test_worker_entity_reembed.py`) and a server booted
   without fitness creds don't need to thread them through. The
   workers themselves fail-loud when called with None — which
   should never happen in practice because `submit_fitness_sync_*`
   gates on the callables being set.
2. **Configuration gate on submit, not on construction.** The
   `JobRunner` accepts None callables without complaint so that
   a server with only Strava configured can still wire the
   Strava half. The `submit_fitness_sync_*` methods raise
   `RuntimeError` ("not configured") at queue time. The worker
   has its own `mark_failed` defence in case a callable somehow
   reaches it as None — belt-and-braces, since this is
   off-the-happy-path bookkeeping that's cheap to maintain.
3. **`already_running` is a `mark_succeeded`, not `mark_failed`.**
   When the W6 single-run guard short-circuits with
   `status="running"`, no work was done but no failure occurred
   either — another sync is already in flight. Marking the job
   succeeded with `{"skipped": true, "reason": "already_running"}`
   matches operator intuition: the queue accepted the request,
   nothing went wrong, and the in-flight run will produce the
   real result. Marking failed would be misleading (it'd suggest
   the user should retry) and would push a failure notification
   for what's effectively a deduplicated request.
4. **Tests use `user_id=1`.** Migration 0011 seeds user 1 and the
   `jobs.user_id` column has a FK to `users(id)`; tests that pass
   `user_id=42` against a fresh DB hit `IntegrityError`. The
   existing entity_reembed tests already use 1 — followed the
   same convention.
5. **Bootstrap persist_tokens callbacks read-then-merge.** Naïvely
   building a fresh `FitnessAuthState` and calling `upsert_auth_state`
   would clobber the columns the fetch service maintains
   independently (`auth_status`, `auth_broken_since`,
   `last_*_at`, `extra_state` for Garmin). Both `_persist`
   closures now read the existing row and pass through the
   maintained columns. This is the kind of subtlety that's
   trivial to miss in a unit test that exercises only fresh-DB
   paths — flagged here so a future probe of prod auth-state can
   verify the merge actually preserves auth_status across token
   refresh.

## What's not done yet

1. **No live HTTP test.** All W8 tests use canned `FitnessSyncResult`
   / `NormalizeResult` returns. The bootstrap helper constructs
   real `StravaFetchService` / `GarminFetchService` instances but
   nothing exercises them end-to-end. W13 (first live smoke test)
   is when this code first hits a real Strava / Garmin API.
2. **No REST or MCP surface.** `JobRunner.submit_fitness_sync_*`
   is the only entry point. W9 adds REST endpoints; W10 adds MCP
   tools.
3. **No CLI re-auth flow.** W11 adds a `journal fitness-auth` CLI
   command for first-run token acquisition (and the documented
   cron entry that triggers `submit_fitness_sync_*` daily).

## Tests

- 2046 passed (2027 prior + 14 worker + 5 runner). 0 failed.
- Lint clean (ruff). No new noqa annotations.

## Pinned

- No new dependencies. Pure orchestration on top of W4–W7.
