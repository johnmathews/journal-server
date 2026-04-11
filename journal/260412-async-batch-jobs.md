# 2026-04-12 — Async batch jobs (entity extraction + mood backfill)

The webapp's Entities page had a "No entities yet. Run the extraction batch
job to populate them." empty state but no way to actually run the job — you
had to drop into `uv run journal extract-entities` on the host. Same for
mood scoring (`uv run journal backfill-mood`). This session adds a UI button
for both, and along the way builds the first async-job infrastructure in the
server.

The webapp half is a sibling commit in `journal-webapp` (see
`journal-webapp/journal/260412-batch-job-ui.md`).

## Why not keep it synchronous?

The existing `POST /api/entities/extract` handler was already synchronous —
it ran `extract_batch` inside the request thread and returned the full
result list when done. The question was whether to keep that shape for the
new UI or flip it async.

Flipped async because:

1. Extraction and mood backfill each make one LLM call per entry. A full
   re-extraction on 200 entries is 15+ minutes of sequential API calls. That
   exceeds default fetch timeouts in every browser, every proxy, and every
   reverse proxy between the webapp and the server.
2. A progress bar is only possible with a separate progress channel — you
   can't stream status from inside a single HTTP response shape without
   special infrastructure (SSE, chunked JSON, websockets).
3. The user wanted to be able to "close the window" and come back — that
   requires state to live somewhere other than the HTTP request.

Async costs us more machinery (jobs table, runner, polling endpoint) but is
the only shape that actually solves the problem. See `docs/jobs.md` for the
full design.

## What shipped

### 1. Progress callbacks in the existing services

Both `extract_batch` (entity_extraction.py) and `backfill_mood_scores`
(backfill.py) now accept an optional
`on_progress: Callable[[int, int], None] | None = None` keyword argument.
Default None preserves every existing caller (CLI, pre-existing tests). The
callback fires once before the loop with `(0, total)` and once after each
entry with `(current, total)`, whether the entry succeeded or failed. A
raising callback is swallowed with a WARNING log — a broken progress sink
must never abort the batch.

This change is mechanical (both functions were already simple for-loops) but
it's the hinge the whole async system rotates on. Without per-entry progress
the UI would be a spinner with no information; with it we get a real
progress bar.

### 2. Jobs table

Migration `0006_jobs.sql` adds a single `jobs` table with columns for id,
type, status, params JSON, progress counters, result JSON, error message,
and three timestamps (`created_at` / `started_at` / `finished_at`). Indexes
on `status` and `created_at DESC`.

The numbering collision is worth a note: migration 0005 was already taken
by `0005_uncertain_spans.sql` — not visible in the plan's original
reconnaissance. The subagent doing U2 caught this and used 0006 instead.
No harm done; the migration runner discovers files by glob + version
prefix.

### 3. JobRepository

`src/journal/db/jobs_repository.py` — SQLite-backed repository with:
- `create(type, params) -> Job`
- `mark_running(id)`
- `update_progress(id, current, total)`
- `mark_succeeded(id, result)`
- `mark_failed(id, error_message)`
- `get(id) -> Job | None`
- `reconcile_stuck_jobs() -> int`

The last method is the restart-recovery hinge. On server startup, any row
with `status ∈ {queued, running}` is rewritten to `failed` with
`error_message = "server restarted before job completed"` — this is the
safety net for jobs that were in flight when the process died. The count is
logged at INFO.

`Job` is a dataclass in `models.py` with `JobStatus` / `JobType` Literal
aliases. `params` and `result` are `dict[str, Any]` in memory, transparently
(de)serialised from `params_json` / `result_json` by the repository.

### 4. JobRunner

`src/journal/services/jobs.py::JobRunner` is the orchestration layer.

```python
self._executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="journal-jobs",
)
```

The `max_workers=1` is load-bearing — see [threading](#threading-and-sqlite)
below. A prominent comment next to the executor construction flags the
invariant for anyone tempted to bump it.

Public methods:
- `submit_entity_extraction(params) -> Job` — validates params shape
  (unknown keys raise, wrong types raise, `bool` masquerading as `int`
  raises), creates the row, submits to executor, returns the Job.
- `submit_mood_backfill(params) -> Job` — same, with `mode` required and
  constrained to `{"stale-only", "force"}`.
- `shutdown(wait)` — `executor.shutdown(wait=wait, cancel_futures=True)`.

Internal `_run_*` methods:
- Wrap the entire body in try/except so a terminal row is *always* written,
  even when the underlying service raises something unexpected.
- Call `mark_running`, build a progress callback closure, dispatch to the
  service function with `on_progress=...`, aggregate results into a summary
  dict, call `mark_succeeded` or `mark_failed`.
- A secondary try/except around `mark_failed` itself guards against
  bookkeeping failures — so even a DB error during error recording won't
  crash the worker thread.

The single-entry extraction path (`entry_id` in params) doesn't have an
`on_progress` hook in `extract_from_entry`, so the runner manually brackets
the call with `progress_callback(0, 1)` / `(1, 1)`.

### 5. Wiring into startup

`mcp_server.py` opens the main SQLite connection with
`check_same_thread=False`, instantiates `SQLiteJobRepository`, calls
`reconcile_stuck_jobs()` (logging the count), instantiates `JobRunner`, and
adds both to the services dict. On shutdown: `atexit.register(...)` —
FastMCP's `lifespan` is per-session, not per-process, and is the wrong
hook for an executor that should outlive the lifespan scope.

There's one quirk: `atexit` runs arbitrarily late — reliably after
pytest/uvicorn has torn down stdout/stderr — so calling `log.info` from the
atexit hook produces "I/O operation on closed file" diagnostics from the
stdlib logging handler. The shutdown hook is silent by design for that
reason; lifecycle observability is preserved because `JobRunner` itself
logs submit / mark_running / terminal transitions.

### 6. REST endpoints

`src/journal/api.py` gains:

- **`POST /api/entities/extract`** — repurposed. Same path, same body shape,
  new response: **202 Accepted** with `{job_id, status}`. The old
  synchronous `{results: [...]}` shape is gone.
- **`POST /api/mood/backfill`** — new. Body: `{mode, start_date, end_date}`.
  Same 202 shape.
- **`GET /api/jobs/{job_id}`** — new. Returns the serialised Job dict or
  404. Uses a Starlette `{job_id:str}` path parameter (the default `path`
  converter would eat slashes, which is wrong for UUIDs).

Bad request bodies (non-dict JSON, unknown keys, invalid mode) are rejected
with 400 before the JobRunner is touched.

The breaking change to `/api/entities/extract` is acceptable because:
- The webapp's `triggerEntityExtraction` client function existed but was
  never called from any UI code.
- The CLI bypasses the REST layer entirely — it calls
  `EntityExtractionService` directly.
- No external consumer has been documented.

### 7. MCP tools

Three new MCP tools under the existing `journal_` naming convention:

- **`journal_extract_entities_batch`** — submits an entity extraction job
  and polls `SQLiteJobRepository.get(job_id)` at 500 ms intervals until
  terminal. Returns a structured dict with status, job_id, result, and
  error_message.
- **`journal_backfill_mood_scores_batch`** — same shape for mood backfill.
- **`journal_get_job_status`** — non-blocking job lookup.

These are **added alongside** the pre-existing `journal_extract_entities`
synchronous tool. The old tool is left in place so any existing MCP flows
keep working — the naming with the `_batch` suffix marks the new async
path as the intended one for new code.

Blocking-until-done in the MCP layer is a deliberate choice: MCP tool calls
are synchronous from Claude's perspective, and Claude benefits most from
tools that return a final result it can reason about. The REST side stays
non-blocking because the webapp needs progress updates and the ability to
close the browser tab.

### 8. Tests

Full test breakdown:

- **U1 (progress callbacks):** 6 new tests in `test_entity_extraction.py`
  and `test_backfill.py` covering the callback sequence, continued progress
  on per-entry failure, dry-run progress advancement, and callbacks that
  raise.
- **U2 (migration + repository + models):** 12 new tests in
  `test_jobs_repository.py` covering create initial state, running/progress/
  succeeded/failed transitions, 404 on missing id, reconcile (including the
  "don't touch succeeded rows" guard), and nested-dict JSON round-trip.
- **U3 (JobRunner):** 13 new tests in `test_jobs_runner.py` covering happy
  paths for both job types, error paths (runner recovers and accepts new
  work after a failed job), param validation (including the `bool is int`
  trap), single-worker serialisation (uses a `threading.Event` to prove the
  executor blocks the second job until the first finishes), and shutdown.
- **U4 (wiring):** 2 new tests in `test_lifespan.py` covering services-dict
  contents and reconcile-on-startup behaviour.
- **U5 (REST):** 10 new tests in `test_api_jobs.py` covering both submit
  flows end-to-end through a Starlette TestClient backed by a real
  JobRunner + SQLiteJobRepository + fake extraction/backfill services.
  Covers 400s on bad bodies, 404 on unknown id, and the full serialised
  shape.
- **U5b (MCP tools):** 6 new tests in `test_mcp_server.py::TestBatchJobTools`
  covering the three new tools (happy + error paths). Reuses the
  FakeEntityExtractionService / FakeMoodBackfill fixtures from U3.

**Total: 611 → 630 passing.** `ruff check` clean.

## Threading and SQLite

The main server connection opens with `check_same_thread=False` so the
JobRunner's worker thread can write through it. This is **only safe because
the executor has `max_workers=1`**. WAL + `synchronous=NORMAL` allows
concurrent reads alongside a single writer across threads — multiple writers
would race on WAL frames and can corrupt the database.

If a future change bumps the pool to multiple workers, this whole assumption
needs revisiting — either by opening per-job connections (requires
refactoring the repository layer to take connection per-call) or by
introducing an explicit write lock. There is a prominent comment at the
executor construction site in `services/jobs.py` flagging this; `docs/jobs.md`
has a longer explanation.

## Post-review fix

Code review caught a subtle test bug in `test_jobs_runner.py`: five tests
in `TestEntityExtractionParamValidation` / `TestMoodBackfillParamValidation`
were querying the wrong SQLite database when asserting "no row created on
invalid params". They used the `db_conn` fixture from `conftest.py` (which
points at `tmp_path/test.db`) instead of the JobRunner's own connection
(which points at `tmp_path/jobs-runner.db`). The `count == 0` check passed
vacuously because it was reading an unrelated empty DB. Fixed by replacing
the `db_conn` query with `jobs_repo._conn` — the same connection the
runner writes through. The tests now actually exercise the invariant.

No other issues found in the diff. 630 tests still passing, ruff still
clean.

## Out of scope (intentional)

1. Cancellation. Jobs are fast enough (tens of seconds typically) that
   interrupting mid-entry isn't worth the complexity.
2. Job history / audit log view in the UI.
3. Automatic pruning of completed job rows.
4. `prune_retired` / `dry_run` flags in the UI (backend still supports them;
   CLI is the interface for those power-user modes).
5. Retry on failure — user resubmits manually.
