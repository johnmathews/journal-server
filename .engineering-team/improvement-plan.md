# W4 — SQLite per-thread connections, scope-and-implement plan

Working doc for this session. The persistent plan is
`docs/sqlite-per-thread-connections-plan.md` §W4. W5 (docs archive)
follows in a separate commit if W4 stays small enough.

## Recon — verified facts

1. Six migrated repos all use the identical hybrid constructor
   `factory_or_conn: ConnectionFactory | sqlite3.Connection`, with
   `_factory` + `_direct_conn` + `_conn()`:
   - `src/journal/db/jobs_repository.py` — has `_lock` + `_commit()` workaround.
   - `src/journal/db/fitness_repository.py` — has `_lock`.
   - `src/journal/db/user_repository.py` — has `_lock`.
   - `src/journal/db/repository/store.py` — no `_lock`.
   - `src/journal/entitystore/store.py` — no `_lock`.
   - `src/journal/services/runtime_settings.py` — no `_lock`.

2. Bootstrap `src/journal/mcp_server/bootstrap.py:274` opens a process-
   wide `conn = get_connection(config.db_path, check_same_thread=False)`.
   Consumers of this `conn` after W3:
   - `run_migrations(conn)` (bootstrap.py:275).
   - `HealthPoller(conn=conn, ...)` (bootstrap.py:609–614) — captured
     and read from the daemon poll thread via `check_sqlite(self._conn)`.
   - `_services["db_conn"]` slot (bootstrap.py:694) — read by:
     - `src/journal/api/settings.py:53, 189, 206` (pricing reads + writes).
     - `src/journal/api/fitness.py:340` (`check_fitness_integrity`).
     - `src/journal/mcp_server/tools/_ctx.py:51` via `_get_db_conn`,
       called from 4 MCP fitness tools
       (`src/journal/mcp_server/tools/fitness.py:146, 446, 477, 514`).

3. `db/connection.py:get_connection` still has the
   `check_same_thread: bool = True` parameter and the long WARNING
   docstring. After W4 nothing should pass `False`.

4. Tests using `db_conn` fixture: 36 files. Tests constructing repos
   from `repo.connection`: `tests/test_api.py` (3 sites) and
   `tests/test_user_preferences.py` (3 sites). Tests explicitly
   opening `get_connection(..., check_same_thread=False)`: 14 files.

5. `TestSharedConnectionCommitRace` lives at
   `tests/test_db/test_jobs_repository.py:350-499` — five tests, all
   exercise the legacy bare-Connection path.

6. HealthPoller tests (`tests/test_services/test_health_poll.py`) use
   a `MagicMock` for `conn`. Switching HealthPoller's API to
   `connection_provider: Callable[[], sqlite3.Connection]` keeps these
   tests trivial: `lambda: mock_conn` replaces `mock_conn` in the
   fixture, no other change.

## Decisions

1. **HealthPoller takes `connection_provider: Callable[[], Connection]`,
   not a `ConnectionFactory`.** Reason: `poll_once` is called from both
   the daemon thread (production) and the main thread (tests). A
   callable lets each call site fetch its own connection via
   `self._connection_provider()`. Tests stay mock-based; production
   passes `db_factory.get`.
   - **Why not** a `ConnectionFactory` directly: that would force tests
     to either build a real factory or write a mock factory class. A
     plain callable is the same one-liner in production and is
     trivially mockable.

2. **`get_connection` stays as a thin shim**, no parameter changes
   except dropping `check_same_thread`. Migrations + 13 CLI sites
   call it. The factory uses it internally to open each thread's
   connection. Removing it entirely would require either inlining
   the PRAGMAs into the factory + duplicating in CLI helpers, or
   forcing CLI commands through the factory — both worse than a
   thin shim. Plan-promised behaviour: kept for `migrations.py`
   and ad-hoc CLI tools.

3. **`db_conn` test fixture becomes `factory.get()`, not removed.**
   Many tests do raw SQL via `db_conn.execute(...)` and never touch
   a repo. Migrating those to `factory.get()` at every call site is
   pointless noise. Keep the fixture; back it with the factory;
   rebind through a new `factory` fixture. Side-effect: don't
   `conn.close()` in the fixture teardown (the factory owns
   connection lifetime).

4. **API-helper migration: `_services["db_factory"]` replaces
   `_services["db_conn"]`.** The three API readers + `_ctx.py` call
   `services["db_factory"].get()` at the point of use, so each
   request thread reads from its own connection. Slightly more
   verbose than `services["db_conn"]` but matches the production
   threading model.

5. **Commit split: three commits.**
   - C1: test-fixture fan-out — adds the factory fixture, migrates
     ~36 test files to construct repos from `factory`. Legacy
     branch in every repo stays alive. Suite stays green.
   - C2: bootstrap + non-repo migration — drops `_services["db_conn"]`
     and the shared `conn`; `run_migrations` runs through
     `db_factory.get()`; HealthPoller takes a provider; the three API
     helpers + the `_get_db_conn` MCP helper read from
     `_services["db_factory"]` / `lifespan_context["db_factory"]`.
   - C3: cleanup — deletes bare-Connection branch from every repo,
     drops `_lock` on jobs/fitness/users, drops `_commit()` workaround
     on jobs, deletes `TestSharedConnectionCommitRace`, drops
     `check_same_thread` parameter from `get_connection`, adds
     negative cross-thread test in `tests/test_db/test_factory.py`.
   - **Why this order**: C1 unblocks C3 (tests must already pass a
     factory before the legacy branch is removed). C2 is independent
     of both — could ship in any slot — but happens in the middle so
     the bootstrap-related test churn lands while C1's fixture work
     is still fresh, and so C3 inherits a fully-migrated codebase to
     simplify the legacy-branch deletion.

6. **Negative cross-thread test placement**:
   `tests/test_db/test_factory.py` (factory invariant).

## Non-goals

1. Documentation archive moves — W5.
2. Removing `get_connection` entirely.
3. Migrating CLI commands to the factory — they are short-lived,
   single-threaded; the existing `get_connection` calls are correct.
4. Any change to migration mechanics or SQL.
5. Adding more concurrent-write stress tests beyond what W3 already
   shipped.

## Work units (one per commit)

### W4-C1 — Test fixture fan-out (M, Low risk)

- **Changes:**
  - `tests/conftest.py`: introduce `factory` fixture (constructs a
    `ConnectionFactory(tmp_db_path)`, runs migrations via
    `factory.get()`, yields factory, calls `factory.close_current()`
    on teardown). `db_conn` fixture stays but is redefined as
    `def db_conn(factory): return factory.get()` with no close
    (factory owns the lifecycle).
  - Per-file fixtures that build a local `conn` and pass it to
    repos: migrate to a local `factory` and pass `factory` instead.
    Files involved (verified by grep):
    - `tests/test_api.py` — `api_db_conn` (line 50) plus 3
      `SQLiteFOO(repo.connection)` sites.
    - `tests/test_api_ingest.py` — `api_db_conn` (line 57).
    - `tests/test_api_jobs.py` — `conn` fixture (line 149).
    - `tests/test_api_fitness.py` — fixture (line 78).
    - `tests/test_api_fitness_garmin_auth.py` — fixture (line 173).
    - `tests/test_api_fitness_strava_auth.py` — fixture (line 123).
    - `tests/test_auth_api.py` — `auth_db_conn` (line 36).
    - `tests/test_data_isolation.py` — fixture (line 27).
    - `tests/test_lifespan.py` — fixture (line 73).
    - `tests/test_mcp_server.py` — fixture (line 212).
    - `tests/test_mcp_tools_fitness.py` — fixture (line 61).
    - `tests/test_session_hashing.py` — uses shared `db_conn`.
    - `tests/test_user_preferences.py` — `api_db_conn` (line 61) plus
      3 `SQLiteFOO(api_repo.connection)` sites.
    - `tests/test_services/test_jobs_runner.py` — fixture (line 230).
    - `tests/test_services/test_jobs/test_worker_*.py` — three files.
    - `tests/test_services/test_reload.py` — fixture (line 357).
    - Plus 12 simpler `test_db/*` and `test_services/*` files that
      only consume `db_conn` from conftest (no own fixture).
  - `test_api.py:125`, `test_api_fitness.py:177`,
    `test_mcp_tools_fitness.py:156`, `test_api_fitness_garmin_auth.py:204`:
    the synthetic `_services` dict these tests construct currently has
    `"db_conn": conn`. Add a sibling `"db_factory": factory` (keep
    `"db_conn"` until C2 removes the callers).
  - `test_cli*.py` files: 13 sites of `get_connection(db_path)`
    (no `check_same_thread`). These already work and don't need
    migration — they instantiate a connection locally for a single
    CLI command. **Skip touching `test_cli*.py` in W4** — they
    don't pass `check_same_thread=False` anywhere.
- **Test impact**: ~36 files diff-touched, all mechanical. No new
  tests in this commit. Full suite must stay green.
- **Reversibility**: revert; tests-only commit.
- **Dependencies**: none.
- **Acceptance**: full suite `uv run pytest -m "not integration"`
  green; ruff clean.

### W4-C2 — Bootstrap + non-repo migration (M, Medium risk)

- **Changes:**
  - `src/journal/mcp_server/bootstrap.py`:
    - Remove the `conn = get_connection(..., check_same_thread=False)`
      line and the surrounding 13-line WARNING comment.
    - Move `db_factory = ConnectionFactory(config.db_path)` up so it
      runs before `run_migrations`.
    - `run_migrations(db_factory.get())` — main-thread call,
      single-threaded by nature.
    - `HealthPoller(connection_provider=db_factory.get, ...)`.
    - Replace `"db_conn": conn` with `"db_factory": db_factory` in
      `_services`. Remove the trailing comment.
  - `src/journal/services/health_poll.py`:
    - Constructor: `connection_provider: Callable[[], sqlite3.Connection]`.
    - Replace `check_sqlite(self._conn)` with
      `check_sqlite(self._connection_provider())` in `poll_once`.
    - Tests update: `connection_provider=lambda: mock_conn` in the
      fixture (line 70).
  - `src/journal/api/settings.py`:
    - Replace 3× `services.get("db_conn")` with
      `services["db_factory"].get()` (factory key is always present
      once services are initialised, so `.get()` on the dict can
      become `[...]` after the None-guard for `services` itself).
    - The route handlers run on Starlette's worker threads; each
      request thread gets its own connection via the factory.
  - `src/journal/api/fitness.py:340`:
    - Same swap. The local `conn` variable becomes
      `services["db_factory"].get()`.
  - `src/journal/mcp_server/tools/_ctx.py`:
    - `_get_db_conn(ctx)` becomes
      `return ctx.request_context.lifespan_context["db_factory"].get()`.
      Function name and signature stay so the four MCP tool callers
      (fitness.py) don't have to change.
  - `tests/test_lifespan.py`: lifespan-startup tests need to look up
    `db_factory` instead of `db_conn` for any state inspection.
  - `tests/test_api*.py`, `tests/test_mcp_tools_fitness.py`: the
    synthetic services dicts these tests build need `db_factory`
    rather than `db_conn`. (C1 already added the factory key; C2
    removes the legacy `db_conn` key from `_services` so C2 also
    removes it from the test dicts.)
- **Test impact**: 2-4 test files touched for the services-dict
  rename. HealthPoller fixture changed in one place. Full suite
  must stay green.
- **Reversibility**: revert. No schema change.
- **Dependencies**: C1.
- **Acceptance**: bootstrap no longer references `get_connection`
  except indirectly via the factory. `_services` has no `db_conn`
  key. Full suite green. Lint clean.

### W4-C3 — Legacy-branch deletion + cleanup (M, Low risk)

- **Changes:**
  - Six repos: delete bare-Connection branch from constructor.
    - `factory_or_conn: ConnectionFactory | sqlite3.Connection` →
      `factory: ConnectionFactory`. Drop `_direct_conn`, the
      isinstance check, the legacy docstring paragraphs.
    - `_conn()` becomes `return self._factory.get()` (no branching).
    - Files: `src/journal/db/jobs_repository.py`,
      `src/journal/db/fitness_repository.py`,
      `src/journal/db/user_repository.py`,
      `src/journal/db/repository/store.py`,
      `src/journal/entitystore/store.py`,
      `src/journal/services/runtime_settings.py`.
  - Three repos: drop `self._lock = threading.Lock()` and every
    `with self._lock:` block (jobs, fitness, users). Methods become
    flat `execute`/`commit` calls. The `import threading` line goes
    if no other use remains (verify by grep).
  - `src/journal/db/jobs_repository.py`: remove the `_commit()` helper
    (lines 112-135), replace every `self._commit(conn, "…")` call
    with `conn.commit()` (8 sites).
  - `tests/test_db/test_jobs_repository.py`: delete the entire
    `TestSharedConnectionCommitRace` class plus its `_RacyConn`
    helper. The remaining
    `test_other_operational_errors_still_propagate` test verifies
    that non-target `OperationalError` types still propagate; relocate
    to its own small test if the assertion can be reproduced against
    the plain `conn.commit()` path. Read the test body when making
    the call — if the workaround was the only thing being asserted,
    delete the test too.
  - `src/journal/db/connection.py`:
    - Drop `check_same_thread` parameter.
    - Trim the WARNING block down to a 3-4-line note pointing at the
      factory and the plan doc.
  - `tests/test_db/test_factory.py`: add a negative test —
    `test_connection_cannot_cross_threads` — that opens a connection
    via the factory in thread A, hands it to thread B, and asserts
    `thread B's conn.execute(...)` raises `sqlite3.ProgrammingError`.
    Belongs in the existing `TestThreadingGuard` class (or named
    equivalent — verify by reading the file).
- **Test impact**: removes 5 tests in
  `test_jobs_repository.py::TestSharedConnectionCommitRace`. Adds 1
  test in `test_factory.py`. Net -4. All remaining tests stay green
  (C1 already made them factory-only).
- **Reversibility**: revert; no schema change.
- **Dependencies**: C1 + C2.
- **Acceptance**:
  - `grep -r 'check_same_thread' src/ tests/` returns only the
    factory's docstring reference and the new negative test.
  - `grep -r '_direct_conn\|factory_or_conn\|_RacyConn' src/ tests/`
    returns nothing.
  - `grep -r 'TestSharedConnectionCommitRace' tests/` returns nothing.
  - `grep -r 'self._lock' src/journal/db/` returns nothing (other
    repos may legitimately use locks elsewhere — verify each removed
    one is intended).
  - Cross-thread connection test fails before the
    `check_same_thread=True` default and passes after.
  - Full unit suite green; ruff clean; lint passes the strict
    project profile.

## After W4

If C1-C3 stay reviewable and the diff is contained, do W5 in the same
session as a clean separate commit:

1. `docs/sqlite-per-thread-connections-plan.md` → add
   `**Status:** closed YYYY-MM-DD.` header, `git mv` to
   `docs/archive/`.
2. `docs/sqlite-threading.md` → add `**Status:** superseded by
   archive/sqlite-per-thread-connections-plan.md (YYYY-MM-DD).`
   header, `git mv` to `docs/archive/`.
3. Update `docs/roadmap.md` (or equivalent) to reflect.
4. Trim the `connection.py` docstring to point at the archived plan.

If C2 or C3 ends up larger than expected, defer W5 to a fresh session.
