# 260510 — fitness multi-user W5: backfill workers + endpoint + mid-run hardening

Fifth work unit from `docs/fitness-multiuser-plan.md`. Wraps the existing
single-user `services/fitness/backfill.py` orchestrator as two new job
workers, exposes them via `POST /api/fitness/backfill/{source}` and a new
MCP tool, makes the W5 spanning idempotency (one fetch job per
`(user_id, source)` across sync **and** backfill) the single dedup
contract for all callers, and retroactively hardens the existing
`fitness_sync_{strava,garmin}` workers against mid-run auth removal.

## What shipped

Code:

- `src/journal/services/fitness/errors.py` — new `MidRunAuthLost`
  (`FitnessError` subclass) with a `reason: "removed" | "broken"`
  attribute so callers can distinguish disconnect vs. external broken
  flip.
- `src/journal/services/fitness/fetch.py` — `_verify_auth_live`
  helper on `_FetchServiceBase` plus a new `initial_auth_status`
  parameter threaded through `_do_fetch_and_persist`. Both
  `StravaFetchService` and `GarminFetchService` call the verify hook
  between provider API calls (Strava: before and after
  `refresh_token_if_needed`; Garmin: before `login`, at the top of
  each day in the daily loop, before `list_activities`). `run_sync`
  now catches `MidRunAuthLost` *before* `FitnessAuthError` and marks
  the `fitness_sync_runs` row `auth_broken` with
  `error_class="MidRunAuthLost"` — crucially, it does **not** call
  `transition_auth`, so a deleted row stays deleted.
- `src/journal/db/fitness_repository.py` — `get_auth_status(user_id,
  source) -> str | None` projection (one column read, no JSON
  hydration) so the mid-run check stays cheap enough to run inside the
  Garmin per-day loop.
- `src/journal/db/jobs_repository.py` —
  `find_active_fitness_fetch_job(user_id, source) -> Job | None`,
  the spanning dedup primitive. Matches `type IN ('fitness_sync_*',
  'fitness_backfill_*') AND status IN ('queued', 'running')` and
  returns the oldest match (`ORDER BY created_at ASC`) so the
  "first-enqueued wins" policy is observable.
- `src/journal/services/jobs/validation.py` — new
  `FITNESS_BACKFILL_KEYS = {"user_id": int, "start": str, "end": str}`.
- `src/journal/services/jobs/workers/__init__.py` — two new optional
  `backfill_strava` / `backfill_garmin` callables on `WorkerContext`.
- `src/journal/services/jobs/workers/fitness_backfill_{strava,garmin}.py`
  — new workers wrapping the existing orchestrator. `BackfillResult` is
  serialised via `dataclasses.asdict` into the job's `result_json`;
  `BackfillBlocked` is mapped to a clean `mark_failed` (with the W5
  spanning dedup it's now improbable but still possible if the
  orchestrator's window-boundary single-run guard races a sync).
- `src/journal/services/jobs/runner.py` — accepts
  `backfill_strava_callable` / `backfill_garmin_callable` and adds
  `submit_fitness_backfill_strava` / `submit_fitness_backfill_garmin`
  with the same configured-check raising `RuntimeError` shape as the
  existing sync-submit methods. Dedup deliberately lives at the
  endpoint/MCP-tool layer, not the runner — see plan-vs-code deltas
  below.
- `src/journal/mcp_server/bootstrap.py` — wires the two new callables
  as closures over the per-source `FetchService` + `FitnessRepository`
  + `notification_service`.
- `src/journal/api/ingestion.py` — new `POST /api/fitness/backfill/
  {source}` endpoint; existing `POST /api/fitness/sync/{source}`
  reworked to call `find_active_fitness_fetch_job` so its dedup now
  spans the backfill worker class too.
- `src/journal/mcp_server/tools/fitness.py` — new
  `fitness_trigger_backfill(source, start, end?)` tool; existing
  `fitness_trigger_sync` rewired to the spanning dedup helper.

Tests (2203 → 2237 passing — 34 new):

- `tests/test_services/test_fitness/test_fetch.py` — five new tests
  cover mid-run robustness: Strava mid-run disconnect, Strava mid-run
  auth_status flip to broken, Garmin mid-run disconnect inside the
  per-day loop, Garmin mid-run auth_status flip, and the
  `MidRunAuthLost` error-class `reason` carry. Each asserts (a) the
  sync_runs row ends in a terminal state with the right error_class,
  (b) the auth_state row is **not** recreated when it was deleted,
  (c) `notify_fitness_auth_broken` does NOT fire.
- `tests/test_services/test_jobs/test_worker_fitness_backfill.py` —
  new file. Direct worker unit tests against a minimal
  `WorkerContext`: happy path for Strava + Garmin, optional `end`
  pass-through, `BackfillBlocked` mapped to `mark_failed`,
  unconfigured runner marks failed, unexpected exception marks failed.
- `tests/test_services/test_jobs_runner.py` — new `TestFitnessBackfill`
  class: queued-job creation for both sources, `RuntimeError` when not
  configured, param-type validation.
- `tests/test_db/test_jobs_repository.py` — new
  `TestFindActiveFitnessFetchJob` class: 9 scenarios covering both
  directions of the spanning dedup (sync blocks backfill submit,
  backfill blocks sync submit), terminal jobs don't block, per-user /
  per-source scoping, and the "oldest wins" tie-break.
- `tests/test_api_fitness.py` — new backfill-endpoint tests (10 cases):
  happy paths, unknown source, missing/malformed/end-before-start
  validation, unconfigured 503, plus three spanning-dedup scenarios
  (sync blocks backfill, backfill blocks sync, backfill blocks
  backfill).
- `tests/test_mcp_tools_fitness.py` — extended `configured_runner`
  fixture with backfill callables, new tests for
  `fitness_trigger_backfill` (unknown source, unconfigured, missing
  start, happy path, blocked-by-running-sync) and a
  `fitness_trigger_sync_blocked_by_running_backfill` test. Tool
  registry assertion updated to include `fitness_trigger_backfill`.

Docs:

- `docs/api.md` — new `POST /api/fitness/backfill/{source}` section
  with full body / response / error envelope; updated
  `POST /api/fitness/sync/{source}` to call out the spanning dedup;
  new `fitness_trigger_backfill` MCP-tool block and matching update
  on the `fitness_trigger_sync` block.
- `docs/fitness-operations.md` §3 "Historical backfill" — new
  preamble explicitly naming the in-app POST endpoint as the primary
  path with the CLI as operator fallback, and a sentence-paragraph
  spelling out the spanning idempotency policy.

## Plan-vs-code deltas

The plan's §5 W5 captured the shape correctly. Three small drifts
worth recording, plus one schema-vs-prompt discrepancy.

1. **Idempotency lives at the endpoint/MCP-tool layer, not on the
   runner submit methods.** The plan reads "If an existing
   queued/running job is found, return its `{job_id}` rather than
   enqueueing a new one" — left ambiguous whether the runner method
   itself does the check (and how it then communicates "this is the
   existing one" back to its caller). I considered three options:

   - **(a)** Runner returns a tuple `(Job, was_existing: bool)`.
     Breaks the existing `submit_fitness_sync_*` callers and every
     test that does `job = runner.submit_fitness_sync_strava(...)`.
   - **(b)** Runner returns `Job` only and the caller infers
     "existing vs new" from `started_at` or a creation-time
     timestamp. Brittle.
   - **(c)** Add `find_active_fitness_fetch_job` to `SQLiteJobRepository`
     as the spanning-dedup primitive, and let the endpoint / MCP tool
     consult it BEFORE calling `submit_*`. Runner submit methods stay
     dumb dispatchers.

   I went with (c). It preserves the existing surface (runner
   `submit_fitness_*` still returns `Job`), keeps a single source of
   truth for the dedup query (one method, three callers), and the
   tiny race window between "find" and "create" is bounded by the
   single-worker executor + SQLite write serialisation — small
   enough to ignore for a personal-scale deploy with one writer.
   Race-recovery is a non-issue: if two submits did slip through and
   create two rows, the next `find_active_fitness_fetch_job` call
   surfaces the oldest (`ORDER BY created_at ASC`) — the policy
   still has a well-defined winner.

2. **The mid-run auth check has an auto-recovery carve-out the plan
   didn't anticipate.** The plan says "If the row is missing (user
   disconnected mid-run) or `auth_status='broken'`, mark the
   `fitness_sync_runs` row…". A literal read aborts every run that
   *starts* with `auth_status='broken'` — but that's exactly the
   auto-recovery scenario (`test_auth_recovery_clears_broken_since_
   and_does_not_notify` in `test_fetch.py`): the fetch service is
   deliberately attempting to push through a broken row to see if
   recovery has happened (e.g., user re-authed via CLI). Aborting on
   the FIRST `_verify_auth_live` would silently disable recovery.

   Resolution: thread `auth.auth_status` from `run_sync` down into
   `_do_fetch_and_persist` as `initial_auth_status`, and have
   `_verify_auth_live` only raise `MidRunAuthLost(reason="broken")`
   when the status has **transitioned** from non-broken to broken
   during the run. The `reason="removed"` path is unconditional —
   a deleted row at any point means the user disconnected, and
   that's always a clean abort. This preserves auto-recovery while
   still catching the intended hazard.

3. **The plan's prompt said `fitness_sync_runs.status` is
   constrained to `{queued, running, success, failed}`.** The
   actual CHECK constraint (migration 0023) is
   `('running', 'success', 'auth_broken', 'transient_failure',
   'normalize_drift')` — no `failed`, no `queued`. That's the
   jobs-table convention bleeding into the prompt. The right value
   for a mid-run auth abort is `auth_broken` (the existing fetch
   service's vocabulary) with `error_class="MidRunAuthLost"` and an
   `error_message` distinguishing `"auth removed during run"` vs
   `"auth broken during run"`. The jobs-table row separately ends
   up in `succeeded` (with the backfill's outcome dict, including
   any `aborted_reason`) or `failed` per the worker's own
   bookkeeping, so the two audit trails read cleanly side-by-side.

4. **The new backfill workers consume `BackfillResult` /
   `BackfillBlocked` from `services/fitness/backfill.py` for the
   first time outside the CLI.** Previously CLI-only. The result is
   serialised through `dataclasses.asdict` (same pattern as the
   existing sync workers) so all of `final_status`,
   `windows_attempted`, `windows_succeeded`, `rows_fetched`,
   `rows_normalized`, and the optional `aborted_reason` land in the
   job's `result_json` for the webapp to render. `BackfillBlocked`
   maps to `mark_failed(job_id, str(exc))` — the orchestrator's
   message ("routine sync in flight … wait for the in-flight run to
   finish, then re-run") is already operator-friendly.

## Idempotency-policy implementation

One repository method, three callers. The shape is:

```python
def find_active_fitness_fetch_job(*, user_id, source) -> Job | None:
    # type IN ('fitness_sync_{source}', 'fitness_backfill_{source}')
    # AND status IN ('queued', 'running')
    # AND user_id = ?
    # ORDER BY created_at ASC LIMIT 1
```

Callers:

- `POST /api/fitness/sync/{source}` — checks BEFORE
  `submit_fitness_sync_*`; returns the in-flight job with
  `already_running: true` (status 202) on hit.
- `POST /api/fitness/backfill/{source}` — same check before
  `submit_fitness_backfill_*`.
- `fitness_trigger_sync` and `fitness_trigger_backfill` MCP tools —
  identical posture.

Result: the dedup is *spanning* across worker classes (sync blocks
backfill and vice versa) AND *spanning* across entry points (the
REST endpoint and MCP tool yield the same decision against the same
DB row). Tests cover both directions explicitly for both surfaces.

## Mid-run hardening: what changed across all four workers

The plan flagged this as retroactive. The change is at the
`_FetchServiceBase` layer (one shared base, two source subclasses,
each of those is the body of one worker per source — so adding the
check to the base lifts all four workers simultaneously):

- `_verify_auth_live` reads only `auth_status` (cheap projection, no
  JSON hydration).
- Strava: called before and after `provider.refresh_token_if_needed()`.
  One window per run, so two checks is the cap.
- Garmin: called before `provider.login()`, at the top of each
  iteration of the per-day loop, and before `list_activities` at the
  end. A 30-day Garmin window now does ~33 cheap SELECTs — dominated
  by the Garmin SDK calls (each takes hundreds of milliseconds).
- The `MidRunAuthLost` exception lands in a new `run_sync` except
  branch BEFORE the existing `FitnessAuthError` branch, so the
  mid-run abort path never reaches `transition_auth` — important
  because that method INSERTS a row if missing, which would silently
  recreate a row the user just deleted.
- Auto-recovery preserved via `initial_auth_status` (see Plan-vs-code
  delta #2).

Backfill inherits the hardening automatically because each window
calls `run_sync`, which now has the mid-run check baked into
`_do_fetch_and_persist`. A user disconnecting between Garmin window 3
and window 4 of a 6-window backfill aborts cleanly at the start of
window 4's first day-loop iteration with the run row ending
`auth_broken` and `error_class="MidRunAuthLost"`, error_message
`"auth removed during run"`. Windows 1-3 stay normalized.

## Gotchas worth recording

1. **`fitness_sync_runs.status` does not include `failed`.** I almost
   wrote a `_do_fetch_and_persist` branch that called
   `finish_sync_run(run_id, status="failed", ...)` based on the
   prompt wording. The CHECK constraint catches this at execution
   time, but the test harness would surface it as a sqlite3
   IntegrityError mid-run — confusing to debug. `auth_broken` is the
   right value for the mid-run abort.

2. **`transition_auth` inserts on missing.** Read this method's body
   before catching `MidRunAuthLost` via the existing
   `FitnessAuthError` path. The existing path calls
   `transition_auth(status="broken")` which performs an INSERT when
   the row is missing — that's correct for the normal 401 case (the
   row may have been deleted by some unrelated path; we want the
   broken row reborn). But for a *user-initiated disconnect*, INSERT
   undoes the user's action. Hence the separate `MidRunAuthLost`
   branch that skips `transition_auth` entirely.

3. **`fitness_sync_runs` for an auto-recovery run starts with
   `auth.auth_status="broken"`.** The mid-run check must NOT abort
   on this; that's the whole point of attempting the run. The
   `initial_auth_status` thread-through (Plan-vs-code delta #2)
   handles it. There's a regression test
   (`test_auth_recovery_clears_broken_since_and_does_not_notify`)
   that was passing before W5 and continued passing after the
   carve-out was added — that test is the canary for this regression.

4. **Backfill orchestrator already takes a `notifier`.** The
   bootstrap closure passes the `notification_service` (which
   satisfies the `NormalizeDriftNotifier` Protocol via its
   `notify_fitness_normalize_drift` method). The CLI's
   `_run_one_source_backfill` uses a `_NoopFitnessNotifier` for the
   fetch service but does NOT pass a normalize notifier to
   `backfill_*` (so normalize-drift events on the CLI path are
   silent). The job-path closure DOES pass the real notifier, so
   webapp-triggered backfills surface normalize drift via Pushover
   if the user has it configured. Functionally an improvement;
   noted here in case the CLI and job paths diverge further later.

## Scope notes — what is *not* in W5

- **No webapp work.** The Backfill button + UI is W9.
- **No env-var removal.** W6.
- **No CLI `--user-id` required-ness.** W7.
- **No webhooks.** Out of scope (plan §7).
- **No new migrations.** Reuses the existing `fitness_sync_runs`
  CHECK constraint and `jobs` table.

## Files touched

Code:

- `src/journal/services/fitness/errors.py` — new `MidRunAuthLost`
- `src/journal/services/fitness/fetch.py` — mid-run hardening
- `src/journal/db/fitness_repository.py` — `get_auth_status`
- `src/journal/db/jobs_repository.py` — `find_active_fitness_fetch_job`
- `src/journal/services/jobs/validation.py` — `FITNESS_BACKFILL_KEYS`
- `src/journal/services/jobs/workers/__init__.py` — context fields
- `src/journal/services/jobs/workers/fitness_backfill_strava.py` (new)
- `src/journal/services/jobs/workers/fitness_backfill_garmin.py` (new)
- `src/journal/services/jobs/runner.py` — submit + ctor params
- `src/journal/mcp_server/bootstrap.py` — wire callables
- `src/journal/api/ingestion.py` — new backfill endpoint + sync dedup
- `src/journal/mcp_server/tools/fitness.py` — new tool + sync dedup

Tests:

- `tests/test_services/test_fitness/test_fetch.py` — +5 mid-run tests
- `tests/test_services/test_jobs/test_worker_fitness_backfill.py` (new)
- `tests/test_services/test_jobs_runner.py` — +`TestFitnessBackfill`
- `tests/test_db/test_jobs_repository.py` — +`TestFindActiveFitnessFetchJob`
- `tests/test_api_fitness.py` — backfill endpoint + spanning-dedup tests
- `tests/test_mcp_tools_fitness.py` — `fitness_trigger_backfill` tests

Docs:

- `docs/api.md` — backfill endpoint + MCP tool sections, spanning dedup
- `docs/fitness-operations.md` — §3 in-app path + idempotency policy

## What's next

W8 (webapp API client) and W9 (settings panel with Backfill button)
are the natural follow-ons; both depend on W2/W3/W5 which are now all
landed. The webapp work is a different surface — Vue components, type
definitions, mock-state coverage — and the server-side context loaded
this session won't carry over usefully. A fresh session under the
`webapp/` repo is the right move for W8.
