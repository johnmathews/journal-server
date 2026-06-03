# OCR line-break reflow — fix bleed-through of physical line breaks

**Date:** 2026-06-03

## Problem

Photographed journal pages sometimes came out with the page's physical line
breaks preserved verbatim in the stored text — every hand-wrapped visual line
became a hard `\n` in `entries.raw_text` rather than a single space inside a
paragraph. Search, display, and downstream chunking all treat that as
many short lines instead of one paragraph.

## Root cause

Two things, layered.

1. The shared OCR `SYSTEM_PROMPT` literally instructed the model to "Preserve
   paragraph breaks and line structure." Gemini (production primary) read
   "preserve line structure" as "mirror each visual line into the output."
2. `reflow_paragraphs()` (the regex that collapses single `\n` → space while
   keeping `\n\n`) was applied **only** inside `GeminiOCRProvider.extract`.
   The Anthropic path returned model output unchanged, so on Anthropic runs
   nothing recovered paragraph structure even when the prompt was the
   culprit.

## Change

- Rewrote `SYSTEM_PROMPT` to tell the model the opposite: output continuous
  prose, treat a wrapped line as a single space, and use `\n\n` ONLY between
  distinct paragraphs (visual gap, indent, or topic shift). When in doubt,
  prefer fewer paragraph breaks.
- Applied `reflow_paragraphs()` in `AnthropicOCRProvider.extract` too. Same
  safety-net role as on the Gemini path — single `\n` → space, `\n\n+` kept,
  character count unchanged so `uncertain_spans` stay anchored.

## Tests added

- `TestAnthropicOCRProvider.test_extract_reflows_single_newlines` — mirror of
  the existing Gemini test.
- `TestAnthropicOCRProvider.test_extract_reflow_preserves_uncertain_span_offsets`
  — confirms span offsets survive reflow on the Anthropic path too.
- `TestAnthropicOCRProvider.test_system_prompt_does_not_instruct_line_structure_preservation`
  — asserts the old "Preserve…line structure" phrase is gone and the new
  "continuous prose" directive is present, so a future prompt edit that
  reintroduces the bug fails loudly.

## Out of scope (PR2)

Mid-page entry boundaries — when a photographed page contains the tail of a
previous entry followed by a fresh dated entry starting partway down. The
heading detector only inspects the first 300 chars and there's no per-page
segmentation. Will be addressed in a follow-up that asks the vision model to
emit an explicit delimiter between distinct entries and loops over segments
in `ingest_image`. Orphan tail above the first new-entry delimiter will be
discarded.
