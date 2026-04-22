# Transcription Confidence Scoring via Logprobs

## What changed

- `TranscriptionProvider.transcribe()` now returns `TranscriptionResult` (text + uncertain
  spans) instead of a plain string.
- `OpenAITranscriptionProvider` passes `include=["logprobs"]` and `response_format="json"` to
  `gpt-4o-transcribe`. Each token in the response includes a log-probability.
- New `_logprobs_to_uncertain_spans()` function converts low-confidence tokens into
  character-offset uncertain spans: strips token whitespace, merges adjacent flagged tokens,
  expands to word boundaries, and re-merges after expansion.
- `ingest_voice()` and `ingest_multi_voice()` store the resulting spans via the existing
  `add_uncertain_spans()` method — the same `entry_uncertain_spans` table used by OCR doubts.
- Multi-voice offset calculation accounts for leading whitespace stripping (same pattern as
  `_strip_and_shift_page_spans` for multi-page OCR).
- New `TRANSCRIPTION_CONFIDENCE_THRESHOLD` config (default -0.5 ≈ 60% confidence).

## Why

The webapp already has a Review UI (yellow highlights, Prev/Next, "All Verified") for OCR
uncertain spans. By storing voice transcription uncertainty in the same table, the existing UI
lights up automatically for voice entries. No webapp changes needed beyond label adjustments.

## Models that support logprobs

Only `gpt-4o-transcribe` and `gpt-4o-mini-transcribe`. `whisper-1` does not — the provider
gracefully returns empty spans for unsupported models.

## Testing

20 new tests for the transcription provider (logprob conversion, model detection, threshold
boundaries) and 4 new tests for voice uncertain span storage in the ingestion service.
