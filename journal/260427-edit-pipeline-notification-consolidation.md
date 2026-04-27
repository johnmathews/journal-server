# 2026-04-27 — Edit-pipeline notification consolidation

## Why

PATCH `/api/entries/{id}` (the edit-save flow) was sending one Pushover notification *per* background job — three
pushes per edit (reprocess_embeddings, entity_extraction, mood_score_entry). The new-entry ingestion flow already
collapses to **one** summary push via the existing pipeline mechanism, so the goal here is to give edits the same UX:
one consolidated Pushover, success or failure.

The edit flow has an extra requirement that the new-entry flow does not: failure consolidation. New-entry's existing
`compressed_success_only` strategy fires per-stage failure pushes immediately and only collapses the eventual success
summary. For edits, both successes and failures must roll up into one push.

## Design

A new pipeline strategy field, `notify_strategy`, is stored on the parent job's **`params_json`** (not result):

- `compressed_success_only` (default for legacy parents) — current new-entry behavior, unchanged.
- `compressed_all` — used by the new edit pipeline. Both per-stage success and per-stage failure pushes are
  suppressed; the pipeline summary fires once and uses a per-stage `+`/`-` breakdown if any child failed.

The strategy lives in params (fixed at parent-creation time) rather than result so it is visible to children's
strategy checks the moment the parent row exists, with no additional `mark_succeeded` UPDATE required. An earlier
draft of this work used `mark_succeeded` twice (once with empty map + strategy, once with populated map) to make
the strategy visible before the map was populated — but that doubled SQLite writes on the shared connection and
triggered the project's known threading edge case (`sqlite3.OperationalError: not an error`) under CI's timing,
breaking `tests/test_api_ingest.py::TestPatchMoodScoring::test_patch_text_queues_mood_scoring`. Moving the
strategy into params eliminated the second UPDATE.

A synthetic parent job of type `save_entry_pipeline` carries the strategy (in params) and the `follow_up_jobs`
map (in result). It does no actual work — `JobRunner.submit_save_entry_pipeline()` creates it, submits the three
children with `parent_job_id` set, then calls `mark_succeeded` once with the populated map.

A defensive `_try_pipeline_notification` call from the API thread covers the rare case where every child completed
in the gap before the populated map landed. To prevent double-firing when a worker call races with this defensive
sweep, `_try_pipeline_notification` now calls `SQLiteJobRepository.try_acquire_notification_lock(parent_job_id)`,
which atomically sets `result_json._notification_sent = 1` and returns False to any subsequent caller.

## Why a synthetic parent and not a `pipeline_id` refactor

The existing pipeline machinery (`_try_pipeline_notification`, `parent_job_id` plumbing, `follow_up_jobs` on the
parent's result) is built around a real parent job that holds the map. The new-entry flow piggybacks the map onto
the ingest job. For edits there is no natural parent — `reprocess_embeddings` etc. are siblings — so the cleanest fit
was a synthetic, no-op parent. This avoids a deeper refactor that would touch the new-entry flow, which the user
explicitly said is "working great" and shouldn't change.

## Frontend

Almost no work needed. `AppNotifications.vue` already emits one toast per terminal job, so per-stage in-app toasts
fall out for free. `EntryDetailView.vue` already creates a `groupId` and tracks the three child IDs; the only change
was renaming the misleading group label from "Entry updated — all processing complete" to "Entry update", since on a
partial failure the old label would lie.

## API contract

`PATCH /api/entries/{id}` response gains a new optional `pipeline_job_id` field (the synthetic parent). The
existing `entity_extraction_job_id`, `reprocess_job_id`, and `mood_job_id` fields are preserved unchanged so the
webapp's existing job-tracking code keeps working without changes.

## Tests

- `TestSaveEntryPipeline` — 7 new tests in `test_jobs_runner.py` covering the happy path, partial failure, total
  failure, mood-disabled path, parent_job_id propagation to children, the synthetic parent's lifecycle, and a
  regression test asserting the new-entry flow's existing notifications are unchanged.
- `TestBuildPipelineFailureBody` and `TestNotifyPipelineFailed` — new direct unit tests for the notification
  service additions in `test_notifications.py`.
- `test_patch_text_queues_entity_extraction` was renamed to `test_patch_text_queues_save_entry_pipeline` and
  updated to mock the new submit method and assert all four IDs in the response.

All 1318 server tests pass. All 1160 webapp tests pass.

## What this does not change

- The new-entry ingestion flow — its `compressed_success_only` strategy is the default and unchanged.
- The webapp's per-stage toast behavior — it was already correct.
- The DB schema — no migration; `jobs.type` is free-text TEXT.
- The orphan-entity cleanup logic (`entity_extraction.py:135–291`) — verified correct on prod for entries 76 and 77.
  A separate finding from this work: prod entity #671 ("Nautilin") is the result of an **LLM mis-extraction** (the
  canonical_name doesn't match the source quote "Nautiline"), not a code truncation bug — that's a follow-up.
