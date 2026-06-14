# Daily Fitness Auto-Sync — Design

**Date:** 2026-06-14
**Status:** approved (design); implementation pending
**Scope:** `server/` (Python backend)

## Goal

Automatically refresh each user's fitness data once per day at **17:00 server-local
time** (UTC in Docker). For every user, sync whichever sources they have working
credentials for:

- only Strava configured → sync Strava only
- only Garmin configured → sync Garmin only
- both configured → sync both
- neither → skip

The job only *enqueues* incremental syncs; all fetch/normalize/notify plumbing already
exists and is reused unchanged.

## Decisions (from brainstorming)

| Question | Decision |
|----------|----------|
| Trigger mechanism | **In-process scheduler** — a daemon thread inside the running server, modeled on the existing `HealthPoller`. Runs only while the server is up. |
| Timezone | **17:00 server-local** (UTC in Docker). No env knob for the time. |
| Broken/expired auth | **Skip.** Only enqueue sources whose credentials are present and `auth_status != 'broken'`. |
| Notifications | **Only on failure / new data.** A successful run that ingested zero new activities is silent; auth failures and runs with new activities still notify. |
| Missed runs (server down at 17:00) | **No catch-up.** Sleep to the next 17:00. The next run is incremental and pulls the backlog since the last successful sync, so little data is lost. |

## Architecture

A single daemon thread (`FitnessSyncScheduler`) lives in the long-running Uvicorn
process, started during `_init_services()` and stopped cleanly on shutdown. Each day at
17:00 it asks the repository which users have active auth per source and submits sync
jobs through the existing `JobRunner`. The `JobRunner`'s single worker drains the queue;
existing per-`(user, source)` dedup (W5) prevents duplicating an in-flight manual sync.

```
Uvicorn process
  └─ FitnessSyncScheduler (daemon thread)
       every day @ 17:00 local:
         for source in (strava, garmin):
           for user_id in repo.list_users_with_active_auth(source):
             job_runner.submit_fitness_sync_<source>(user_id, quiet_success=True)
  └─ JobRunner (ThreadPoolExecutor, max_workers=1)
       └─ fitness_sync_strava / fitness_sync_garmin workers (reused)
```

## Components

### 1. `FitnessRepository.list_users_with_active_auth(source: str) -> list[int]`

New repository method. One SQL query against `fitness_auth_state` returning distinct
`user_id`s where:

- `source = ?`
- `auth_status != 'broken'`
- credentials are present, mirroring the per-source `_has_credentials` rule used by the
  fetch services:
  - **strava:** `access_token IS NOT NULL OR refresh_token IS NOT NULL`
  - **garmin:** `json_extract(extra_state_json, '$.tokens_blob') IS NOT NULL`

The per-source credential predicate intentionally duplicates the fetch services'
`_has_credentials` logic in SQL. A unit test asserts the two agree on representative
rows so they cannot silently drift.

### 2. `FitnessSyncScheduler` — `services/fitness/scheduler.py` (new)

Daemon thread modeled on `services/health_poll.py::HealthPoller`.

- `__init__(self, *, job_runner, fitness_repo, hour: int = 17, enabled: bool = True, clock=...)`
  — `hour` and an injectable clock exist for testability; default fire time is 17:00
  server-local. `clock` defaults to a small wrapper over `datetime.now()`.
- `start()` — spawn the daemon thread (no-op if `enabled` is false).
- `stop()` — set the stop `Event` and `join()` the thread (bounded timeout).
- Loop: compute seconds until the next 17:00 local; sleep in short interruptible slices
  (e.g. ≤60s) checking the stop `Event` so shutdown is prompt; at fire time call
  `run_daily_sync()`; recompute and repeat.
- `run_daily_sync()` — for each source in `("strava", "garmin")`, look up active users and
  call the matching `job_runner.submit_fitness_sync_*` with `quiet_success=True`. Wrap each
  submit in try/except so one failing user is logged and skipped, not fatal. Log a one-line
  summary, e.g. `daily fitness sync: strava=1 enqueued, garmin=1 enqueued`.

Next-fire-time math is a pure function (given "now", return the next 17:00 local) so it can
be unit-tested without sleeping.

### 3. Quiet-success notifications

Thread a `quiet_success: bool` flag through the sync job params:

- `JobRunner.submit_fitness_sync_strava` / `submit_fitness_sync_garmin` accept an optional
  `quiet_success: bool = False` and place it into the job `params`.
- The `fitness_sync_strava` / `fitness_sync_garmin` workers read the flag. On a **successful**
  result that ingested **zero** new activities, skip the success notification. Auth failures,
  transient failures, and successful runs with ≥1 new activity notify as today.
- Manual syncs (REST/MCP) pass nothing → flag defaults `False` → today's behavior unchanged.

**Dependency / risk:** this requires `FitnessSyncResult` (or the worker) to expose a count
of newly-ingested activities. If no such count is available, fall back to
**notify-on-failure-only** for scheduled syncs (still silent on success) and note the
limitation in the implementation. Confirm during implementation.

### 4. Bootstrap & shutdown wiring

- In `mcp_server/bootstrap.py::_init_services()`: construct `FitnessSyncScheduler` with the
  initialized `job_runner` and fitness repo, gated on a new `FITNESS_SYNC_ENABLED` config
  (default `True`), and `start()` it.
- In the existing shutdown/atexit path (next to `HealthPoller.stop()` and
  `job_runner.shutdown()`): call `scheduler.stop()`.
- **Clean thread shutdown is mandatory.** A leaked thread/executor has caused CI segfaults
  in this repo before; `stop()` must reliably set the event and join.

### Config

- `FITNESS_SYNC_ENABLED: bool = True` (in `config.py`) — lets tests and ops disable the
  scheduler without code edits. No env var for the time; 17:00 local is fixed.

## Error handling

- A failed individual submit is caught, logged, and skipped — one bad user never aborts the
  daily run.
- An unexpected exception in the scheduler loop is logged and the loop continues to the next
  day rather than killing the thread.
- Auth-broken sources are excluded up front, so the run does not generate guaranteed-failing
  jobs.

## Testing

Unit tests (pytest, in-memory SQLite, fake `JobRunner` — no real threads or sleeps):

- `list_users_with_active_auth`: returns users with present creds; excludes
  `auth_status='broken'`; excludes rows with missing creds; correct per source (strava token
  vs garmin `tokens_blob`); the drift-guard test asserting agreement with the fetch services'
  `_has_credentials`.
- `run_daily_sync`: across a fixture mix (strava-only, garmin-only, both, neither, broken)
  enqueues exactly the right `(user, source)` jobs with `quiet_success=True`.
- Next-fire-time function: given various "now" values, returns the correct next 17:00 local.
- Quiet-success suppression: success+0-new → no notify; success+N-new → notify; failure →
  notify. (Worker-level test.)
- Lifecycle: `start()`/`stop()` is clean; any spawned thread is joined in teardown (guards
  against the known CI segfault).

## Out of scope (YAGNI)

- Per-user timezones (no stored user timezone today).
- Configurable run time via env / multiple runs per day.
- Catch-up / persisted "last run" tracking.
- A UI to toggle auto-sync per user.
