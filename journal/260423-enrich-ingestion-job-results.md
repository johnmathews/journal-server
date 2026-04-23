# Enrich ingestion job results

## What changed

Image and audio ingestion jobs now store richer metadata in their result dict
instead of just `{"entry_id": N}`.

### New result fields for `ingest_images`:
- `entry_date`, `source_type`, `word_count`, `chunk_count`, `page_count`
- `follow_up_jobs`: dict mapping label to job ID for queued mood scoring and
  entity extraction jobs

### New result fields for `ingest_audio`:
- `entry_date`, `source_type`, `word_count`, `chunk_count`, `recording_count`
- `follow_up_jobs`: same as above

### `_queue_post_ingestion_jobs` return value
Changed from `None` to `dict[str, str]` so the caller can include follow-up
job IDs in the ingestion result. The follow-up queuing now happens *before*
`mark_succeeded` so the IDs are available in the result dict.

## Why

The webapp's Job History view at `/jobs` showed "Entry Id: N" in the details
column for ingestion jobs, which duplicated the params column. With richer
result data, the webapp can now show word count, chunk count, page/recording
count, and follow-up job status.

## Tests updated

- `test_multi_image_progress_total_equals_page_count` — asserts new fields
- `test_single_recording_succeeds` — asserts new fields
- `FakeIngestionService` — now sets `word_count=2, chunk_count=1` on the
  fake entry so enriched results are realistic
