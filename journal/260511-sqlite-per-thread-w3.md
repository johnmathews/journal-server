# SQLite per-thread connections — W3

Plan: [`docs/sqlite-per-thread-connections-plan.md`](../docs/sqlite-per-thread-connections-plan.md).
Builds on [W1 + W2](./260511-sqlite-per-thread-w1-w2.md).

W2 shipped the hybrid-constructor pattern on `SQLiteJobRepository`
(production path uses `ConnectionFactory`, legacy path keeps a bare
`sqlite3.Connection` so the ~14 test files that haven't migrated
yet keep working unchanged). W3 fans that pattern out to every
remaining repo so the production-path SQLite surface is now
entirely thread-isolated.

Shipped as five separate commits, one per repo cluster — same
review-tractability rationale the plan called for, just batched on
one branch.

## Repos migrated

| Repo cluster | Files | Commit | New tests |
|---|---|---|---|
| `SQLiteEntryRepository` | `db/repository/{store,core,pages,chunks,search,mood,stats,analytics}.py` | PR 1 | 4 |
| `FitnessRepository` | `db/fitness_repository.py` | PR 2 | 4 |
| `SQLiteEntityStore` + mixins | `entitystore/{store,mentions,merge}.py` | PR 3 | 4 |
| `SQLiteUserRepository` | `db/user_repository.py` | PR 4 | 4 |
| `RuntimeSettings` | `services/runtime_settings.py` | PR 5 | 3 |

Net: 19 new factory-path tests across the suite. Full suite went
from 2280 to 2299 passing. Lint clean throughout.

`mcp_server/bootstrap.py` now constructs one process-wide
`ConnectionFactory` (`db_factory`) immediately after migrations
finish and hands the same factory to every migrated repo — jobs,
entries, fitness, entity store, user repo, runtime settings.

## Pattern, verbatim from W2

Every migrated class follows the same shape:

1. Constructor: `factory_or_conn: ConnectionFactory | sqlite3.Connection`
   — `isinstance` switches between two stored attributes (`_factory`
   on the factory path, `_direct_conn` on the legacy path).
2. `_conn()` method dispatches to `factory.get()` or `self._direct_conn`.
3. Every public method opens with `conn = self._conn()` and operates
   on that local — no class attribute access in execute/commit calls.
4. `connection` property (where one existed) now returns
   `self._conn()` so callers that read it cross-thread get *their*
   thread's connection, not a captured reference.
5. Existing `_lock` retained where it already lived (fitness, jobs,
   users). On the factory path it's a no-op (one thread, one
   connection, no contention); on the legacy path it stays
   protective. W4 removes it along with the legacy branch.

## Per-repo notes (the things that weren't mechanical)

### SQLiteEntryRepository — the package case

The repo is a package of 7 mixins composed into `SQLiteEntryRepository`
in `store.py`. All mixins reference `self._conn` directly (e.g.
`with self._conn:` for transaction contexts). The W2 pattern needs
the attribute name not to clash with the method name, so the file-
level transform was: rename the stored attribute to `_direct_conn`
(legacy) or `_factory` (factory), expose `_conn()` as a method,
and in every mixin method rewrite `with self._conn: ...
self._conn.execute(...)` into `conn = self._conn(); with conn: ...
conn.execute(...)`.

Three external sites in `services/{backfill,ingestion/service,
entity_extraction/service}.py` reached into `repo._conn` directly
(behind `# type: ignore[attr-defined]`). Those switched to the
public `repo.connection` property, which routes through `_conn()`
and returns the calling thread's connection.

### FitnessRepository — bulk transform

35 sites of `self._conn.{execute,commit}`. The migration was three
`replace_all`s plus a fourth one to insert `conn = self._conn()`
before every `with self._lock:` block. No external `_conn`
consumers; no multi-statement transactions to rethink (every write
is a single statement + commit pair).

### SQLiteEntityStore — multi-mixin, with `merge_entities`

Same package shape as the entry repo: base store + two mixins.
Each mixin keeps the existing `# type: ignore[attr-defined]`
convention for its cross-class references (`self._hydrate`,
`self.get_entity`, and now `self._conn()`).

`merge_entities` is the only multi-statement implicit-transaction
method in this cluster — multiple `conn.execute(...)` calls followed
by a single `conn.commit()` at the end. Under per-thread connections
that whole transaction runs on the calling thread's connection,
which is exactly what `BEGIN`-`COMMIT` already required: the
factory path is *more* correct here, not just safer, because
concurrent writes from another repo can no longer end this
transaction mid-merge. Added a dedicated `test_merge_runs_under_factory`
test to lock that in.

### SQLiteUserRepository — vanilla

Same shape as fitness: lock-per-method, single-statement writes.
Mechanical migration, no surprises.

### RuntimeSettings — the carve-out that wasn't

The W3 brief flagged this one as a possible read-only carve-out:
"if it doesn't write at runtime, leave it on `conn`." It does
write. `set()` is called from API request threads when the admin
toggles a setting, so it has exactly the same multi-thread write
profile that motivated the whole refactor. Migrated.

This class has no `_lock` (never had one — settings writes are
rare and idempotent, so the original design accepted the small race
window). On the factory path the absence of a lock is fine because
each request thread owns its own connection; on the legacy path
it inherits the same "good enough" semantics it had before.

## Things deferred to W4

1. The bare `conn` opened with `check_same_thread=False` still
   exists in `bootstrap.py`. `run_migrations` uses it (single-
   threaded by nature), `HealthPoller` reads through it (read-only
   liveness check), and three API helpers (`api/settings.py`,
   `api/fitness.py`, `mcp_server/tools/_ctx.py`) plus one MCP tool
   read it from the `db_conn` slot in `_services`. W4 retires the
   shared connection along with the legacy constructor branch.
2. The legacy bare-`Connection` branch on every migrated repo
   stays live until W4 — ~30 test files still construct repos
   with `db_conn`, and migrating them en masse was specifically
   what the hybrid pattern was designed to defer.
3. `TestSharedConnectionCommitRace` in the jobs-repo test file
   still exercises the legacy path; it goes when the legacy
   branch goes.
4. The per-method `_lock` on fitness, jobs, and users repos stays
   until W4 — it's a no-op on the factory path but protective on
   the legacy one.

## Files touched

Production:
- `src/journal/db/repository/{store,core,pages,chunks,search,mood,stats,analytics}.py`
- `src/journal/db/fitness_repository.py`
- `src/journal/db/user_repository.py`
- `src/journal/entitystore/{store,mentions,merge}.py`
- `src/journal/services/runtime_settings.py`
- `src/journal/services/{backfill,entity_extraction/service,ingestion/service}.py`
  (external `_conn` consumers → `connection` property)
- `src/journal/mcp_server/bootstrap.py` (single `db_factory`, all migrated repos)

Tests:
- `tests/test_db/test_repository.py` (+4 factory-path tests)
- `tests/test_db/test_fitness_repository.py` (+4)
- `tests/test_db/test_user_repository.py` (+4)
- `tests/test_services/test_entity_store.py` (+4)
- `tests/test_services/test_runtime_settings.py` (+3)
