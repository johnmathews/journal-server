# Unit 1b — Remove `api/` reach-ins into private service state

Date: 2026-05-07

## What landed

Eliminated all 30 instances of `api/` modules reaching through
`_`-prefixed attributes on service objects. After this unit:

```bash
grep -rE 'query_svc\._|ingestion_svc\._|entity_store\._' src/journal/api/
```

returns nothing. The grep is the gate; record it on the next refactor's
checklist.

### Catalog (resolved)

- 17 reach-ins via `query_svc._repo.<method>` → public methods on
  `QueryService` (entry reads/writes, per-entry metadata, corpus stats,
  dashboard aggregations).
- 1 reach-in via `query_svc._repo._conn` → `QueryService.connection`
  property (sqlite3.Connection — only consumer is
  `journal.services.liveness.check_sqlite`).
- 1 reach-in via `query_svc._vector_store` → `QueryService.vector_store`
  property (only consumer is `liveness.check_chromadb`).
- 2 reach-ins via `ingestion_svc._repo.get_page_count` → new public
  method on `IngestionService` (same delegation shape as `QueryService`).
- 1 reach-in via `ingestion_svc._store_source_file` → renamed to
  `store_source_file` (was just an underscore wart — its in-service
  callers were already calling it across the public boundary).

### What got added on `QueryService`

19 additions, all thin pass-throughs documented under a "Public entry
reads / writes / metadata" header inside `query.py`. The header tells
future agents not to extend the section speculatively — only when there
is a concrete caller that would otherwise reach into `_repo`. The same
guidance covers the dashboard-aggregation section.

### Tests

Two new test files / classes:

- `tests/test_services/test_query_service_public_api.py` — 22 contract
  tests using `MagicMock`-backed repo + vector_store. Each new public
  method asserts the correct repo method is called with the right
  kwargs and the return value forwards. Pins names + signatures so a
  future repo refactor can't silently break the api/ layer.
- `tests/test_services/test_ingestion.py::TestIngestionPublicAPI` —
  3 behavioural tests against a real SQLite repo: page count for image
  ingestion (= 1), page count for text ingestion (= 0), and
  `store_source_file` actually inserts a row visible in `source_files`.

24 new tests; 1793 total passing (was 1769).

## Decisions worth remembering

1. **Pass-throughs over a `repo` property.** The plan explicitly favours
   "named methods that describe the operation; avoid exposing repos /
   clients wholesale via a `repo` property unless there's no cleaner
   option." That's the rule we followed for the 17 query methods. The
   exception: `connection` and `vector_store` exposed as properties
   because the only callers (liveness checks) genuinely need the raw
   handles, not a query.

2. **Conservative scope on write ownership.** `update_entry_date` and
   `verify_doubts` are writes that semantically belong on
   `IngestionService`, but the existing call sites use
   `query_svc._repo.<...>` — so the new public methods landed on
   `QueryService` matching the existing service handle. Reorganising
   write ownership would touch the call site's service variable too,
   which is a separate concern; recorded as a follow-up but out of
   Unit 1b's mechanical scope.

3. **`_store_source_file` rename, not a new method.** The function
   already existed and worked. Removing the leading underscore was
   enough; no behaviour change. The internal `self._repo._conn` reach-in
   *inside* the function body is a service-internal detail — not a
   cross-module reach-in — and stays. (Cleaning that up later would
   need either an `EntryRepository.execute_raw_sql` escape hatch or
   moving source-file persistence into the repo. Both are bigger than
   Unit 1b warrants.)

4. **`get_ingestion_stats` keyword-only, default-now.** The repo method
   takes `now: datetime` as a positional-required argument. The new
   `QueryService.get_ingestion_stats(*, now=None, user_id=None)` lets
   the health route call without arguments and defaults to
   `datetime.now(UTC)`, since that is what every caller would do anyway.
   Tested both shapes (default and explicit-now).

5. **Mock-based contract tests vs real-DB behavioural tests.**
   `test_query_service_public_api.py` mocks the repo because the value
   of those tests is the contract (call shape + delegation), not SQL
   behaviour — `test_query.py` already covers SQL with a real SQLite
   repo. The ingestion-side tests use the real repo because the new
   methods are short enough that a behavioural test costs no more than
   a mock test and the result is more trustworthy.

## Coverage

- Overall: 84% (unchanged; new code is thin enough that the ratio
  doesn't move).
- New `QueryService` methods: covered both by direct mock-based contract
  tests and transitively by `test_api.py` route tests that go through
  the new public API after the call-site updates.
- Ingestion new methods: covered behaviourally and by route tests.
