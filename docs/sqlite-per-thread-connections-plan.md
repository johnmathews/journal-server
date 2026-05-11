# SQLite Per-Thread Connections Refactor

**Status:** active. **Last updated:** 2026-05-11. **Supersedes:** none.

This plan moves the server from one shared `sqlite3.Connection` (used across the
ASGI request threads and the `JobRunner` worker thread) to per-thread connections
managed by a connection factory. It is the proper structural fix to the class of
race that produced both the 2026-04-XX `OperationalError: not an error` and the
2026-05-11 `OperationalError: cannot commit - no transaction is active`.

See [`sqlite-threading.md`](./sqlite-threading.md) for the full diagnosis of
*why* the shared-connection model is unsafe (in short: Python's `sqlite3`
driver tracks implicit-transaction state on the `Connection` object itself,
so writes from different threads can clobber each other's transaction state
even when each repository holds its own lock).

Background for engineers picking this up cold:

- The shared-connection guardrails today are a per-repo `threading.Lock` and
  a per-worker single-thread `ThreadPoolExecutor`. Both help in the common case
  but cannot close cross-repo or cross-call races on the same connection.
- `SQLiteJobRepository._commit()` ships a narrow workaround for the
  no-transaction race (logs a warning, persists data anyway). It exists so
  prod doesn't crash while this plan is being executed — it does **not**
  remove the underlying hazard.

## Decisions & tradeoffs

1. **Per-thread connection via factory, not per-request and not pooled.**
   - **Why:** matches SQLite's native model (each `sqlite3.Connection` is
     single-threaded; WAL coordinates cross-connection access at the file
     level). A pool implies handing connections back which is unneeded —
     there are O(threads) connections, not O(open work items).
   - **Rejected:** a pool with checkout/checkin semantics (overkill for a
     ~5-thread server) and per-request connections (would need to thread the
     connection through every repo call, painful refactor).

2. **`threading.local`-based factory, with a separate factory per process.**
   - **Why:** simplest correct primitive. The factory lazily opens a
     connection the first time the current thread asks for it, runs the
     standard PRAGMAs, and caches it on `threading.local`. Thread exit
     leaks one FD per thread, acceptable for the small fixed thread set
     this server uses.
   - **Rejected:** `contextvars` (request-scoped, not what we want),
     dependency injection per call (too invasive).

3. **Repositories take a `ConnectionFactory`, not a `Connection`.**
   - **Why:** the failure mode this plan eliminates is "code captures the
     connection at construction and reuses it across threads." Requiring
     every repo method to call `self._factory.get()` makes that impossible.
   - **Cost:** every repo method gains one line. ~15 repos, ~150 methods.
     Mechanical change; large diff but low conceptual cost.

4. **Per-repo locks go away.** With per-thread connections, no two threads
   share the same `Connection`, so the `threading.Lock` becomes redundant
   noise. Remove it in the same units that migrate each repo.

5. **`check_same_thread` stays True.** Once each thread has its own
   connection, we want Python's built-in cross-thread guard back on as a
   tripwire — if we ever pass a connection across threads again, it raises
   `ProgrammingError` immediately instead of silently corrupting state.

6. **WAL stays.** WAL is what makes per-connection model work: readers don't
   block the writer, the writer doesn't block readers. Already enabled by
   `connection.py`.

7. **The `_commit()` workaround in `SQLiteJobRepository` stays until W3
   ships.** Removing it earlier risks reintroducing the prod crash on any
   path not yet migrated.

## Non-goals

1. Not switching to Postgres. Discussed and rejected 2026-05-11 — see the
   conversation log; the bug is in Python's sqlite3 driver use pattern, not
   in SQLite the database. Postgres would solve it for the wrong reason and
   at much higher cost.
2. Not changing schema, migrations, or any SQL.
3. Not introducing an ORM or query builder. Raw SQL + `sqlite3.Row` stays.
4. Not changing the `JobRunner`'s single-worker `ThreadPoolExecutor` to
   multi-worker. That's a separate decision with its own risks.
5. Not moving ChromaDB or any vector logic. Out of scope.

## Kill criteria

1. If the W1 spike reveals that per-thread connections plus WAL still
   produce write-write contention beyond the 5s `busy_timeout` under
   realistic load, reconsider. (Expected outcome: this is fine — writes
   are rare and short.)
2. If a separate decision is made to move off SQLite (e.g. multi-machine
   deploy becomes a near-term requirement), abandon this plan in favour
   of the migration.
3. If FastAPI / ASGI starts using async DB access (we move to
   `aiosqlite` or similar), the threading model changes and this plan
   is superseded.

## Work units

Ordering: foundation-first (W1 builds the factory before anyone uses it),
then risk-first (W2 migrates the highest-trafficked repo and proves the
shape under real load), then mechanical fan-out, then cleanup.

### W1 — Build `ConnectionFactory` and parallel-test it (S, Low risk)

- **Changes:**
  - New `src/journal/db/factory.py` with `ConnectionFactory` class.
    Holds `db_path`. `.get()` returns the current thread's connection,
    opening it lazily on first use and applying the standard PRAGMAs.
    `.close_current()` for tests.
  - Keep `db/connection.py` `get_connection()` exactly as-is; new code
    sits alongside, no existing code changes.
  - Wire `check_same_thread=True` on connections handed out by the
    factory.
- **Test impact:** new `tests/test_db/test_factory.py`:
  threading.local semantics, distinct connections per thread, PRAGMA
  application, both-threads-can-write-simultaneously, busy_timeout
  honoured under contention. No existing test changes.
- **Reversibility:** pure additive — revert the commit.
- **Dependencies:** none.
- **Acceptance criteria:** new tests pass; existing 2263 tests
  unchanged; lint clean. `ConnectionFactory` exists and is unused by
  prod code.

### W2 — Migrate `SQLiteJobRepository` to the factory (M, Medium risk)

- **Changes:**
  - `SQLiteJobRepository.__init__` takes `factory: ConnectionFactory`
    instead of `conn: sqlite3.Connection`.
  - Every method opens with `conn = self._factory.get()` and uses that
    local variable for execute + commit + fetch.
  - Remove `self._lock` and `with self._lock:` wrappers (per-thread
    connections obviate them).
  - Remove the `_commit()` workaround helper — calls go back to plain
    `conn.commit()`.
  - Update `mcp_server/bootstrap.py:552` to construct
    `SQLiteJobRepository(factory)`.
  - Update the `connection` property to return `self._factory.get()`
    (still useful for tests; doc the caveat that the connection is
    thread-local).
- **Test impact:**
  - `tests/test_db/test_jobs_repository.py` — `jobs_repo` fixture
    changes from `(db_conn)` to `(factory)`. ~30 tests touched, all
    mechanical.
  - `TestSharedConnectionCommitRace` class is **deleted** —
    the race it reproduces is structurally impossible after this unit.
    Document in the commit message that this is intentional regression
    deletion, not test rot.
  - New stress test: 8 threads × 100 iterations writing + reading jobs
    on the same factory, no errors. Use the loop pattern from
    `sqlite-threading.md`.
- **Reversibility:** revert commit. The schema doesn't change so a
  rollback is trivial.
- **Dependencies:** W1.
- **Acceptance criteria:** all unit tests pass; stress test passes 50
  consecutive runs; the prod failure cannot be reproduced even with the
  proxy-based injection from the deleted race-class. Deploy to prod,
  run an editing workflow that triggered the 2026-05-11 incident, no
  warnings logged from anywhere.

### W3 — Migrate the remaining repositories (L, Medium risk)

Mechanical fan-out of the W2 pattern. Could be one large PR or split
per repo; recommendation is one PR per repo for review tractability,
shipped in a tight batch.

- **Changes:** per-repo migration to take `ConnectionFactory`.
  Inventory (from `src/journal/db/` and `src/journal/entitystore/`):
  - `SQLiteEntryRepository` (repository package — multiple files;
    they all share the same `_conn`, migrate together).
  - `FitnessRepository`.
  - `EntityStore`, `EntityMentions`, `EntityRelationships`,
    `EntityAliases` (entitystore package).
  - `UserRepository` (auth).
  - Any other consumer of `db_conn` — grep `Connection` in
    `src/journal/` to enumerate before starting.
- **Test impact:** corresponding test fixtures swap `db_conn` for
  `factory`. Largely mechanical; bulk of the test diff. Coverage
  threshold must not drop.
- **Reversibility:** revert per repo. Each repo's migration is
  independent.
- **Dependencies:** W1 + W2.
- **Acceptance criteria:** all repos take a factory; bootstrap
  constructs one factory and hands it to every repo; the only
  remaining `Connection` reference is inside the factory; full suite
  + integration tests green; deploy to prod under a small-load
  shadow window.

### W4 — Retire `check_same_thread=False`, deprecate `get_connection` (S, Low risk)

- **Changes:**
  - `db/connection.py:get_connection` keeps `check_same_thread=True`
    as the only mode; remove the `check_same_thread` parameter and
    the now-obsolete WARNING block.
  - Move the PRAGMA-application code into the factory; deprecate
    `get_connection` (keep one thin shim that delegates, marked
    `# kept for migrations.py and ad-hoc CLI tools`).
  - Update `sqlite-threading.md` with a **Status:** superseded
    header pointing to this plan's closed-state, and `git mv` it
    into `docs/archive/` along with this plan once everything ships.
    (Don't archive prematurely — it's the canonical "why" doc until
    W4 closes.)
- **Test impact:** none functional — but verify no test sets
  `check_same_thread=False`. New negative test: passing a connection
  across threads now raises `sqlite3.ProgrammingError`.
- **Reversibility:** revert commit.
- **Dependencies:** W3.
- **Acceptance criteria:** the `check_same_thread=False` flag does
  not appear anywhere in the codebase; ProgrammingError is raised
  if it ever happens by accident in tests.

### W5 — Close out documentation (S, Low risk)

- **Changes:**
  - This plan: `Status: closed YYYY-MM-DD`, `git mv` to
    `docs/archive/sqlite-per-thread-connections-plan.md`.
  - `sqlite-threading.md`: `Status: superseded by archive/sqlite-per-thread-connections-plan.md (YYYY-MM-DD)`,
    `git mv` to `docs/archive/`.
  - `docs/roadmap.md`: update the "Active planning docs" section to
    reflect the closed state and remove the now-archived links.
  - `connection.py` docstring: trim the WARNING block down to a
    historical note pointing at the archived plan.
- **Test impact:** none.
- **Reversibility:** revert.
- **Dependencies:** W4.
- **Acceptance criteria:** active `docs/` listing has no stale
  references to the shared-connection model; archive is complete.

## Open questions

1. **Migrations.** `migrations.py` opens its own connection and runs
   the SQL files at startup. Does it need the factory, or stays as a
   one-shot bootstrap helper? Lean: stays single-threaded by nature,
   factory not needed — but verify no migration path calls into a
   repo.
2. **CLI tools.** `cli/` opens its own short-lived connection per
   invocation, single-threaded by definition. Factory probably
   unnecessary there. Confirm during W3.
3. **Test fixtures.** Several integration tests use a shared `db_conn`
   fixture across threads (e.g. when exercising the job runner). They
   need a `factory` fixture instead. Cost is the bulk of the test
   diff in W2 + W3; flagged here so it's expected, not surprising.

## How to start (for the engineer picking this up)

1. Read `docs/sqlite-threading.md` end-to-end — it's the diagnosis;
   this doc is the prescription.
2. Read `src/journal/db/connection.py` — the WARNING block is the
   problem statement in code form.
3. Read `src/journal/db/jobs_repository.py` `_commit()` helper — it's
   the workaround you're replacing.
4. Start with W1. Don't try to land W1 + W2 in one PR; the W1 spike
   is cheap and lets you validate the threading.local + PRAGMA model
   in isolation.
