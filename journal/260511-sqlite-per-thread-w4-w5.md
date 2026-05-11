# SQLite per-thread connections — W4 + W5 (plan close)

Plan: [`docs/archive/sqlite-per-thread-connections-plan.md`](../docs/archive/sqlite-per-thread-connections-plan.md).
Builds on [W1 + W2](./260511-sqlite-per-thread-w1-w2.md) and
[W3](./260511-sqlite-per-thread-w3.md).

W4 retires the bare-`Connection` constructor branch on every migrated
repo, drops the shared `check_same_thread=False` connection from
`bootstrap.py`, removes the per-repo `_lock` and the
`SQLiteJobRepository._commit()` no-transaction workaround, deletes
`TestSharedConnectionCommitRace`, removes the `check_same_thread`
parameter from `db/connection.py`, and adds a negative cross-thread
test in `tests/test_db/test_factory.py`. W5 closes the plan by
archiving the plan and the diagnosis doc.

Shipped as three production commits + one docs commit:

| # | Title | Net diff |
|---|---|---|
| C1 | Test-fixture fan-out | +665 -309, 40 files (tests only) |
| C2 | Bootstrap + non-repo migration | +129 -183, 11 files |
| C3 | Legacy branch deletion + cleanup | +833 -1223, 16 files |
| W5 | Docs archive | docs/ moves + roadmap update + src/ comment refs |

Full unit suite: 2291 passing (was 2295 before C3 — five tests in
`TestSharedConnectionCommitRace` removed plus one negative test
added; net −4). Lint clean throughout.

## C1 — test-fixture fan-out

Added a project-wide `factory` fixture in `tests/conftest.py` that
constructs a migrated `ConnectionFactory` on a temp DB. Rebound
`db_conn` to `factory.get()` — tests that read raw SQL via
`db_conn.execute(...)` keep working unchanged; only tests that
construct repos needed their fixture migrated to take `factory`.

API tests (`test_api*.py`, `test_mcp_tools_fitness.py`) had built
their synthetic `_services` dicts with a hand-rolled
`check_same_thread=False` connection feeding the `"db_conn"` slot.
C1 added a sibling `"db_factory"` slot using the new factory and
kept the legacy `"db_conn"` slot temporarily — C2 removed the legacy
callers, and the slot followed them out in the same commit.

Fan-out covered ~36 files; the bulk of the W4 diff. Some gotchas
encountered:

1. `test_ingestion.py` had ~40 sites of `SQLiteEntryRepository(db_conn)`
   spread across test methods that all took `db_conn` as a pytest
   fixture parameter. Migrated by `replace_all` of the constructor
   call + parameter rename (`db_conn` → `factory`) — the same
   ergonomic trick W3 used in the repo-level test files.
2. `test_user_preferences.py` and `test_api.py` derived secondary
   repos from `repo.connection`
   (e.g. `SQLiteEntityStore(repo.connection)`); migrated to
   `SQLiteEntityStore(factory)` instead so every repo in the test
   shares the same factory.
3. `test_jobs_runner.py`, `test_services/test_jobs/test_worker_*.py`
   used their own `threadsafe_conn` fixture (open
   `check_same_thread=False` for the JobRunner worker thread) — the
   factory's per-thread connection makes that fixture trivially
   correct, so the fixture name changed to `threadsafe_factory` and
   the body switched to a `ConnectionFactory`.

## C2 — bootstrap + non-repo migration

The bootstrap no longer opens a shared cross-thread connection. The
`db_factory = ConnectionFactory(config.db_path)` line moved above
`run_migrations`; `run_migrations(db_factory.get())` runs on the
main thread (single-threaded by nature, safe). `HealthPoller` now
takes a `connection_provider: Callable[[], sqlite3.Connection]`
rather than a captured Connection — passing `db_factory.get` means
the daemon poll thread opens its own connection lazily on first
poll, and test calls from the main thread use a `lambda: mock_conn`
provider.

The three API readers that previously fished a Connection out of
`_services["db_conn"]` (pricing GET/PATCH in `api/settings.py`, the
integrity check in `api/fitness.py`, and `_get_db_conn` in
`mcp_server/tools/_ctx.py`) now read
`services["db_factory"].get()` and operate on the calling thread's
connection. The legacy `"db_conn"` slot is gone from `_services`;
no caller remains.

## C3 — legacy branch deletion + cleanup

Six repos lost the bare-Connection branch. Constructor signature is
now `factory: ConnectionFactory`; `_conn()` becomes
`return self._factory.get()` with no `isinstance` switch and no
`_direct_conn`. Three repos (`jobs`, `fitness`, `users`) lost the
per-method `_lock` and every `with self._lock:` block — a Python
script flattened the lock blocks across the ~50 affected methods,
preserving exact indentation of the bodies. `SQLiteJobRepository`
lost the `_commit()` no-transaction workaround; every
`self._commit(conn, "...")` site went back to plain `conn.commit()`.

`db/connection.py` lost the `check_same_thread` parameter. The
function stays as a thin shim used by `migrations.py`,
`ConnectionFactory`, and CLI commands — the WARNING-block docstring
shrunk to a 3-line note pointing at the archived plan.

`TestSharedConnectionCommitRace` and its `_RacyConn` helper were
deleted from `tests/test_db/test_jobs_repository.py`. The race the
class reproduced — concurrent writers on a shared Connection losing
each other's implicit-transaction state — is now structurally
impossible: each repo method calls `factory.get()` and operates on
a thread-local Connection. Future readers see the prod incident in
the W1+W2 journal entry rather than as a still-running test.

A `test_bare_get_connection_also_armed_against_cross_thread_use`
test was added to `tests/test_db/test_factory.py` — opens a
connection via `get_connection`, hands it to another thread, asserts
`sqlite3.ProgrammingError`. Acts as a regression guard for any
future change that re-adds the `check_same_thread` parameter with a
permissive default.

CLI commands (`cli/__init__.py`, `cli/_services.py`, `cli/mood.py`,
`cli/entities.py`, `cli/fitness.py`) now open a
`ConnectionFactory(config.db_path)` instead of calling
`get_connection` directly. CLI commands are still single-threaded;
the factory's per-thread model gives them the same connection on
every call to `factory.get()` within the process. This was scope
creep relative to the original plan (which said CLI tests don't
need migration) but became necessary once C3 deleted the legacy
constructor branch — CLI commands that constructed repos with bare
Connections were the last remaining callers.

## W5 — docs archive

1. `docs/sqlite-per-thread-connections-plan.md` — added
   `**Status:** closed 2026-05-11` header, `git mv` to
   `docs/archive/`.
2. `docs/sqlite-threading.md` — added `**Status:** superseded by
   archive/sqlite-per-thread-connections-plan.md` header, `git mv`
   to `docs/archive/`. (The diagnosis is preserved because it
   explains *why* the shared-Connection model was unsafe — that
   reasoning informs how the factory is supposed to be used.)
3. `docs/roadmap.md` updated to point at the archived locations
   with a "closed 2026-05-11" note.
4. All `docs/sqlite-{per-thread-connections-plan,threading}.md`
   references in `src/journal/**` doctsrings updated to point at
   `docs/archive/` paths.

## Verification

- Full unit suite: `uv run pytest -m "not integration"` →
  2291 passed, 8 deselected. Lint clean.
- `grep -r 'check_same_thread' src/ tests/` returns only the
  factory's docstring and the negative-test docstring.
- `grep -r '_direct_conn\|factory_or_conn\|_RacyConn'` returns
  nothing.
- `grep -r 'with self._lock' src/journal/db/` returns nothing.

## Files touched

C1 (tests only):
- `tests/conftest.py` + 39 other test files.

C2:
- `src/journal/mcp_server/bootstrap.py`
- `src/journal/services/health_poll.py`
- `src/journal/api/settings.py`
- `src/journal/api/fitness.py`
- `src/journal/mcp_server/tools/_ctx.py`
- `tests/test_services/test_health_poll.py`
- `tests/test_api*.py`, `tests/test_mcp_tools_fitness.py`

C3:
- `src/journal/db/{jobs,fitness,user}_repository.py`
- `src/journal/db/repository/store.py`
- `src/journal/entitystore/store.py`
- `src/journal/services/runtime_settings.py`
- `src/journal/db/connection.py`
- `src/journal/cli/{__init__,_services,mood,entities,fitness}.py`
- `tests/test_db/test_jobs_repository.py` (race-class removed)
- `tests/test_db/test_factory.py` (negative test added)
- `tests/test_cli.py`, `tests/test_cli_fitness.py`

W5:
- `docs/{sqlite-per-thread-connections-plan,sqlite-threading}.md`
  → `docs/archive/`
- `docs/roadmap.md` updated
- Source docstring references updated to point at archived paths
