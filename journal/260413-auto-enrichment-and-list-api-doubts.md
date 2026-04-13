# Auto entity extraction & mood scoring on all paths + uncertain_span_count in list API

## What changed

### Automatic post-ingestion enrichment

Entity extraction and mood scoring now fire automatically on every entry
lifecycle event, closing a gap where new entries were only partially enriched:

- **POST /api/entries/ingest/text** and **POST /api/entries/ingest/file**: now
  submit both `mood_score_entry` (if enabled) and `entity_extraction` jobs
  after creating the entry. Previously only mood scoring was queued.

- **Image ingestion job worker** (`_run_image_ingestion`): now submits
  `entity_extraction` as a follow-up job after the image ingestion succeeds.
  Mood scoring already ran inline inside the ingestion service. The follow-up
  job queues on the same single-worker executor, so it starts after the image
  job marks itself complete.

- **PATCH /api/entries/{id}**: now submits `mood_score_entry` alongside the
  existing `entity_extraction` and `reprocess_embeddings` jobs when text is
  updated. Previously only entity extraction and re-embedding fired.

All new job submissions are best-effort (try/except, logged on failure) and
don't block the HTTP response.

The PATCH handler was also hardened: `services["job_runner"]` changed to
`services.get("job_runner")` with a None guard, so tests without a job runner
don't crash.

### uncertain_span_count in list API

- Added `get_uncertain_span_count(entry_id)` to the repository Protocol and
  SQLite implementation (simple COUNT query on `entry_uncertain_spans`).
- `_entry_summary()` now accepts and returns `uncertain_span_count`.
- `GET /api/entries` queries the count per entry in the existing list loop.

### Tests

- New tests in `test_api_ingest.py`:
  - `TestAutoEntityExtraction` — verifies text and file ingest queue entity extraction
  - `TestPatchMoodScoring` — verifies PATCH queues mood scoring when config is set
  - `TestListEntriesUncertainSpanCount` — verifies list includes per-entry counts

## Decisions

- Entity extraction on image ingestion is a **follow-up job** (not inline)
  to keep the image ingestion job focused on OCR. The single-worker executor
  serializes them naturally.
- Mood scoring on PATCH is gated on `config.enable_mood_scoring` for
  consistency with the ingest endpoints.
