# Voice recording ingestion via REST API

Added async audio ingestion support so the webapp can record voice entries and submit
them for transcription.

## What changed

- **New `ingest_audio` job type** in `models.py` — extends the `JobType` literal.
- **`ingest_multi_voice()` in `ingestion.py`** — transcribes multiple audio recordings
  via the existing Whisper provider, concatenates the texts with newline separators, and
  creates a single `voice` entry. Mirrors `ingest_multi_page_entry` for images. For a
  single recording, delegates to the existing `ingest_voice()`.
- **Job runner** (`jobs.py`) — `submit_audio_ingestion()` stores recordings in a
  `_pending_audio` dict (same memory pattern as images), `_run_audio_ingestion()` worker
  includes retry logic with exponential backoff for transient API errors.
- **REST endpoint** `POST /api/entries/ingest/audio` — accepts multipart audio files
  (MP3, MP4, WAV, WebM, OGG, FLAC, M4A) with limits of 100 MB/file and 500 MB total.
  Returns 202 with `{job_id, status}` for async processing.
- Entity extraction is queued as a follow-up job after successful transcription.

## Tests

- 9 new ingestion service tests (multi-voice: delegates single, multiple recordings,
  newline join, duplicates, empty transcription, progress callback, chunks).
- 8 new API endpoint tests (single/multiple recordings, no audio, wrong type, size
  limit, custom date, MP3/WAV acceptance).
- 6 new job runner tests (single/multiple success, empty rejection, job type, recording
  count in params, no ingestion service failure).

## Design decisions

- Audio file size limits (100 MB/file, 500 MB total) are significantly higher than
  images (10 MB/file, 50 MB total) because voice recordings can be 30+ minutes long.
- Multiple recordings are joined with `\n` (not `\n\n`) to keep the chunker blind to
  recording boundaries, same rationale as multi-page image joining.
