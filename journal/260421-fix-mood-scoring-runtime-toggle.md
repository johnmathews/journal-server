# Fix mood scoring runtime toggle not propagating

**Date:** 2026-04-21

## Problem

Toggling `enable_mood_scoring` in the webapp's Settings page had no effect until the server was
restarted. The `_on_runtime_setting_change` callback in `mcp_server.py` handled `ocr_dual_pass`,
`ocr_provider`, and `preprocess_images` but had no case for `enable_mood_scoring`. This meant mood
scoring would not run for entries ingested after the setting was changed.

Entity extraction was unaffected because it's always queued as a separate job with no feature-flag
check.

## Fix

Added an `enable_mood_scoring` handler to the runtime settings callback. When enabled, it creates
a fresh `MoodScoringService` (with current config for model, dimensions, API key) and sets it on
both `IngestionService._mood_scoring` and `JobRunner._mood_scoring`. When disabled, both references
are set to `None`.

## Files changed

- `src/journal/mcp_server.py` — add `enable_mood_scoring` case to `_on_runtime_setting_change`
- `docs/mood-scoring.md` — document runtime toggle behavior
- `docs/jobs.md` — note that webapp bell auto-discovers follow-up jobs
