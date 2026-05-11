# Async Batch Jobs

The journal server runs two batch operations that can take a long time to complete: entity extraction (LLM call per
entry) and mood backfill (LLM call per entry). Running these synchronously inside an HTTP request exposes the caller to
socket timeouts, indeterminate progress, and no way to monitor work.

This document describes the async job model that replaced the synchronous path.

## Data model

A single `jobs` table (migration `0006_jobs.sql`) holds one row per submitted batch run:

| column             | type    | notes                                                        |
| ------------------ | ------- | ------------------------------------------------------------ |
| `id`               | TEXT PK | UUID v4 assigned at submission time                          |
| `type`             | TEXT    | One of: `entity_extraction`, `mood_backfill`, `mood_score_entry`, `entity_reembed`, `reprocess_embeddings`, `ingest_images`, `ingest_audio`, `save_entry_pipeline`, `fitness_sync_strava`, `fitness_sync_garmin` (10 types as of 2026-05-10). |
| `status`           | TEXT    | `queued` → `running` → `succeeded` \| `failed`               |
| `params_json`      | TEXT    | JSON-encoded submission params (e.g. `{"stale_only": true}`) |
| `progress_current` | INTEGER | Updated after each entry finishes                            |
| `progress_total`   | INTEGER | Fixed once the set of target entries is resolved             |
| `result_json`      | TEXT    | Aggregated summary (null until terminal)                     |
| `error_message`    | TEXT    | Populated when `status = failed`                             |
| `created_at`       | TEXT    | ISO 8601, set at submission                                  |
| `started_at`       | TEXT    | ISO 8601, set when the worker picks up the row               |
| `finished_at`      | TEXT    | ISO 8601, set at terminal transition                         |

There are indexes on `status` and `created_at DESC` so the (small) dashboard lookups stay cheap.

## JobRunner

`src/journal/services/jobs/runner.py::JobRunner` owns the in-process execution of jobs. (The single-file
`services/jobs.py` was split into the `services/jobs/` package on 2026-05-07. The package now contains
`runner.py`, `validation.py`, `notifier.py`, `save_pipeline.py`, `retry.py`, `errors.py`, and a `workers/`
subdirectory with one file per job type.) Its contract:

1. **Single-worker `ThreadPoolExecutor`.** `max_workers=1`. Concurrent submissions queue behind the running one. This is
   deliberate and load-bearing — see [threading and SQLite](#threading-and-sqlite) below.
2. **Param validation before the row is created.** Unknown keys, wrong types, and invalid enum values (`mode`,
   `entry_id`) raise `ValueError` synchronously from `submit_*`. No job row is created for invalid input.
3. **Always reach a terminal state.** The worker body is wrapped in `try/except` so any exception — including unexpected
   ones from inside the service layer — is caught, logged, and recorded as `failed` with the exception message. A "stuck
   running" row is impossible under normal process lifetime.
4. **Progress via callback.** `extract_batch` and `backfill_mood_scores` both accept an optional
   `on_progress: Callable[[int, int], None]`. The runner passes a closure that writes to
   `jobs.progress_current / progress_total`. Callback exceptions in the service layer are swallowed and logged — a broken
   sink must never abort the batch.
5. **Shutdown.** `JobRunner.shutdown(wait=False)` cancels pending futures and stops accepting new submissions. Registered
   with `atexit` in `mcp_server/bootstrap.py`. Any job still running when the process dies will be left with `status=running` — see
   [restart recovery](#restart-recovery).

## Restart recovery

Because jobs run in-process, an unclean server restart can leave rows stuck in `running` or (less likely) `queued`. On
startup, `mcp_server/bootstrap.py` calls `SQLiteJobRepository.reconcile_stuck_jobs()`, which rewrites any such row to `failed` with
`error_message = "server restarted before job completed"` and sets `finished_at` to now. The reconciled count is logged
at INFO.

This means the worst case after an unexpected crash is "the user sees the job they submitted 10 minutes ago is marked
`failed`, with a clear explanation, and can resubmit." No data integrity issue, no zombie rows.

## Threading and SQLite

The main server connection is opened with `check_same_thread=False` (`src/journal/db/connection.py::get_connection`) so
that the JobRunner's worker thread can write to it from outside the thread where it was created.

**This is only safe because the executor has `max_workers=1`.** WAL + `synchronous=NORMAL` are safe for cross-thread
access _as long as all writes serialise_ — and single-worker guarantees that. If the pool is ever bumped to multiple
workers, the SQLite threading model must be revisited. There is a prominent comment in `services/jobs.py` next to the
executor construction that flags this invariant.

## REST surface

Job-related endpoints are registered via `mcp.custom_route` in `src/journal/api/jobs.py` (and a few in
`api/ingestion.py` for the `submit_*` entry points). The single-file `api.py` was split into the `src/journal/api/`
package on 2026-05-07.

### `POST /api/entities/extract`

Submit an entity-extraction batch. Body:

```json
{
 "entry_id": 42, // optional: single-entry mode
 "start_date": "2026-03-01", // optional: ISO date filter
 "end_date": "2026-03-31", // optional: ISO date filter
 "stale_only": true // optional: only entries flagged stale
}
```

All four fields are optional. `entry_id` overrides the filter params (the runner calls `extract_from_entry` and returns a
one-result batch). Otherwise the runner calls `extract_batch` with the filters.

Response on success (**202 Accepted**):

```json
{ "job_id": "a3f9...", "status": "queued" }
```

Unknown fields in the body or wrong types return **400** with `{"error": "..."}`.

### `POST /api/mood/backfill`

Submit a mood-backfill batch. Body:

```json
{
 "mode": "stale-only", // required: "stale-only" | "force"
 "start_date": "2026-03-01", // optional
 "end_date": "2026-03-31" // optional
}
```

`stale-only` rescores entries that are missing at least one currently-loaded mood dimension; `force` rescores every entry
in the selected date window. `prune_retired` and `dry_run` (exposed by the CLI) are intentionally not surfaced here —
they are power-user flags and the UI stays minimal.

Response on success (**202 Accepted**):

```json
{ "job_id": "8e12...", "status": "queued" }
```

### `POST /api/fitness/sync/{source}`

Submit a fitness fetch + normalize job. `source` is `"strava"` or `"garmin"`.
No request body. The job carries only `{"user_id": int}` (validated against
`FITNESS_SYNC_KEYS`) — the source identity rides in the `type` column
(`fitness_sync_strava` or `fitness_sync_garmin`), mirroring how
`mood_score_entry` doesn't carry a `dimension` parameter.

Response on success (**202 Accepted**):

```json
{ "job_id": "8e12...", "status": "queued" }
```

A single in-flight (`queued` or `running`) job per `(user_id, source)` is
allowed; subsequent submits return the existing job id with `"already_running":
true` instead of queueing a duplicate. **503** is returned for Strava when
`STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET` are unset — fail-loud at submit
time rather than queueing a row that's guaranteed to fail. Garmin never 503s
here post-multi-user-plan W6 (Garmin is always wired, with per-user
credentials and no global env vars); a user without a `fitness_auth_state`
row just produces a clean `auth_broken` sync run instead. The full route
shape is documented in
[`api.md` § Fitness endpoints](api.md#post-apifitnesssyncsource).

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

Returns **404** for an unknown id. Callers are expected to poll once per second until `status` is a terminal value
(`succeeded` or `failed`).

## MCP tool surface

Three MCP tools expose the same functionality via the MCP protocol. Unlike the REST endpoints, the `_batch` tools
**block** until the job reaches a terminal state — they poll `SQLiteJobRepository.get(job_id)` internally at 500 ms
intervals, up to a 3600-second deadline. This matches how Claude naturally consumes tool calls (synchronous in / out)
while still using the single-worker executor for serialisation.

- **`journal_extract_entities_batch`** — mirrors the REST body shape. Blocks until done and returns
  `{status, job_id, result, error_message}`.
- **`journal_backfill_mood_scores_batch`** — mirrors the REST body shape for mood backfill. Blocks until done.
- **`journal_get_job_status`** — takes a `job_id`, returns the serialised job dict without blocking. Useful for checking
  a job submitted by another client (e.g. the webapp) from within an MCP conversation.

### Wrapper sentinel values

The `_batch` tools return a small wrapper shape with a `status` field. In addition to the real `JobStatus` values
(`succeeded`, `failed`), the wrapper may return **`"timeout"`** if the 3600-second polling deadline elapses without the
job reaching a terminal state. A timeout sentinel does NOT mean the job has been cancelled — the row in the database
remains in `running`, and the worker keeps going. The caller can poll `journal_get_job_status` (or `GET /api/jobs/{id}`)
later to see the final outcome. Treat `"timeout"` as "I gave up waiting, but the job may still be in flight."

Likewise, if submission itself fails validation, the wrapper returns `status: "failed"` with `job_id: null` and the
validation error message — distinguishable from a post-submission failure because `job_id` is null.

The pre-existing synchronous `journal_extract_entities` tool is left in place for backward compatibility with any
existing MCP usage. New code should use the `_batch` variants.

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

Image ingestion jobs store:

```json
{
 "entry_id": 75,
 "entry_date": "2026-04-13",
 "source_type": "photo",
 "word_count": 250,
 "chunk_count": 5,
 "page_count": 3,
 "follow_up_jobs": {
   "mood_scoring": "job-uuid-1",
   "entity_extraction": "job-uuid-2"
 }
}
```

Audio ingestion jobs store:

```json
{
 "entry_id": 76,
 "entry_date": "2026-04-14",
 "source_type": "voice",
 "word_count": 120,
 "chunk_count": 2,
 "recording_count": 1,
 "follow_up_jobs": {
   "mood_scoring": "job-uuid-3",
   "entity_extraction": "job-uuid-4"
 }
}
```

Mood scoring (single entry) jobs store:

```json
{
 "entry_id": 75,
 "scores_written": 7
}
```

Reprocess embeddings jobs store:

```json
{
 "entry_id": 75,
 "chunk_count": 5
}
```

Entity-reembed jobs (triggered by `PATCH /api/entities/{id}` when the description changes) store one of:

```json
{ "entity_id": 7, "embedded": true, "dimensions": 1536 }
```

```json
{ "entity_id": 7, "embedded": false, "reason": "empty description" }
```

The job recomputes the entity's stored embedding from `f"{canonical_name} {description}"` so the stage-c similarity
match in entity extraction reflects later edits. Notification topic `notif_job_success_entity_reembed` defaults off
(these fire on every description edit and are routine); failures still go through the global `notif_job_failed`.

`fitness_sync_strava` and `fitness_sync_garmin` jobs store the fetch and normalize summaries:

```json
{
 "fetch": {
  "run_id": 233,
  "status": "success",
  "rows_fetched": 12,
  "started_at": "2026-05-09T18:42:08Z",
  "finished_at": "2026-05-09T18:42:11Z",
  "...": "..."
 },
 "normalize": {
  "rows_normalized": 12,
  "drift_events": []
 }
}
```

When the fetch service short-circuits with `status="running"` (a routine sync
is already in flight under W6's single-run guard), the job marks succeeded
with `{"skipped": true, "reason": "already_running", "fetch": {...}}`
instead of re-running. Auth-broken and transient-failure paths mark the job
**failed** with operator-facing messages (`"Strava authorization is broken
— please re-authorize"`, `"Strava sync failed transiently — will retry on
next run"`); the W6 fetch service has already recorded the run row and
fired the once-per-transition Pushover before the worker sees the result,
so the failed jobs row only needs to surface the outcome to the webapp.
See [`fitness-pipeline.md`](fitness-pipeline.md) for the layer-by-layer
view.

Consumers should not assume any field beyond these — if the server adds new counters in a future version, clients must
tolerate unknown keys.

## Automatic job triggering

Jobs are queued automatically at key lifecycle events so users don't need to manually request entity extraction or mood
scoring:

| Event                             | Entity extraction | Mood scoring | Reprocess embeddings |
| --------------------------------- | :---------------: | :----------: | :------------------: |
| `POST /api/entries/ingest/text`   |  yes (async job)  | yes (async)  |          —           |
| `POST /api/entries/ingest/file`   |  yes (async job)  | yes (async)  |          —           |
| `POST /api/entries/ingest/images` |  yes (follow-up)  |    inline    |          —           |
| `POST /api/entries/ingest/audio`  |  yes (follow-up)  |    inline    |          —           |
| `PATCH /api/entries/{id}` (text)  |  yes (async job)  | yes (async)  |   yes (async job)    |

For image and audio ingestion, mood scoring runs inline inside the ingestion worker (via
`IngestionService._process_text`). Entity extraction is submitted as a follow-up job after the ingestion job succeeds —
it runs on the same single-worker executor, so it starts once the ingestion job marks itself complete. The webapp's
notification bell automatically re-hydrates its active jobs list when any tracked job reaches terminal state, so
server-spawned follow-up jobs (like entity extraction after ingestion) appear in the bell without a page refresh.

For text/file ingestion, both mood scoring and entity extraction are submitted as separate background jobs immediately
after the entry is created. Both are best-effort — failures are logged but don't fail the ingest response.

For PATCH, all three background jobs (reprocess embeddings, entity extraction, mood scoring) are submitted via
`JobRunner.submit_save_entry_pipeline()`, which wraps them in a synthetic `save_entry_pipeline` parent job (see
"Save-entry pipeline" below). The PATCH response includes `pipeline_job_id` (the parent), plus `reprocess_job_id`,
`entity_extraction_job_id`, and `mood_job_id` for the three children.

## Pipeline notifications (Pushover)

When an ingestion job (image or audio) triggers follow-up jobs (mood scoring and entity extraction), the server sends
**one combined Pushover notification** after all follow-ups finish, instead of one per job. This mirrors the webapp's
in-browser toast compression (which groups related toasts via job grouping in the Pinia store).

### How it works

1. Follow-up jobs carry a `parent_job_id` in their params linking them to the parent ingestion job.
2. The parent ingestion job suppresses its own notification — it does not call `_notify_success`.
3. When each follow-up completes (success or failure), it calls `_try_pipeline_notification(parent_job_id)`.
4. `_try_pipeline_notification` checks whether **all** siblings have reached a terminal state (succeeded or failed).
   If any sibling is still running, it returns early — the last one to finish sends the notification.
5. The combined result includes results from succeeded follow-ups only. Failed follow-ups are excluded from the
   combined dict (their individual failure notifications were already sent).

### Notification matrix

| Parent   | Mood scoring | Entity extraction | Notifications the user receives                                               |
| -------- | ------------ | ----------------- | ----------------------------------------------------------------------------- |
| succeeds | succeeds     | succeeds          | 1 combined success: entry created + mood scores + entities                     |
| succeeds | fails        | succeeds          | 1 failure (mood) + 1 combined success: entry created + entities               |
| succeeds | succeeds     | fails             | 1 failure (entity) + 1 combined success: entry created + mood scores          |
| succeeds | fails        | fails             | 2 failures (mood + entity) + 1 combined: entry created (no enrichment data)   |
| fails    | —            | —                 | 1 failure (ingestion) — follow-ups never queued                               |

### Standalone batch jobs

Jobs triggered manually (entity extraction batch from the settings page, mood backfill) have no `parent_job_id` and
notify individually as before. The pipeline grouping only applies to follow-up jobs auto-triggered by ingestion.

### Edge case: follow-ups fail to queue

If the executor rejects follow-up submissions (e.g. during server shutdown), `_queue_post_ingestion_jobs` catches the
error and returns an empty `follow_up_jobs` dict. In this case the parent sends its own notification directly, so the
user still learns the entry was created.

## Save-entry pipeline (PATCH /entries/:id)

Edits to existing entries fan out into three background jobs (`reprocess_embeddings`, `entity_extraction`, and
`mood_score_entry`). To match the new-entry flow's "one push per pipeline" UX — and to consolidate failures rather
than fire one Pushover per stage — these are wrapped in a **synthetic parent job** of type `save_entry_pipeline`.

### Synthetic parent

`JobRunner.submit_save_entry_pipeline()`:

1. Creates a parent job of type `save_entry_pipeline` (status `queued`) with `notify_strategy` stored in **params**.
   Storing the strategy in params (fixed at creation) — rather than result (which would require an extra
   `mark_succeeded` UPDATE) — makes it visible to fast-failing children the moment the parent row exists, with no
   additional SQLite write that would contend with worker-thread writes on the shared connection.
2. Submits the three children with `parent_job_id` set in their params.
3. Marks the parent succeeded **once** with the populated `follow_up_jobs` map in `result`.
4. Triggers a defensive `_try_pipeline_notification` call from the API thread to handle the rare case where every
   child completed before mark_succeeded landed (workers' calls would all have returned early seeing
   `parent.status != "succeeded"`). The atomic `try_acquire_notification_lock` on the repository guards against
   double-firing if a worker call races with this defensive sweep.

The parent does no actual work — it exists only to carry `notify_strategy` (in params) and `follow_up_jobs` (in
result), the two fields `_try_pipeline_notification` reads to dispatch correctly.

### `notify_strategy`

A field on the parent's **`params_json`** that controls how children handle their per-job notifications:

| Value                       | Used by             | Per-child success push | Per-child failure push | Pipeline summary                              |
| --------------------------- | ------------------- | ---------------------- | ---------------------- | --------------------------------------------- |
| `compressed_success_only`   | `ingest_images` /   | suppressed             | fired immediately      | success summary (lists what worked)           |
| (default)                   | `ingest_audio`      |                        |                        |                                               |
| `compressed_all`            | `save_entry_pipeline`| suppressed             | suppressed             | success summary if all OK; otherwise per-stage|
|                             |                     |                        |                        | failure breakdown (`+`/`-` markers)           |

The default of `compressed_success_only` keeps the existing new-entry behavior unchanged. The edit pipeline opts
into `compressed_all` so the user gets exactly one Pushover regardless of outcome.

### Notification matrix

| Reprocess | Entity extraction | Mood scoring | Notifications the user receives           |
| --------- | ----------------- | ------------ | ----------------------------------------- |
| succeeds  | succeeds          | succeeds     | 1 success summary: entry updated + 3 stage counts |
| succeeds  | succeeds          | fails        | 1 partial-failure summary listing all 3 stages    |
| any combination with ≥1 fail | …      | …            | 1 partial-failure summary (or "update failed" if all 3 failed) |

Per-stage in-app toasts still fire (one per terminal job) — the consolidation is only about Pushover.

### Race conditions and dedup

The pipeline orchestration must handle two race windows:

1. **Strategy visible before map populated.** The first `mark_succeeded` (with an empty map) is what makes the
   strategy visible to children. Without this, a child that fails before the API thread reaches the map-population
   step would default to `compressed_success_only` and fire its own immediate failure Pushover, undoing the
   consolidation.
2. **All children complete before map populated.** The defensive `_try_pipeline_notification` from the API thread
   covers this. To avoid double-firing if a worker happens to race, `_try_pipeline_notification` calls
   `try_acquire_notification_lock(parent_job_id)` on the repository, which atomically sets
   `result_json._notification_sent = 1` and returns False to subsequent callers.

## What's intentionally out of scope for v1

- **Cancellation.** There is no API to cancel a running job. Jobs are fast enough (tens of seconds for typical runs) that
  cancellation is not worth the complexity of interrupting the worker mid-entry.
- **Job history UI.** The webapp has a dedicated Job History page (`/jobs`) backed by `GET /api/jobs` that lists all
  historical jobs with filters and pagination. Old rows accumulate in the database; no pruning runs automatically.
- **Retry.** A failed _terminal_ job is not automatically retried. The user can submit a new job with the same
  parameters. Note: image and audio ingestion workers DO retry transparently within a single job execution on
  transient upstream errors (Anthropic 529 / overloaded, Google 503 UNAVAILABLE / 429 RESOURCE_EXHAUSTED, OpenAI
  rate limit) using exponential backoff at 3 / 6 / 12 / 24 / 48 minutes — see
  `services/jobs/retry.py::run_with_retry` and `services/jobs/errors.py::is_transient`. The first retry sends a
  `notify_retrying` Pushover; subsequent retries are silent. After all retries are exhausted (or on a non-transient
  error) the job moves to `failed` and behaves as documented in this bullet.
- **Multi-worker parallelism.** Would require rethinking the SQLite threading model and per-LLM rate limits.
