# Auto entity re-extraction on text save

**Date:** 2026-04-13

## Summary

When entry text is saved via `PATCH /api/entries/{id}`, the API now queues an async
entity extraction job so entity mentions stay in sync with corrected text.

## Changes

### API handler (`api.py`)

After `update_entry_text` succeeds, the PATCH handler submits an entity extraction
job via `job_runner.submit_entity_extraction({"entry_id": entry_id})`. The response
includes `entity_extraction_job_id` when a job is queued.

Entity re-extraction is best-effort: if the job runner is unavailable (e.g. during
tests or if the service is degraded), the save still succeeds and the failure is
logged as a warning. The SQLite trigger already marks the entry as stale, so a
subsequent `--stale-only` batch run would catch it.

### Documentation

Updated `docs/entity-tracking.md` and `docs/api.md` to document the new automatic
extraction trigger and the `entity_extraction_job_id` response field.

## Tests added

- `test_patch_text_succeeds_without_job_runner` — PATCH works when no job_runner present
- `test_patch_text_queues_entity_extraction` — verifies job is submitted and ID returned
- `test_patch_date_only_does_not_queue_extraction` — date changes don't trigger extraction
