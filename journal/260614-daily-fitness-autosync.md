# 2026-06-14 — Daily fitness auto-sync scheduler

Shipped an in-process daily scheduler that enqueues incremental Strava/Garmin
syncs for every connected user, removing the need for an external cron job or
a manual REST call to keep fitness data current.

## Decisions

**In-process daemon thread, not an external cron.** The server already runs a
`HealthPoller` daemon thread. Mirroring its lifecycle keeps the operational
surface small — no host-level cron, no separate container, no new infra.
The tradeoff is that a down server misses its daily run, but incremental syncs
recover naturally from the existing watermarks.

**17:00 server-local time.** Late afternoon was chosen so a full day of
activities is available when the sync fires. Because the production container
runs UTC by default the effective wall-clock time is 17:00 UTC, but the
implementation uses `datetime.now()` (naive) rather than hard-coding UTC —
if `TZ` is set on the container the fire time follows it. The doc and code
both call this out explicitly.

**Skip broken credentials, don't abort the run.** `list_users_with_active_auth`
excludes any `fitness_auth_state` row with `auth_status = 'broken'` or an
absent credential. A user with a broken Strava token gets no Strava sync but
still gets a Garmin sync if that source is healthy. A failure listing one
source or submitting for one user is logged and skipped; the rest of the run
continues.

**No catch-up on missed fires.** Incremental syncs always pull from the
last-fetched watermark, so a server restart or brief downtime self-heals on
the next daily fire without special logic.

**`quiet_success=True` for scheduled runs.** A routine sync that fetched zero
new rows is a background no-op and should not generate a notification. Runs
that import new data, or that hit auth errors, still notify. Manual syncs via
REST/MCP are unchanged (always notify).

**`FITNESS_SYNC_ENABLED` env var, default `true`.** Lets operators disable the
scheduler at deploy time without a code change — useful for test environments
or if scheduling is handled externally.

## Components added / touched

| File | Change |
|---|---|
| `src/journal/services/fitness/scheduler.py` | New module: `FitnessSyncScheduler` daemon thread + `next_fire_after` helper |
| `src/journal/db/fitness_repository.py` | New query: `list_users_with_active_auth(source)` — returns user IDs with non-broken auth + present credential |
| `src/journal/services/jobs/workers/fitness_strava.py` | Added `quiet_success` param; suppress success notification when `True` and rows fetched = 0 |
| `src/journal/services/jobs/workers/fitness_garmin.py` | Same `quiet_success` plumbing |
| `src/journal/services/jobs/runner.py` | Threaded `quiet_success` kwarg through `submit_fitness_sync_{strava,garmin}` |
| `src/journal/config.py` | `fitness_sync_enabled` field reading `FITNESS_SYNC_ENABLED` env var |
| `src/journal/mcp_server/bootstrap.py` | Construct + start `FitnessSyncScheduler` after `HealthPoller`; stop it in `_shutdown_job_runner` atexit hook |
| `tests/test_services/test_fitness_scheduler.py` | New test module covering scheduler lifecycle, `next_fire_after`, `run_daily_sync` per-source dispatch, error isolation, and the disabled case |
| `tests/test_db/test_fitness_repository.py` | Tests for `list_users_with_active_auth` covering broken/missing credential filtering |

## References

- Design spec: `docs/superpowers/specs/2026-06-14-daily-fitness-auto-sync-design.md`
- Implementation plan: `docs/superpowers/` (task list for this feature)
- Ops runbook: `docs/fitness-operations.md` §4 "Daily auto-sync"
- Config reference: `docs/configuration.md` → `FITNESS_SYNC_ENABLED`
