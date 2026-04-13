# Async Batch Jobs

The journal server runs two batch operations that can take a long time to
complete: entity extraction (LLM call per entry) and mood backfill (LLM call
per entry). Running these synchronously inside an HTTP request exposes the
caller to socket timeouts, indeterminate progress, and no way to monitor work.

This document describes the async job model that replaced the synchronous path.

## Data model

A single `jobs` table (migration `0006_jobs.sql`) holds one row per submitted
batch run:

| column             | type     | notes                                                                    |
| ------------------ | -------- | ------------------------------------------------------------------------ |
| `id`               | TEXT PK  | UUID v4 assigned at submission time                                      |
| `type`             | TEXT     | `entity_extraction` \| `mood_backfill`                                    |
| `status`           | TEXT     | `queued` → `running` → `succeeded` \| `failed`                            |
| `params_json`      | TEXT     | JSON-encoded submission params (e.g. `{"stale_only": true}`)             |
| `progress_current` | INTEGER  | Updated after each entry finishes                                        |
| `progress_total`   | INTEGER  | Fixed once the set of target entries is resolved                         |
| `result_json`      | TEXT     | Aggregated summary (null until terminal)                                 |
| `error_message`    | TEXT     | Populated when `status = failed`                                         |
| `created_at`       | TEXT     | ISO 8601, set at submission                                              |
| `started_at`       | TEXT     | ISO 8601, set when the worker picks up the row                           |
| `finished_at`      | TEXT     | ISO 8601, set at terminal transition                                     |

There are indexes on `status` and `created_at DESC` so the (small) dashboard
lookups stay cheap.

## JobRunner

`src/journal/services/jobs.py::JobRunner` owns the in-process execution of
jobs. Its contract:

1. **Single-worker `ThreadPoolExecutor`.** `max_workers=1`. Concurrent
   submissions queue behind the running one. This is deliberate and
   load-bearing — see [threading and SQLite](#threading-and-sqlite) below.
2. **Param validation before the row is created.** Unknown keys, wrong types,
   and invalid enum values (`mode`, `entry_id`) raise `ValueError` synchronously
   from `submit_*`. No job row is created for invalid input.
3. **Always reach a terminal state.** The worker body is wrapped in `try/except`
   so any exception — including unexpected ones from inside the service layer
   — is caught, logged, and recorded as `failed` with the exception message.
   A "stuck running" row is impossible under normal process lifetime.
4. **Progress via callback.** `extract_batch` and `backfill_mood_scores` both
   accept an optional `on_progress: Callable[[int, int], None]`. The runner
   passes a closure that writes to `jobs.progress_current / progress_total`.
   Callback exceptions in the service layer are swallowed and logged — a
   broken sink must never abort the batch.
5. **Shutdown.** `JobRunner.shutdown(wait=False)` cancels pending futures and
   stops accepting new submissions. Registered with `atexit` in
   `mcp_server.py`. Any job still running when the process dies will be left
   with `status=running` — see [restart recovery](#restart-recovery).

## Restart recovery

Because jobs run in-process, an unclean server restart can leave rows stuck in
`running` or (less likely) `queued`. On startup, `mcp_server.py` calls
`SQLiteJobRepository.reconcile_stuck_jobs()`, which rewrites any such row to
`failed` with `error_message = "server restarted before job completed"` and
sets `finished_at` to now. The reconciled count is logged at INFO.

This means the worst case after an unexpected crash is "the user sees the job
they submitted 10 minutes ago is marked `failed`, with a clear explanation,
and can resubmit." No data integrity issue, no zombie rows.

## Threading and SQLite

The main server connection is opened with `check_same_thread=False`
(`src/journal/db/connection.py::get_connection`) so that the JobRunner's
worker thread can write to it from outside the thread where it was created.

**This is only safe because the executor has `max_workers=1`.** WAL +
`synchronous=NORMAL` are safe for cross-thread access *as long as all writes
serialise* — and single-worker guarantees that. If the pool is ever bumped to
multiple workers, the SQLite threading model must be revisited. There is a
prominent comment in `services/jobs.py` next to the executor construction that
flags this invariant.

## REST surface

All three job endpoints are registered via `mcp.custom_route` in
`src/journal/api.py` alongside the other REST routes.

### `POST /api/entities/extract`

Submit an entity-extraction batch. Body:

```json
{
  "entry_id": 42,              // optional: single-entry mode
  "start_date": "2026-03-01",  // optional: ISO date filter
  "end_date": "2026-03-31",    // optional: ISO date filter
  "stale_only": true           // optional: only entries flagged stale
}
```

All four fields are optional. `entry_id` overrides the filter params (the
runner calls `extract_from_entry` and returns a one-result batch). Otherwise
the runner calls `extract_batch` with the filters.

Response on success (**202 Accepted**):

```json
{ "job_id": "a3f9...", "status": "queued" }
```

Unknown fields in the body or wrong types return **400** with
`{"error": "..."}`.

### `POST /api/mood/backfill`

Submit a mood-backfill batch. Body:

```json
{
  "mode": "stale-only",         // required: "stale-only" | "force"
  "start_date": "2026-03-01",   // optional
  "end_date": "2026-03-31"      // optional
}
```

`stale-only` rescores entries that are missing at least one currently-loaded
mood dimension; `force` rescores every entry in the selected date window.
`prune_retired` and `dry_run` (exposed by the CLI) are intentionally not
surfaced here — they are power-user flags and the UI stays minimal.

Response on success (**202 Accepted**):

```json
{ "job_id": "8e12...", "status": "queued" }
```

### `GET /api/jobs/{job_id}`

Poll a job's current state:

```json
{
  "id": "a3f9...",
  "type": "entity_extraction",
  "status": "running",
  "params": { "stale_only": true },
  "progress_current": 12,
  "progress_total": 48,
  "result": null,
  "error_message": null,
  "created_at": "2026-04-12T09:14:33+00:00",
  "started_at": "2026-04-12T09:14:33+00:00",
  "finished_at": null
}
```

Returns **404** for an unknown id. Callers are expected to poll once per
second until `status` is a terminal value (`succeeded` or `failed`).

## MCP tool surface

Three MCP tools expose the same functionality via the MCP protocol. Unlike
the REST endpoints, the `_batch` tools **block** until the job reaches a
terminal state — they poll `SQLiteJobRepository.get(job_id)` internally at
500 ms intervals, up to a 3600-second deadline. This matches how Claude
naturally consumes tool calls (synchronous in / out) while still using the
single-worker executor for serialisation.

- **`journal_extract_entities_batch`** — mirrors the REST body shape. Blocks
  until done and returns `{status, job_id, result, error_message}`.
- **`journal_backfill_mood_scores_batch`** — mirrors the REST body shape for
  mood backfill. Blocks until done.
- **`journal_get_job_status`** — takes a `job_id`, returns the serialised job
  dict without blocking. Useful for checking a job submitted by another
  client (e.g. the webapp) from within an MCP conversation.

### Wrapper sentinel values

The `_batch` tools return a small wrapper shape with a `status` field. In
addition to the real `JobStatus` values (`succeeded`, `failed`), the wrapper
may return **`"timeout"`** if the 3600-second polling deadline elapses
without the job reaching a terminal state. A timeout sentinel does NOT mean
the job has been cancelled — the row in the database remains in `running`,
and the worker keeps going. The caller can poll `journal_get_job_status` (or
`GET /api/jobs/{id}`) later to see the final outcome. Treat `"timeout"` as
"I gave up waiting, but the job may still be in flight."

Likewise, if submission itself fails validation, the wrapper returns
`status: "failed"` with `job_id: null` and the validation error message —
distinguishable from a post-submission failure because `job_id` is null.

The pre-existing synchronous `journal_extract_entities` tool is left in place
for backward compatibility with any existing MCP usage. New code should use
the `_batch` variants.

## Result payloads

Entity extraction jobs store this summary in `result_json`:

```json
{
  "processed": 42,
  "entities_created": 18,
  "entities_matched": 67,
  "mentions_created": 112,
  "relationships_created": 9,
  "warnings": ["..."]
}
```

Mood backfill jobs store:

```json
{
  "scored": 40,
  "skipped": 2,
  "errors": []
}
```

Consumers should not assume any field beyond these — if the server adds new
counters in a future version, clients must tolerate unknown keys.

## Automatic job triggering

Jobs are queued automatically at key lifecycle events so users don't need to
manually request entity extraction or mood scoring:

| Event                                | Entity extraction | Mood scoring | Reprocess embeddings |
| ------------------------------------ | :---------------: | :----------: | :------------------: |
| `POST /api/entries/ingest/text`      | yes (async job)   | yes (async)  | —                    |
| `POST /api/entries/ingest/file`      | yes (async job)   | yes (async)  | —                    |
| `POST /api/entries/ingest/images`    | yes (follow-up)   | inline       | —                    |
| `PATCH /api/entries/{id}` (text)     | yes (async job)   | yes (async)  | yes (async job)      |

For image ingestion, mood scoring runs inline inside the ingestion worker
(via `IngestionService._process_text`). Entity extraction is submitted as a
follow-up job after the image ingestion job succeeds — it runs on the same
single-worker executor, so it starts once the ingestion job marks itself
complete.

For text/file ingestion, both mood scoring and entity extraction are submitted
as separate background jobs immediately after the entry is created. Both are
best-effort — failures are logged but don't fail the ingest response.

For PATCH, all three background jobs (reprocess embeddings, entity extraction,
mood scoring) are submitted after the text save succeeds. `mood_job_id` is
included in the PATCH response alongside the existing `entity_extraction_job_id`
and `reprocess_job_id`.

## What's intentionally out of scope for v1

- **Cancellation.** There is no API to cancel a running job. Jobs are fast
  enough (tens of seconds for typical runs) that cancellation is not worth
  the complexity of interrupting the worker mid-entry.
- **Job history UI.** The webapp has a dedicated Job History page (`/jobs`)
  backed by `GET /api/jobs` that lists all historical jobs with filters
  and pagination. Old rows accumulate in the database; no pruning runs
  automatically.
- **Retry.** A failed job is not automatically retried. The user can submit
  a new job with the same parameters.
- **Multi-worker parallelism.** Would require rethinking the SQLite
  threading model and per-LLM rate limits.
