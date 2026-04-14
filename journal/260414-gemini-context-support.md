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

### Impact

OCR accuracy for proper nouns (family names, place names, recurring topics) should
improve immediately when using the Gemini provider. No configuration changes needed —
`OCR_CONTEXT_DIR` is already set in the deployment environment.
