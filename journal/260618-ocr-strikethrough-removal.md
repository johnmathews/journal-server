# OCR — drop crossed-out (strikethrough) words from entry text

**Date:** 2026-06-18
**Branch:** `<current server branch>`
**Reference doc:** [`docs/ocr-context.md`](../docs/ocr-context.md) (new "Crossed-out words" section)

## Context

When ingesting handwritten pages, if the author crosses a word out (a
line struck through a mistake), the OCR model was transcribing it as
Markdown strikethrough (`~~mistaken-word~~`) and that text leaked
straight into the entry. The user wants crossed-out words **gone** —
they're deliberate deletions, not content.

There was no strikethrough handling anywhere in the code: the model just
naturally emitted `~~…~~`, nothing stripped it, and `~~` wasn't used
downstream for anything else (verified by grep across `src/` and
`tests/`).

## Change

Two layers, both in `providers/ocr.py`:

1. **System prompt** — added a sentence to the shared `SYSTEM_PROMPT`
   telling the model that crossed-out text is a deletion: omit it
   entirely, don't transcribe it, don't mark it with strikethrough.
   Covers both the Anthropic and Gemini providers (one shared prompt).

2. **Deterministic stripper** — new `strip_strikethrough(raw)` removes
   any `~~…~~` the model emits anyway, as a safety net. Called on the raw
   model output **before** `parse_uncertain_markers` so the
   `uncertain_spans` character offsets stay anchored to the final text.
   Wired into both providers' `extract()`.

The stripper cleans up the whitespace a removal would strand: collapses
double spaces, drops a space stranded before sentence punctuation
(`"happy ."` → `"happy."`), and trims line whitespace. The regex is
non-greedy and single-line, so `~~a~~b~~c~~` keeps `b`, and a lone
unmatched `~~` is left alone instead of eating the rest of a paragraph.

## Notes / decisions

- **Order matters:** strikethrough is stripped before sentinel parsing
  precisely so the uncertain-span offsets don't shift. A test
  (`test_extract_strikethrough_keeps_uncertain_spans_valid`) pins this —
  `"Met ~~Bob~~ ⟪Ritsya⟫ today."` → `"Met Ritsya today."` with the span
  still on `Ritsya`.
- **Forward-only:** existing stored entries are untouched. Only new
  ingestions get the cleanup. Could add a backfill later if wanted.
- **Wrapped struck phrase** (strikethrough spanning a line break) is the
  accepted tail risk for the deterministic stripper (single-line regex);
  the prompt instruction is the primary defence there.

## Tests

TDD: wrote the failing tests first (RED — `ImportError` for the missing
function), then implemented.

- `TestStripStrikethrough` — 12 unit cases for the pure function (mid-
  sentence, start, end, before-punctuation, multiple spans, non-greedy
  boundaries, lone `~~`, paragraph preservation).
- `TestAnthropicOCRProvider.test_extract_drops_crossed_out_words` and
  `…_keeps_uncertain_spans_valid` — provider-level integration.
- `test_system_prompt_instructs_omitting_crossed_out_text` — guards the
  prompt instruction against accidental deletion.

Full unit suite: 2924 passed. `ruff` clean.
