# SQLite Threading and Connection Safety

**Status:** active. **Last updated:** 2026-05-09. **Supersedes:** none.

This document records a production-grade threading bug found in the job repository, how it was diagnosed, and the
patterns that prevent it. It applies to any code that shares a `sqlite3.Connection` across threads.

The connection.py docstring referenced below also points at `docs/refactor-follow-ups.md` (now archived to
`docs/archive/refactor-follow-ups.md`) for the item 1.1 reopen criteria.

**Update 2026-05-11:** the same class of race surfaced again as
`OperationalError: cannot commit - no transaction is active` (mood-scoring worker on
prod). A narrow workaround landed in `SQLiteJobRepository._commit()` — see
[`sqlite-per-thread-connections-plan.md`](./sqlite-per-thread-connections-plan.md) for the
proper structural fix that retires the shared-connection model entirely.

## The bug

`SQLiteJobRepository` methods were called from two threads simultaneously:

- **API handler thread** (Starlette/ASGI) — calls `create()` when a user submits a job via POST
- **Executor thread** (`JobRunner`'s single-worker `ThreadPoolExecutor`) — calls `mark_running()`, `update_progress()`,
  `mark_succeeded()` as the job executes

Both threads issued `execute()` + `commit()` on the **same `sqlite3.Connection`**. When the timing aligned, the
concurrent commits produced:

```
sqlite3.OperationalError: not an error
```

This surfaced as an intermittent 500 on job submission — roughly 1 in 5 runs under test load.

## Why it was hard to find

1. **Production hid it.** Real LLM calls take seconds, so the API handler thread's `create()` almost never overlaps
   with the executor thread's `mark_running()`. The race window is microseconds.

2. **Tests hid it too.** Fake services complete instantly, making the race much more likely — but the test used
   `raise_server_exceptions=False` (needed for other tests that assert on error responses), so the 500 was silently
   swallowed. The test saw `total == 1` instead of `total == 2` and the failure message pointed at the wrong thing.

3. **The error message is misleading.** `"not an error"` is SQLite's way of saying "the connection was used
   concurrently in an unsafe way." It does not explain what went wrong.

4. **`check_same_thread=False` suppresses the only guard.** Python's `sqlite3` module raises `ProgrammingError` by
   default when a connection is used from a different thread. Setting `check_same_thread=False` disables this check
   entirely — it does not make the connection thread-safe.

## The fix

The originally-attempted fix (item 1.1) was a `threading.Lock` in `SQLiteJobRepository` wrapping each `execute()` +
`commit()` pair:

```python
class SQLiteJobRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()

    def create(self, job_type: str, params: dict) -> Job:
        with self._lock:
            self._conn.execute("INSERT INTO jobs ...", (...))
            self._conn.commit()
```

The lock **still ships in the repo today**, but it turned out to be insufficient on its own — the multi-step
"execute → fetchone()" or "execute → lastrowid → commit" windows are still exposed (see the WARNING block in
`src/journal/db/connection.py`). The race that the lock failed to close was the within-call race in
`submit_save_entry_pipeline`, where the API thread was creating job rows while the worker thread was already starting
to read them.

**The shipped fix** for that specific race (and the only race we've actually observed in production) is to **defer
worker dispatch** until the API thread's writes complete:

- `submit_save_entry_pipeline` writes the parent + child job rows synchronously on the API thread, then schedules the
  worker to start *after* the writes are committed.
- This avoids the cross-thread interleaving entirely for the only path that surfaced the bug.

Cross-call races (two unrelated workers writing simultaneously) remain theoretical — workers spend most of their time
in LLM calls, not SQLite, so concurrent SQLite writes are rare. The repository lock catches the simple cases; the
deferred-dispatch fix catches the only one that actually mattered. If new `sqlite3.OperationalError: not an error`
reports surface in prod logs, the proper structural fix is per-thread connections (option 1 in the connection.py
docstring), which is a real architectural change.

## Rules for SQLite connections in this codebase

1. **One connection = one lock.** If a `sqlite3.Connection` is used with `check_same_thread=False`, the code that
   owns it must also own a `threading.Lock` and hold it for every `execute()` + `commit()` pair.

2. **`check_same_thread=False` is not thread safety.** It only disables Python's runtime check. The underlying
   SQLite C library does not serialize access on a single connection — that is the caller's responsibility.

3. **WAL mode does not help here.** WAL allows concurrent *readers* across *different connections*. It does not
   make concurrent *writes* on the *same connection* safe.

4. **Hold the lock across execute + commit, not just one of them.** A lock around only `execute()` still leaves
   a window where another thread can call `execute()` between the first thread's `execute()` and `commit()`,
   interleaving their implicit transactions.

5. **Reads need the lock too.** A `SELECT` on a connection with a pending uncommitted `INSERT` from another thread
   can see inconsistent state or trigger the same `OperationalError`.

## How the bug was diagnosed

1. **Added status code assertions** to the test's POST calls. This surfaced that the second POST was returning 500
   instead of 202 — proving the server was erroring, not silently losing data.

2. **Temporarily set `raise_server_exceptions=True`** on the `TestClient`. This produced the full stack trace ending
   at `self._conn.commit()` with `sqlite3.OperationalError: not an error`.

3. **Stress-tested** by running the test file 50 times in a loop. Before the fix: ~20% failure rate. After: 0/50.

## How to stress-test for threading races

When investigating flaky tests that involve threads (executor pools, background workers, async handlers):

```bash
# Run a single test file N times, stop on first failure
for i in $(seq 1 50); do
  result=$(uv run pytest tests/test_file.py -x -q 2>&1)
  if echo "$result" | grep -q "FAILED"; then
    echo "Failed on run $i"
    echo "$result"
    break
  fi
done
```

If a test fails intermittently (say 1 in 10 runs), a 50-run loop will almost certainly catch it. If it passes 50
times, the fix is likely correct.

## Files involved

- `src/journal/db/jobs_repository.py` — the per-method `threading.Lock` lives here.
- `src/journal/db/connection.py` — creates connections; the docstring is the authoritative explanation of why the lock
  approach is incomplete and what the residual risks are.
- `src/journal/services/jobs/runner.py` — `JobRunner` calls repository methods from the executor thread (formerly
  `services/jobs.py`; split into the `services/jobs/` package on 2026-05-07).
- `src/journal/api/ingestion.py` (and other `src/journal/api/*` route modules) — API handlers call `submit_*()` (which
  calls `create()`) from the ASGI thread; `submit_save_entry_pipeline` defers worker dispatch.

**PRAGMAs set on connection open** (`connection.py:67-71`): `journal_mode=WAL`, `synchronous=NORMAL`,
`cache_size=-64000`, `busy_timeout=5000`, `foreign_keys=ON`.
