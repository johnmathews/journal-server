# Compress pipeline Pushover notifications into a single message

## Problem

Ingestion pipelines (audio and image) sent 3 separate Pushover notifications
on the happy path: one when the parent ingestion job completed, one when mood
scoring completed, and one when entity extraction completed. The design
intention (documented in 260423-pushover-notifications.md) was "1 notification
for the happy path, not 4 per pipeline stage", but the implementation didn't
deliver on that — each job independently called `_notify_success`.

The in-browser toast compression (webapp commit 5ced1d3) was working correctly;
this was a server-side issue only.

## Solution

Follow-up jobs (mood scoring, entity extraction) now carry a `parent_job_id`
in their params when auto-triggered by an ingestion pipeline. This lets each
follow-up distinguish between "I was triggered as part of a pipeline" vs
"I was manually triggered as a standalone batch job".

### Notification flow

1. Parent ingestion job completes → **no notification** (suppressed, unless no
   follow-ups were queued — e.g. during server shutdown)
2. Each follow-up completes (success or failure) → calls
   `_try_pipeline_notification(parent_job_id)`
3. `_try_pipeline_notification` checks if all sibling follow-ups have reached
   a terminal state (succeeded or failed)
4. If any sibling is still running → no-op (wait for the last one)
5. When all are terminal → merge results from succeeded follow-ups into a
   combined dict, send a single combined Pushover notification

### Failure handling

- If a follow-up **fails**, `_notify_failed` fires immediately so the user
  knows about the failure. Then `_try_pipeline_notification` is called to
  check if all siblings are terminal — if so, the combined notification fires
  with results from whichever follow-ups succeeded.
- If **both** follow-ups fail, the user gets 2 failure notifications plus 1
  combined notification showing just "Entry N created" (no enrichment data,
  and no misleading "All processing complete").
- If both follow-ups **fail to queue** (e.g. executor shutting down), the
  parent sends its own notification directly as a fallback.

### Combined notification content

The single notification includes:
- Entry ID (from parent ingestion result)
- Mood score count (from mood scoring result, if it succeeded)
- Entity + mention counts (from entity extraction result, if it succeeded)

Happy path example: "Entry 76 created / 7 mood scores / 8 entities, 18 mentions"
Partial failure example: "Entry 76 created / 8 entities, 18 mentions" (mood failed)

### Standalone batch jobs unaffected

Jobs triggered manually (entity extraction batch, mood backfill) have no
`parent_job_id` in their params, so they continue to notify individually.

## Files changed

- `src/journal/services/jobs.py` — Added `parent_job_id` to allowed params for
  entity_extraction and mood_score_entry; modified `_queue_post_ingestion_jobs`
  to pass it; suppressed parent ingestion notification; added
  `_try_pipeline_notification` helper; wired follow-up runners to use it on
  both success and failure paths; fallback notification when no follow-ups queued
- `src/journal/services/notifications.py` — Updated `_build_success_message` to
  include mood + entity results when available, and avoid misleading "All
  processing complete" when follow-ups were queued but all failed
- `tests/test_services/test_jobs_runner.py` — 9 new tests covering: happy path
  (audio + image), standalone jobs still notify, parent_job_id stored in params,
  mood-fails-entity-succeeds, entity-fails-mood-succeeds, both fail (no
  misleading message), message content verification
- `tests/test_services/test_notifications.py` — 1 new test: combined message
  includes mood + entity results
- `docs/jobs.md` — Added "Pipeline notifications" section with notification
  matrix and edge case documentation

## Why server-side, not webapp

The webapp toast compression (commit 5ced1d3) groups related frontend toasts.
But Pushover notifications are sent by the server — the webapp has no control
over them. The server needed its own pipeline grouping logic.
