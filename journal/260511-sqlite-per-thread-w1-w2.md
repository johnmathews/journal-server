# SQLite per-thread connections — W1 + W2

Plan: [`docs/sqlite-per-thread-connections-plan.md`](../docs/sqlite-per-thread-connections-plan.md).

Structural fix for the shared-`sqlite3.Connection` race that crashed mood
scoring on 2026-05-11. W1 builds the `ConnectionFactory`; W2 makes
`SQLiteJobRepository` consume it on the production path while keeping the
bare-Connection path working for tests not yet migrated (W3 scope).

## W1 — `ConnectionFactory`

`src/journal/db/factory.py`: per-thread `sqlite3.Connection`s on
`threading.local`, lazily opened on first use via the existing
`get_connection()` helper (so PRAGMAs stay in one place). Connections
are opened with `check_same_thread=True` — the whole point: each thread
owns one connection, and accidental cross-thread use trips Python's
built-in guard instead of silently corrupting state.

Tests in `tests/test_db/test_factory.py` (13 tests across three
classes): single-thread idempotence + close/reopen, PRAGMA application
(WAL, foreign_keys, busy_timeout ≥ 5s, NORMAL sync, `sqlite3.Row`),
cross-thread distinctness, the `check_same_thread` guard staying armed,
and a 4-thread × 25-write concurrent-writer test that exercises
WAL + busy_timeout at the file level.

## W2 — `SQLiteJobRepository` migration

Constructor now accepts `ConnectionFactory | sqlite3.Connection`:

- **Factory path (production):** `_conn()` returns this thread's
  connection from the factory; the race is structurally impossible
  because no two threads share a `Connection`. Every method routes
  through `conn = self._conn()` for execute/commit/fetch.
- **Legacy Connection path:** retained as-is so the ~14 test files
  that still construct `SQLiteJobRepository(conn)` work unchanged.
  The per-method `threading.Lock` and the `_commit()` no-transaction
  workaround stay live on this path — they're no-ops in the factory
  path and protective in the legacy one. W3 retires the legacy path
  along with the rest of the shared-connection model and removes
  both pieces of scaffolding.

`mcp_server/bootstrap.py:552` now constructs
`ConnectionFactory(config.db_path)` and hands it to the jobs repo —
the rest of the bootstrap (Entry / Fitness / RuntimeSettings) keeps
the existing `conn` until W3.

New test class `TestFactoryPathSemantics` (4 tests) covers the
production path: lifecycle round-trip, per-thread distinct connections
through the repo's `connection` property, **6-thread × 20-job
concurrent lifecycle stress** that would have surfaced the prod failure
under the old model, and cross-thread visibility via WAL.

The pre-existing `TestSharedConnectionCommitRace` class stays — it
exercises the legacy path explicitly and gets deleted in W3 when that
path goes away.

## Verification

- Full suite: 2280 passed (was 2263 — 17 new tests).
- Lint: clean.
- 50-iteration stress loop on `test_jobs_repository.py` +
  `test_factory.py`: 50/50 clean.

## Notes / decisions

1. **Hybrid constructor over a hard break.** The plan W2 originally
   said "delete `TestSharedConnectionCommitRace` and migrate every
   fixture." Scoping the call sites surfaced ~14 test files
   constructing the repo (many derive a `Connection` from a sibling
   repo's `.connection` property). Migrating all of them in W2 would
   have bloated the diff well past M-sized and pulled in test
   refactoring that belongs in W3. The hybrid constructor lets W2
   land the production win cleanly while leaving the legacy path
   tests untouched.
2. **`_lock` and `_commit` workaround retained.** Both are no-ops on
   the factory path (one thread per connection, no contention) and
   protective on the legacy path. Removing them prematurely would
   re-open the prod failure for any test that runs `JobRunner`
   against a bare Connection.
3. **`connection` property semantics on factory path.** Returns the
   *calling thread's* connection. For tests this is what they want
   (committed rows are visible via WAL across connections). For any
   future code that captured this property and crossed threads with
   it, `check_same_thread=True` would raise immediately — the
   tripwire is the point.

## Files

- `src/journal/db/factory.py` — new (W1).
- `src/journal/db/jobs_repository.py` — constructor + every method
  routes through `_conn()`; helper takes `conn` parameter.
- `src/journal/mcp_server/bootstrap.py:552` — passes factory.
- `tests/test_db/test_factory.py` — new (W1).
- `tests/test_db/test_jobs_repository.py` — `jobs_factory` /
  `jobs_repo_factory` fixtures + `TestFactoryPathSemantics` class.
