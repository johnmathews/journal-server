# Gemini OCR now receives the context glossary

## What changed

Fixed a bug where `GeminiOCRProvider` was not receiving the context-priming glossary
(people, places, topics from `OCR_CONTEXT_DIR`). Only `AnthropicOCRProvider` was wired
to load and send context files — Gemini received only the bare `SYSTEM_PROMPT`, which
meant it had no candidate list for proper nouns like family names or place names.

### Root cause

When the Gemini provider was added (260414), context support was noted as a known
limitation. The `GeminiOCRProvider.__init__` didn't accept a `context_dir` parameter,
and `build_ocr_provider()` didn't pass one. The Gemini SDK's `system_instruction`
parameter supports arbitrary text just like Anthropic's `system` parameter, so no
API limitation prevented it — it was simply not implemented.

### Fix

- Added `context_dir: Path | None = None` to `GeminiOCRProvider.__init__`
- Provider now calls `load_context_files()` and composes
  `SYSTEM_PROMPT + CONTEXT_USAGE_INSTRUCTIONS + glossary` — identical to Anthropic
- Updated `build_ocr_provider()` to pass `config.ocr_context_dir` to Gemini
- Updated `docs/ocr-context.md` to reflect both providers support context priming
- Added 4 new tests mirroring Anthropic's context tests (composition, missing dir,
  empty dir, factory passthrough)

### Gemini line-break reflow

Gemini preserves physical line breaks from handwritten pages, producing many short
lines. Added `reflow_paragraphs()` — replaces single `\n` with a space while
preserving `\n\n+` paragraph breaks. Applied after sentinel parsing in
`GeminiOCRProvider.extract()`. The 1-for-1 character swap keeps uncertain span
offsets valid. 11 new tests (9 unit + 2 integration).

### Fix SQLite threading race in job repository

The `SQLiteJobRepository` shared a single `sqlite3.Connection` across the API
handler thread and the `JobRunner` executor thread without synchronization.
When the API handler called `create()` (INSERT + COMMIT) while the executor
thread was calling `mark_running()` or `mark_succeeded()` (UPDATE + COMMIT),
the concurrent commits caused `sqlite3.OperationalError: not an error` — a
known SQLite threading issue. Added a `threading.Lock` to all repository
methods. Reproduced at ~20% failure rate before the fix; 50/50 passes after.

### Impact

OCR accuracy for proper nouns (family names, place names, recurring topics) should
improve immediately when using the Gemini provider. No configuration changes needed —
`OCR_CONTEXT_DIR` is already set in the deployment environment. OCR output is also
now naturally reflowed into paragraphs instead of preserving hard line breaks.
