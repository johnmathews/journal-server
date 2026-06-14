# Daily Fitness Auto-Sync Implementation Plan

**Status:** completed 2026-06-14 — all 9 tasks shipped, merged to `main`, deployed to prod. (One known deviation from this plan, discovered at deploy time: the prod VM container runs CEST/UTC+2, not UTC, so 17:00 fires at 5pm local — the design intent. Docs/code comments were corrected accordingly.)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically sync each user's connected fitness sources (Strava and/or Garmin) once per day at 17:00 server-local time, via an in-process daemon thread, skipping users with broken/absent credentials and staying quiet on no-op runs.

**Architecture:** A new `FitnessSyncScheduler` daemon thread (modeled on the existing `HealthPoller`) wakes at 17:00 each day, asks `FitnessRepository.list_users_with_active_auth(source)` for the users to sync per source, and enqueues incremental syncs through the existing `JobRunner.submit_fitness_sync_*` methods with a new `quiet_success` flag. The flag makes the existing sync workers suppress the success notification when the run fetched zero new rows. All fetch/normalize/notify plumbing is reused unchanged.

**Tech Stack:** Python 3.13, `uv`, pytest, SQLite (FTS5 + `json_extract`), `threading` (daemon thread + `Event`), frozen-dataclass `Config`.

---

## Source of truth (verified during planning)

- Sync entry points: `JobRunner.submit_fitness_sync_strava(*, user_id)` and `submit_fitness_sync_garmin(*, user_id)` in `src/journal/services/jobs/runner.py:531-566`. Each builds `params = {"user_id": user_id}`, calls `validate_params(params, FITNESS_SYNC_KEYS, job_type=...)`, creates a job row, and submits the worker.
- Param allow-list: `FITNESS_SYNC_KEYS` in `src/journal/services/jobs/validation.py:68-70` (currently `{"user_id": int}`). `validate_params` already supports `bool`-typed keys.
- Workers: `run_fitness_sync_strava` (`src/journal/services/jobs/workers/fitness_sync_strava.py`) and `run_fitness_sync_garmin` (`.../fitness_sync_garmin.py`). On the success path they call `ctx.notifier.notify_success(user_id, "<job_type>", result)`. `fetch_result` is a `FitnessSyncResult` with `.status`, `.run_id`, `.rows_fetched`, `.rows_normalized` (`src/journal/services/fitness/fetch.py:78-91`).
- Credential rules (`src/journal/services/fitness/fetch.py`): Strava base `_has_credentials` = `bool(auth.access_token)` (line 299); Garmin override = `bool(auth.extra_state and auth.extra_state.get("tokens_blob"))` (line ~425).
- Repo: `FitnessRepository` (`src/journal/db/fitness_repository.py:160`), connection via `self._conn()` (line 176), rows are `sqlite3.Row` (subscriptable by column name, see `get_auth_status` line 208-214). Table `fitness_auth_state` has columns `user_id`, `source`, `access_token`, `extra_state_json`, `auth_status` (migration `0023`).
- HealthPoller pattern to mirror: `src/journal/services/health_poll.py` (daemon thread, `threading.Event`, `start`/`stop(timeout)`/`is_running`, `_run` loop using `self._stop_event.wait(interval)`).
- Bootstrap wiring: `_init_services()` in `src/journal/mcp_server/bootstrap.py` — `job_runner` created at line 630, `fitness_repo` at line 560, `config` in scope, `HealthPoller` started at lines 656-665, shutdown hook `_shutdown_job_runner` at lines 672-681 (registered via `atexit`). The services dict returned at lines 715+.

## File Structure

- **Modify** `src/journal/db/fitness_repository.py` — add `list_users_with_active_auth`.
- **Test** `tests/db/test_fitness_repository.py` (or the existing fitness-repo test module) — query behavior + drift guard.
- **Modify** `src/journal/services/jobs/validation.py` — add `quiet_success` to `FITNESS_SYNC_KEYS`.
- **Modify** `src/journal/services/jobs/runner.py` — `quiet_success` param on both `submit_fitness_sync_*`.
- **Modify** `src/journal/services/jobs/workers/fitness_sync_strava.py` and `fitness_sync_garmin.py` — suppress success notify when `quiet_success` and `rows_fetched == 0`.
- **Test** `tests/services/jobs/workers/` — worker notify suppression (both sources).
- **Create** `src/journal/services/fitness/scheduler.py` — `FitnessSyncScheduler`.
- **Test** `tests/services/fitness/test_scheduler.py` — next-fire math, `run_daily_sync` enqueueing, lifecycle.
- **Modify** `src/journal/config.py` — add `fitness_sync_enabled` field.
- **Modify** `src/journal/mcp_server/bootstrap.py` — construct/start scheduler, stop it in shutdown hook.

> **Note on test file paths:** confirm the exact existing test module names before creating new ones (e.g. `ls tests/db/ tests/services/fitness/ tests/services/jobs/workers/`). If a fitness-repo test module already exists, add to it rather than creating a duplicate. The paths below are the expected locations; adjust to match the repo's actual layout.

---

## Task 1: Repository query — `list_users_with_active_auth`

**Files:**
- Modify: `src/journal/db/fitness_repository.py` (add method after `get_auth_status`, ~line 214)
- Test: `tests/db/test_fitness_repository.py`

- [ ] **Step 1: Write the failing test**

Add to the fitness-repo test module. This fixture builds a repo over an in-memory DB with migrations applied — match the existing pattern in that test file for constructing a `FitnessRepository` (it will already have a fixture; reuse it). Insert users first (the `user_id` FK references `users(id)`).

```python
def test_list_users_with_active_auth_returns_only_valid_rows(fitness_repo, conn):
    # Users 1-5 exist (insert via the test's user-seeding helper / direct SQL).
    # Strava: user 1 has a token (ok), user 2 has a token but auth_status='broken',
    # user 3 has an empty-string token (no creds), user 4 has NULL token.
    conn.executemany(
        "INSERT INTO fitness_auth_state (user_id, source, access_token, "
        "extra_state_json, auth_status) VALUES (?, ?, ?, ?, ?)",
        [
            (1, "strava", "tok-1", "{}", "ok"),
            (2, "strava", "tok-2", "{}", "broken"),
            (3, "strava", "", "{}", "ok"),
            (4, "strava", None, "{}", "unknown"),
            # Garmin: user 1 has a tokens_blob (ok), user 5 has empty blob, user 2 NULL.
            (1, "garmin", None, '{"tokens_blob": "blob-1"}', "ok"),
            (5, "garmin", None, '{"tokens_blob": ""}', "ok"),
            (2, "garmin", None, "{}", "unknown"),
        ],
    )
    conn.commit()

    assert fitness_repo.list_users_with_active_auth(source="strava") == [1]
    assert fitness_repo.list_users_with_active_auth(source="garmin") == [1]


def test_list_users_with_active_auth_empty_when_none(fitness_repo):
    assert fitness_repo.list_users_with_active_auth(source="strava") == []
```

> If the existing fixture doesn't expose a raw `conn`, use `fitness_repo.connection` (the public property at `fitness_repository.py:179`) to run the INSERTs. Adjust the user-seeding to whatever helper the test module already uses; if none, insert minimal `users` rows directly: `INSERT INTO users (id, email, password_hash, ...) VALUES (...)` matching the users-table schema, or reuse a `user_repo` fixture.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/db/test_fitness_repository.py -k list_users_with_active_auth -v`
Expected: FAIL with `AttributeError: 'FitnessRepository' object has no attribute 'list_users_with_active_auth'`

- [ ] **Step 3: Write minimal implementation**

Add this method to `FitnessRepository` (after `get_auth_status`, ~line 214). The SQL mirrors the fetch services' `_has_credentials`: Strava needs a non-empty `access_token`; Garmin needs a non-empty `tokens_blob`. Both exclude `auth_status = 'broken'`.

```python
def list_users_with_active_auth(self, *, source: str) -> list[int]:
    """User IDs that have working credentials for ``source``.

    Mirrors the fetch services' ``_has_credentials`` rule so the daily
    scheduler (services/fitness/scheduler.py) only enqueues syncs that
    can actually run:

    - strava: non-empty ``access_token`` (``bool(auth.access_token)``)
    - garmin: non-empty ``tokens_blob`` in ``extra_state_json``

    Rows with ``auth_status = 'broken'`` are excluded for both sources.
    Returns user IDs in ascending order (deterministic for tests).
    """
    if source == "strava":
        cred_clause = "access_token IS NOT NULL AND access_token != ''"
    elif source == "garmin":
        cred_clause = (
            "json_extract(extra_state_json, '$.tokens_blob') IS NOT NULL "
            "AND json_extract(extra_state_json, '$.tokens_blob') != ''"
        )
    else:
        raise ValueError(f"Unknown fitness source: {source!r}")

    conn = self._conn()
    rows = conn.execute(
        f"SELECT DISTINCT user_id FROM fitness_auth_state "
        f"WHERE source = ? AND auth_status != 'broken' AND {cred_clause} "
        f"ORDER BY user_id",
        (source,),
    ).fetchall()
    return [row["user_id"] for row in rows]
```

> The f-string only interpolates the hard-coded `cred_clause` (no user input), and `source` is still passed as a bound parameter — no injection surface.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/db/test_fitness_repository.py -k list_users_with_active_auth -v`
Expected: PASS (both tests)

- [ ] **Step 5: Add the drift-guard test**

This pins the SQL predicate to the live `_has_credentials` logic so the two can't silently diverge. It builds a `FitnessAuthState` and asserts the Python check and a representative SQL-row agree.

```python
from journal.services.fitness.fetch import StravaFetchService, GarminFetchService
from journal.models import FitnessAuthState  # adjust import to where FitnessAuthState lives


def test_credential_rule_matches_has_credentials(fitness_repo, conn):
    # Strava: access_token present -> both Python and SQL say "active".
    conn.execute(
        "INSERT INTO fitness_auth_state (user_id, source, access_token, "
        "extra_state_json, auth_status) VALUES (10, 'strava', 'tok', '{}', 'ok')",
    )
    conn.commit()
    state = fitness_repo.get_auth_state(user_id=10, source="strava")
    # Python rule (Strava base _has_credentials is bool(access_token)).
    assert bool(state.access_token) is True
    # SQL rule agrees.
    assert 10 in fitness_repo.list_users_with_active_auth(source="strava")
```

> Locate the real import path for `FitnessAuthState` (`grep -rn "class FitnessAuthState" src/`). If instantiating `StravaFetchService`/`GarminFetchService` to call `_has_credentials` directly is heavy (constructor needs repo/notifier/config), it is sufficient to assert against the documented rule (`bool(access_token)` / `bool(tokens_blob)`) as shown — the point is a test that fails if someone changes one side without the other. Keep it lightweight.

- [ ] **Step 6: Run the drift-guard test**

Run: `uv run pytest tests/db/test_fitness_repository.py -k credential_rule -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/journal/db/fitness_repository.py tests/db/test_fitness_repository.py
git commit -m "feat(fitness): list_users_with_active_auth for daily sync scheduler"
```

---

## Task 2: `quiet_success` param plumbing (validation + runner)

**Files:**
- Modify: `src/journal/services/jobs/validation.py:68-70`
- Modify: `src/journal/services/jobs/runner.py:531-566`
- Test: `tests/services/jobs/test_runner.py` (or wherever `submit_fitness_sync_*` is tested)

- [ ] **Step 1: Write the failing test**

Find the existing test for `submit_fitness_sync_strava` (`grep -rn "submit_fitness_sync_strava" tests/`) and mirror its fixture (a `JobRunner` wired with fake fetch/normalize callables). Add:

```python
def test_submit_fitness_sync_strava_passes_quiet_success(job_runner_with_fitness):
    runner, jobs_repo = job_runner_with_fitness
    job = runner.submit_fitness_sync_strava(user_id=7, quiet_success=True)
    created = jobs_repo.get(job.id)  # adjust to the repo's read method
    assert created.params["quiet_success"] is True


def test_submit_fitness_sync_strava_quiet_success_defaults_false(job_runner_with_fitness):
    runner, jobs_repo = job_runner_with_fitness
    job = runner.submit_fitness_sync_strava(user_id=7)
    created = jobs_repo.get(job.id)
    assert created.params.get("quiet_success", False) is False
```

> Match the existing test's way of reading back the stored params (it may assert on the job object directly, or on a fake jobs repo). If the existing tests inspect `params` differently, follow that style.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/jobs/test_runner.py -k quiet_success -v`
Expected: FAIL — `submit_fitness_sync_strava() got an unexpected keyword argument 'quiet_success'`

- [ ] **Step 3a: Add the key to the allow-list**

In `src/journal/services/jobs/validation.py`, change `FITNESS_SYNC_KEYS` (lines 68-70):

```python
FITNESS_SYNC_KEYS: dict[str, type | tuple[type, ...]] = {
    "user_id": int,
    # Optional: scheduled (daily) syncs set this True so the worker stays
    # quiet on a success that fetched zero new rows. Manual syncs omit it.
    "quiet_success": bool,
}
```

- [ ] **Step 3b: Thread the param through both submit methods**

In `src/journal/services/jobs/runner.py`, update `submit_fitness_sync_strava` (line 531) and `submit_fitness_sync_garmin` (line 551). For Strava:

```python
def submit_fitness_sync_strava(
    self, *, user_id: int, quiet_success: bool = False,
) -> Job:
    """Queue a Strava fitness sync (fetch + normalize end-to-end).

    Raises ``RuntimeError`` if the runner was constructed without a
    Strava fetch + normalize pair — the worker has nothing to call
    in that case, so we fail at submit time rather than queueing a
    row that's guaranteed to fail.

    ``quiet_success`` (set by the daily scheduler) makes the worker
    suppress the success notification when the run fetched no new rows.
    """
    if self._ctx.fetch_strava is None or self._ctx.normalize_strava is None:
        raise RuntimeError(
            "Strava fitness sync is not configured on this server "
            "(no fetch_strava_callable / normalize_strava_callable "
            "passed to JobRunner)",
        )
    params: dict[str, Any] = {"user_id": user_id}
    if quiet_success:
        params["quiet_success"] = True
    validate_params(params, FITNESS_SYNC_KEYS, job_type="fitness_sync_strava")
    job = self._jobs.create("fitness_sync_strava", params, user_id=user_id)
    self._executor.submit(run_fitness_sync_strava, self._ctx, job.id, params)
    return job
```

Apply the identical change to `submit_fitness_sync_garmin` (Garmin gate text, `fetch_garmin`/`normalize_garmin`, `run_fitness_sync_garmin`, `job_type="fitness_sync_garmin"`):

```python
def submit_fitness_sync_garmin(
    self, *, user_id: int, quiet_success: bool = False,
) -> Job:
    """Queue a Garmin fitness sync (fetch + normalize end-to-end).

    Same configuration gate as ``submit_fitness_sync_strava``.
    ``quiet_success`` suppresses the success notification on a no-new-rows run.
    """
    if self._ctx.fetch_garmin is None or self._ctx.normalize_garmin is None:
        raise RuntimeError(
            "Garmin fitness sync is not configured on this server "
            "(no fetch_garmin_callable / normalize_garmin_callable "
            "passed to JobRunner)",
        )
    params: dict[str, Any] = {"user_id": user_id}
    if quiet_success:
        params["quiet_success"] = True
    validate_params(params, FITNESS_SYNC_KEYS, job_type="fitness_sync_garmin")
    job = self._jobs.create("fitness_sync_garmin", params, user_id=user_id)
    self._executor.submit(run_fitness_sync_garmin, self._ctx, job.id, params)
    return job
```

> `quiet_success` is only written into `params` when True. This keeps existing manual-sync job rows byte-identical (no new key), so any tests asserting exact params for manual syncs stay green.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/jobs/test_runner.py -k quiet_success -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/journal/services/jobs/validation.py src/journal/services/jobs/runner.py tests/services/jobs/test_runner.py
git commit -m "feat(fitness): thread quiet_success through fitness sync submit"
```

---

## Task 3: Worker — suppress success notify on quiet no-op runs

**Files:**
- Modify: `src/journal/services/jobs/workers/fitness_sync_strava.py:72-80`
- Modify: `src/journal/services/jobs/workers/fitness_sync_garmin.py` (analogous block)
- Test: `tests/services/jobs/workers/test_fitness_sync_strava.py` and `...garmin.py`

- [ ] **Step 1: Write the failing test (Strava)**

Find the existing worker test (`grep -rn "run_fitness_sync_strava" tests/`) and reuse its `ctx` fixture (a fake `WorkerContext` with stubbed `jobs`, `fetch_strava`, `normalize_strava`, `notifier`). The notifier should be a mock/spy recording `notify_success` calls. Add three cases:

```python
def test_quiet_success_suppresses_notify_when_no_new_rows(strava_ctx):
    ctx, notifier = strava_ctx
    ctx.fetch_strava.return_value = _result(status="success", run_id=1, rows_fetched=0)
    run_fitness_sync_strava(ctx, "job-1", {"user_id": 7, "quiet_success": True})
    notifier.notify_success.assert_not_called()
    ctx.jobs.mark_succeeded.assert_called_once()  # job still recorded as succeeded


def test_quiet_success_still_notifies_when_new_rows(strava_ctx):
    ctx, notifier = strava_ctx
    ctx.fetch_strava.return_value = _result(status="success", run_id=1, rows_fetched=3)
    run_fitness_sync_strava(ctx, "job-1", {"user_id": 7, "quiet_success": True})
    notifier.notify_success.assert_called_once()


def test_manual_success_always_notifies(strava_ctx):
    ctx, notifier = strava_ctx
    ctx.fetch_strava.return_value = _result(status="success", run_id=1, rows_fetched=0)
    run_fitness_sync_strava(ctx, "job-1", {"user_id": 7})  # no quiet_success
    notifier.notify_success.assert_called_once()
```

Where `_result` builds a `FitnessSyncResult`:

```python
from journal.services.fitness.fetch import FitnessSyncResult

def _result(*, status, run_id, rows_fetched, rows_normalized=0):
    return FitnessSyncResult(
        status=status, run_id=run_id,
        rows_fetched=rows_fetched, rows_normalized=rows_normalized,
    )
```

> Match the existing worker-test fixture exactly — it already constructs a `WorkerContext` with the right attributes and stubs `normalize_strava` to return a `NormalizeResult`. If the existing fixture names differ, adapt. The key assertions are `notify_success` called / not-called.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/jobs/workers/test_fitness_sync_strava.py -k quiet -v`
Expected: FAIL — `test_quiet_success_suppresses_notify_when_no_new_rows` fails because `notify_success` IS called.

- [ ] **Step 3: Implement suppression (Strava)**

In `src/journal/services/jobs/workers/fitness_sync_strava.py`, replace the success-path block (lines 72-80):

```python
        normalize_result = ctx.normalize_strava(
            user_id=user_id, sync_run_id=fetch_result.run_id,
        )
        result: dict[str, Any] = {
            "fetch": asdict(fetch_result),
            "normalize": asdict(normalize_result),
        }
        ctx.jobs.mark_succeeded(job_id, result)
        # Scheduled (quiet_success) syncs stay silent on a no-op run — a
        # successful fetch that returned zero new rows. Auth/transient
        # failures notify above; manual syncs always notify here.
        quiet = bool(params.get("quiet_success")) and fetch_result.rows_fetched == 0
        if not quiet:
            ctx.notifier.notify_success(user_id, "fitness_sync_strava", result)
```

- [ ] **Step 4: Run test to verify it passes (Strava)**

Run: `uv run pytest tests/services/jobs/workers/test_fitness_sync_strava.py -k quiet -v`
Expected: PASS (all three)

- [ ] **Step 5: Repeat for Garmin**

Add the analogous three tests to `tests/services/jobs/workers/test_fitness_sync_garmin.py` (using `ctx.fetch_garmin`, `run_fitness_sync_garmin`, job type `"fitness_sync_garmin"`), then apply the identical suppression edit to `src/journal/services/jobs/workers/fitness_sync_garmin.py`'s success block:

```python
        quiet = bool(params.get("quiet_success")) and fetch_result.rows_fetched == 0
        if not quiet:
            ctx.notifier.notify_success(user_id, "fitness_sync_garmin", result)
```

> Read the Garmin worker first — its success block should match the Strava shape, but confirm the exact `notify_success` job-type string and surrounding lines before editing.

- [ ] **Step 6: Run both worker test modules**

Run: `uv run pytest tests/services/jobs/workers/test_fitness_sync_strava.py tests/services/jobs/workers/test_fitness_sync_garmin.py -v`
Expected: PASS (all, including pre-existing cases)

- [ ] **Step 7: Commit**

```bash
git add src/journal/services/jobs/workers/fitness_sync_strava.py src/journal/services/jobs/workers/fitness_sync_garmin.py tests/services/jobs/workers/test_fitness_sync_strava.py tests/services/jobs/workers/test_fitness_sync_garmin.py
git commit -m "feat(fitness): quiet success notify on no-op scheduled syncs"
```

---

## Task 4: `FitnessSyncScheduler` — next-fire-time helper (pure function)

**Files:**
- Create: `src/journal/services/fitness/scheduler.py`
- Test: `tests/services/fitness/test_scheduler.py`

Build the scheduler in two tasks: the pure time math first (Task 4), then the thread + enqueue logic (Task 5). The pure function is trivially testable without threads or sleeps.

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime

from journal.services.fitness.scheduler import next_fire_after


def test_next_fire_later_today():
    # 09:00 -> same day 17:00
    now = datetime(2026, 6, 14, 9, 0, 0)
    assert next_fire_after(now, hour=17) == datetime(2026, 6, 14, 17, 0, 0)


def test_next_fire_rolls_to_tomorrow_when_past_hour():
    # 17:30 -> next day 17:00
    now = datetime(2026, 6, 14, 17, 30, 0)
    assert next_fire_after(now, hour=17) == datetime(2026, 6, 15, 17, 0, 0)


def test_next_fire_exactly_on_the_hour_rolls_forward():
    # exactly 17:00:00 -> treat as already fired, go to tomorrow
    now = datetime(2026, 6, 14, 17, 0, 0)
    assert next_fire_after(now, hour=17) == datetime(2026, 6, 15, 17, 0, 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/fitness/test_scheduler.py -k next_fire -v`
Expected: FAIL — `ModuleNotFoundError` / `cannot import name 'next_fire_after'`

- [ ] **Step 3: Write minimal implementation**

Create `src/journal/services/fitness/scheduler.py` with the module docstring and the pure helper (the class comes in Task 5):

```python
"""Daily fitness auto-sync scheduler.

A daemon thread (modeled on :class:`journal.services.health_poll.HealthPoller`)
that wakes once per day at a fixed local hour (default 17:00) and enqueues an
incremental sync for every user with working credentials per source. See
``docs/superpowers/specs/2026-06-14-daily-fitness-auto-sync-design.md``.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def next_fire_after(now: datetime, *, hour: int) -> datetime:
    """Next occurrence of ``hour``:00:00 strictly after ``now``.

    If ``now`` is at or past today's fire time, returns tomorrow's. Naive
    datetimes throughout — the server runs in UTC (Docker), so "local" is
    UTC there; tests pass naive datetimes directly.
    """
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/fitness/test_scheduler.py -k next_fire -v`
Expected: PASS (all three)

- [ ] **Step 5: Commit**

```bash
git add src/journal/services/fitness/scheduler.py tests/services/fitness/test_scheduler.py
git commit -m "feat(fitness): next_fire_after helper for daily sync scheduler"
```

---

## Task 5: `FitnessSyncScheduler` — enqueue logic + daemon thread

**Files:**
- Modify: `src/journal/services/fitness/scheduler.py`
- Test: `tests/services/fitness/test_scheduler.py`

- [ ] **Step 1: Write the failing test for `run_daily_sync`**

Use fakes — no real threads, no DB. A fake repo returns canned user lists; a fake runner records submit calls.

```python
from unittest.mock import MagicMock

from journal.services.fitness.scheduler import FitnessSyncScheduler


def _scheduler(strava_users, garmin_users):
    repo = MagicMock()
    repo.list_users_with_active_auth.side_effect = lambda *, source: (
        strava_users if source == "strava" else garmin_users
    )
    runner = MagicMock()
    sched = FitnessSyncScheduler(job_runner=runner, fitness_repo=repo)
    return sched, runner, repo


def test_run_daily_sync_enqueues_per_source():
    # user 1: both, user 2: strava only, user 3: garmin only
    sched, runner, _ = _scheduler(strava_users=[1, 2], garmin_users=[1, 3])
    sched.run_daily_sync()
    runner.submit_fitness_sync_strava.assert_any_call(user_id=1, quiet_success=True)
    runner.submit_fitness_sync_strava.assert_any_call(user_id=2, quiet_success=True)
    assert runner.submit_fitness_sync_strava.call_count == 2
    runner.submit_fitness_sync_garmin.assert_any_call(user_id=1, quiet_success=True)
    runner.submit_fitness_sync_garmin.assert_any_call(user_id=3, quiet_success=True)
    assert runner.submit_fitness_sync_garmin.call_count == 2


def test_run_daily_sync_no_users_is_noop():
    sched, runner, _ = _scheduler(strava_users=[], garmin_users=[])
    sched.run_daily_sync()
    runner.submit_fitness_sync_strava.assert_not_called()
    runner.submit_fitness_sync_garmin.assert_not_called()


def test_run_daily_sync_continues_past_submit_error():
    # A RuntimeError on one user (e.g. Strava not wired) must not abort the run.
    sched, runner, _ = _scheduler(strava_users=[1, 2], garmin_users=[])
    runner.submit_fitness_sync_strava.side_effect = [RuntimeError("not wired"), MagicMock()]
    sched.run_daily_sync()  # must not raise
    assert runner.submit_fitness_sync_strava.call_count == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/fitness/test_scheduler.py -k run_daily_sync -v`
Expected: FAIL — `cannot import name 'FitnessSyncScheduler'`

- [ ] **Step 3: Implement the class**

Append to `src/journal/services/fitness/scheduler.py`. Add the imports at the top of the file (next to the existing `datetime` import):

```python
import logging
import threading
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from journal.services.jobs import JobRunner
    from journal.db.fitness_repository import FitnessRepository

log = logging.getLogger(__name__)

_SOURCES = ("strava", "garmin")
_DEFAULT_HOUR = 17
_POLL_SLICE = 60  # seconds; max latency for stop() to take effect
```

Then the class:

```python
class FitnessSyncScheduler:
    """Daemon thread that enqueues a daily per-user fitness sync.

    Mirrors :class:`HealthPoller`'s lifecycle (``start``/``stop``/
    ``is_running``). Each day at ``hour``:00 local it asks the repo which
    users have active auth per source and submits incremental syncs via
    the JobRunner with ``quiet_success=True``. ``enabled=False`` makes
    ``start()`` a no-op (used by tests and the FITNESS_SYNC_ENABLED gate).
    """

    def __init__(
        self,
        *,
        job_runner: JobRunner,
        fitness_repo: FitnessRepository,
        hour: int = _DEFAULT_HOUR,
        enabled: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._runner = job_runner
        self._repo = fitness_repo
        self._hour = hour
        self._enabled = enabled
        self._clock: Callable[[], datetime] = clock or datetime.now
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def run_daily_sync(self) -> None:
        """Enqueue syncs for all users with active auth, per source.

        One bad submit (e.g. a RuntimeError because Strava isn't wired,
        or a transient JobRunner error) is logged and skipped so the rest
        of the run still happens.
        """
        for source in _SOURCES:
            submit = (
                self._runner.submit_fitness_sync_strava
                if source == "strava"
                else self._runner.submit_fitness_sync_garmin
            )
            try:
                user_ids = self._repo.list_users_with_active_auth(source=source)
            except Exception:  # noqa: BLE001 — never let one source abort the run
                log.exception("daily fitness sync: failed to list %s users", source)
                continue
            enqueued = 0
            for user_id in user_ids:
                try:
                    submit(user_id=user_id, quiet_success=True)
                    enqueued += 1
                except Exception:  # noqa: BLE001 — skip one user, keep going
                    log.exception(
                        "daily fitness sync: %s submit failed for user %d",
                        source, user_id,
                    )
            log.info("daily fitness sync: %s=%d enqueued", source, enqueued)

    def start(self) -> None:
        """Start the daemon thread (no-op if disabled)."""
        if not self._enabled:
            log.info("Fitness sync scheduler disabled (FITNESS_SYNC_ENABLED=false)")
            return
        self._thread = threading.Thread(
            target=self._run, name="fitness-sync-scheduler", daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal stop and join. Idempotent; safe before start()."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        log.info("Fitness sync scheduler started (fires daily at %02d:00)", self._hour)
        next_fire = next_fire_after(self._clock(), hour=self._hour)
        while not self._stop_event.is_set():
            now = self._clock()
            if now >= next_fire:
                try:
                    self.run_daily_sync()
                except Exception:  # noqa: BLE001 — keep the thread alive to next day
                    log.exception("daily fitness sync run failed")
                next_fire = next_fire_after(self._clock(), hour=self._hour)
            # Sleep in <=60s slices so stop() takes effect promptly even
            # though the next fire may be ~24h away. No shutdown log here:
            # stop() runs from an atexit hook (see HealthPoller for the
            # closed-stream rationale).
            remaining = (next_fire - self._clock()).total_seconds()
            self._stop_event.wait(min(_POLL_SLICE, max(0.0, remaining)))
```

> `Callable` is imported from `typing` for the annotation; `datetime.now` (the bound method) is the default clock and returns naive local time. The `TYPE_CHECKING` imports avoid a circular import with `services/jobs` at module load.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/fitness/test_scheduler.py -k run_daily_sync -v`
Expected: PASS (all three)

- [ ] **Step 5: Add a lifecycle test (clean start/stop, no thread leak)**

```python
def test_start_stop_lifecycle_disabled_is_noop():
    repo = MagicMock()
    runner = MagicMock()
    sched = FitnessSyncScheduler(job_runner=runner, fitness_repo=repo, enabled=False)
    sched.start()
    assert sched.is_running() is False
    sched.stop()  # must not raise


def test_start_then_stop_joins_thread():
    repo = MagicMock()
    repo.list_users_with_active_auth.return_value = []
    runner = MagicMock()
    # Fire hour far from "now" so the thread just sleeps then we stop it.
    sched = FitnessSyncScheduler(
        job_runner=runner, fitness_repo=repo, enabled=True,
    )
    sched.start()
    assert sched.is_running() is True
    sched.stop(timeout=5.0)
    assert sched.is_running() is False
```

> `test_start_then_stop_joins_thread` spawns a real daemon thread; it MUST be stopped in the test (the `stop()` call). This guards against the thread-leak class of CI segfault flagged in project memory. If the test fixture supports teardown, also call `sched.stop()` in a `finally`/fixture-teardown to be safe even if an assert fails mid-test.

- [ ] **Step 6: Run the lifecycle tests**

Run: `uv run pytest tests/services/fitness/test_scheduler.py -v`
Expected: PASS (all), and the run returns promptly (no hang).

- [ ] **Step 7: Commit**

```bash
git add src/journal/services/fitness/scheduler.py tests/services/fitness/test_scheduler.py
git commit -m "feat(fitness): FitnessSyncScheduler daemon thread + run_daily_sync"
```

---

## Task 6: Config flag `fitness_sync_enabled`

**Files:**
- Modify: `src/journal/config.py`
- Test: `tests/test_config.py` (or the existing config test module)

- [ ] **Step 1: Write the failing test**

Find the config test module (`grep -rln "class Config\|Config()" tests/`). Mirror an existing bool-env test (e.g. for `preprocess_images`):

```python
def test_fitness_sync_enabled_defaults_true(monkeypatch):
    monkeypatch.delenv("FITNESS_SYNC_ENABLED", raising=False)
    assert Config().fitness_sync_enabled is True


def test_fitness_sync_enabled_respects_env_false(monkeypatch):
    monkeypatch.setenv("FITNESS_SYNC_ENABLED", "false")
    assert Config().fitness_sync_enabled is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -k fitness_sync_enabled -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'fitness_sync_enabled'`

- [ ] **Step 3: Add the field**

In `src/journal/config.py`, add to the `Config` dataclass (place it near other bool flags, following the exact `preprocess_images` pattern at lines 60-64):

```python
    # Daily fitness auto-sync scheduler (services/fitness/scheduler.py).
    # When true (default), a daemon thread enqueues per-user Strava/Garmin
    # syncs once a day at 17:00 server-local time. Set false to disable.
    fitness_sync_enabled: bool = field(
        default_factory=lambda: os.environ.get(
            "FITNESS_SYNC_ENABLED", "true"
        ).lower() in ("1", "true", "yes", "on")
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -k fitness_sync_enabled -v`
Expected: PASS (both)

- [ ] **Step 5: Commit**

```bash
git add src/journal/config.py tests/test_config.py
git commit -m "feat(config): FITNESS_SYNC_ENABLED flag (default on)"
```

---

## Task 7: Bootstrap wiring — start scheduler, stop in shutdown hook

**Files:**
- Modify: `src/journal/mcp_server/bootstrap.py:654-681` (after HealthPoller, inside the shutdown hook)

This task is integration glue. The unit-level behavior is already covered by Tasks 1-6; bootstrap has no isolated unit test (it constructs the whole world). Verification is the full suite + a manual import/boot check.

- [ ] **Step 1: Construct and start the scheduler after the HealthPoller block**

In `src/journal/mcp_server/bootstrap.py`, immediately after `health_poller.start()` / its log line (line 665) and before the shutdown-hook comment (line 667), add:

```python
    # Fitness sync scheduler — daemon thread that enqueues per-user
    # Strava/Garmin syncs once a day at 17:00 server-local (UTC in Docker).
    from journal.services.fitness.scheduler import FitnessSyncScheduler

    fitness_sync_scheduler = FitnessSyncScheduler(
        job_runner=job_runner,
        fitness_repo=fitness_repo,
        enabled=config.fitness_sync_enabled,
    )
    fitness_sync_scheduler.start()
    if config.fitness_sync_enabled:
        log.info("  Fitness sync scheduler started (daily at 17:00 server-local)")
```

- [ ] **Step 2: Stop it in the shutdown hook**

Update `_shutdown_job_runner` (lines 672-679) to stop the scheduler alongside the health poller:

```python
    def _shutdown_job_runner() -> None:
        # Deliberately quiet: atexit runs arbitrarily late (often
        # after pytest or uvicorn has closed stdout/stderr), so any
        # `log.info` here reliably triggers a spurious "I/O on
        # closed file" print from the stdlib logging handler. The
        # JobRunner already logs its own shutdown lifecycle.
        fitness_sync_scheduler.stop()
        health_poller.stop()
        job_runner.shutdown(wait=False)

    atexit.register(_shutdown_job_runner)
```

- [ ] **Step 3 (optional): expose the scheduler in the services dict**

For symmetry/observability, add it to the returned `_services` dict (near `"job_runner"` at line 734) so future code/tests can reach it:

```python
        "fitness_sync_scheduler": fitness_sync_scheduler,
```

> Optional — only if other code needs a handle. The shutdown hook already closes over the local, so this is not required for correctness. Skip if it would force touching unrelated tests that assert the exact `_services` keys.

- [ ] **Step 4: Verify the module imports and bootstrap is syntactically sound**

Run: `uv run python -c "import journal.mcp_server.bootstrap; import journal.services.fitness.scheduler; print('ok')"`
Expected: prints `ok` with no ImportError (catches circular-import or typo issues).

- [ ] **Step 5: Run the full unit suite**

Run: `uv run pytest -m "not integration"`
Expected: all pass (the prior ~2580 unit tests plus the new ones). No new skips, no hangs.

- [ ] **Step 6: Lint**

Run: `uv run ruff check src/ tests/`
Expected: clean (no new findings in the touched files).

- [ ] **Step 7: Commit**

```bash
git add src/journal/mcp_server/bootstrap.py
git commit -m "feat(fitness): wire daily sync scheduler into server bootstrap"
```

---

## Task 8: Docs + journal

**Files:**
- Modify: `src/journal/...` docs — `docs/` (find the fitness/operations doc, e.g. `grep -rln "fitness" docs/`)
- Create: `journal/260614-daily-fitness-autosync.md`

- [ ] **Step 1: Document the feature**

Add a short section to the relevant fitness doc under `docs/` (or create `docs/fitness-auto-sync.md` if no fitness ops doc exists) covering: what it does, the 17:00 server-local timing (UTC in Docker), `FITNESS_SYNC_ENABLED` to disable, "skip broken auth" behavior, quiet-success notifications, and no missed-run catch-up. Link to the spec.

- [ ] **Step 2: Add a journal entry**

Create `journal/260614-daily-fitness-autosync.md` capturing the decision summary (in-process scheduler, 17:00 UTC, skip broken, quiet-success, no catch-up), the components touched, and a pointer to the spec + plan.

- [ ] **Step 3: Commit**

```bash
git add docs/ journal/260614-daily-fitness-autosync.md
git commit -m "docs(fitness): document daily auto-sync scheduler"
```

---

## Task 9: Push and watch CI

- [ ] **Step 1: Push the branch**

```bash
git push -u origin feat/daily-fitness-autosync
```

- [ ] **Step 2: Watch CI**

Run: `gh run watch` (per `server/CLAUDE.md`). If CI fails: read logs, write a failing test reproducing the issue if it's a bug, fix, re-run the full suite locally, commit, push, watch again. Max 3 fix attempts, then flag to the user.

- [ ] **Step 3 (optional, recommended): real-server smoke check**

If a dev stack is available, set `FITNESS_SYNC_ENABLED=true` and a temporary near-future hour (you can patch `_DEFAULT_HOUR`/pass `hour=` only in a throwaway check, not committed) to confirm `run_daily_sync` enqueues jobs for a seeded user. The committed code keeps 17:00. Do not commit any temporary hour change.

---

## Self-Review (completed during planning)

**Spec coverage:**
- Component 1 (repo query) → Task 1. ✓
- Component 2 (scheduler thread + run_daily_sync) → Tasks 4 + 5. ✓
- Component 3 (quiet-success notifications) → Tasks 2 (plumbing) + 3 (worker suppression). ✓
- Component 4 (bootstrap + shutdown wiring) → Task 7. ✓
- Config `FITNESS_SYNC_ENABLED` → Task 6. ✓
- Error handling (per-user/per-source isolation; loop survives exceptions) → Task 5 `run_daily_sync` try/except + `_run` try/except, tested in Task 5 Step 1. ✓
- Skip broken auth → Task 1 SQL `auth_status != 'broken'`, tested. ✓
- No catch-up → Task 5 `_run` computes only the next fire; missed days are skipped. ✓
- Testing (repo query, run_daily_sync mix, next-fire math, quiet-success, lifecycle/no-leak) → Tasks 1,3,4,5. ✓
- Docs + journal (global rules) → Task 8. ✓
- Push + CI (CLAUDE.md) → Task 9. ✓

**Type consistency:** `FitnessSyncResult(status, run_id, rows_fetched, rows_normalized)` used consistently (Tasks 2,3); `submit_fitness_sync_{strava,garmin}(*, user_id, quiet_success=False)` consistent across Tasks 2,5,7; `list_users_with_active_auth(*, source)` consistent across Tasks 1,5; `next_fire_after(now, *, hour)` consistent across Tasks 4,5; scheduler ctor `FitnessSyncScheduler(*, job_runner, fitness_repo, hour=17, enabled=True, clock=None)` consistent across Tasks 5,7.

**Placeholder scan:** No TBD/TODO/"handle edge cases" — every code step shows full code. Test-fixture reuse is flagged with explicit `grep` commands to locate the real fixtures, since exact existing test-module/fixture names vary and must be confirmed at implementation time.
