# 1. OCR markdown-escape stripping — page fidelity fix

**Date:** 2026-07-15

## 1.1 The bug

Ingesting a photo/scan of a page with a centered `***` divider on its own line stored the text as `\*\*\*` — literal
backslashes the author never wrote. Since the webapp renders entry text verbatim (plain Vue interpolation, no markdown
rendering), the backslashes were user-visible.

## 1.2 Root cause

Verified by elimination: the entire server pipeline (strikethrough strip → sentinel parse → reflow → page combine →
content window → store) was audited line-by-line and never inserts characters, so the backslashes had to originate in
the OCR model response. The models are trained on Markdown; `***` alone on a line is a Markdown *thematic break*, so
the model emits `\*\*\*` to keep the asterisks "literal" — correct for a Markdown consumer, wrong for this app's
plain-text entries. The same defect class applies to every CommonMark-escapable character (`\_`, `\#`, `\[`, …).

## 1.3 The fix (two layers, mirroring the strikethrough design)

1. **Deterministic safety net** — new `strip_markdown_escapes()` in `src/journal/providers/ocr.py`: removes a
   backslash before any CommonMark-escapable character (exactly the ASCII punctuation set,
   `re.compile(r"\\([!-/:-@\[-`{-~])")`), including `\\` → `\`. Wired into both `AnthropicOCRProvider.extract` and
   `GeminiOCRProvider.extract`, **after** `strip_strikethrough` (so a handwritten literal `\~\~` unescapes to `~~`
   only after the strikethrough stripper has run, and survives) and **before** `parse_uncertain_markers` (unescaping
   changes character counts, so it must precede uncertain-span offset computation).
2. **Prompt hardening** — `SYSTEM_PROMPT` now instructs: output plain text, never escape punctuation with backslashes,
   reproduce punctuation exactly as written. The code layer remains the guarantee.

## 1.4 Tests (failing-test-first)

Provider-level regression tests reproducing the exact reported case were written first and confirmed red
(`\*\*\*` retained), then the fix made them green:

- `TestStripMarkdownEscapes` — unit tests: the reported divider case, full CommonMark escapable set, `\\` → `\`,
  backslash-before-letter/Unicode-punctuation/trailing-backslash untouched.
- `TestAnthropicOCRProvider` / `TestGeminiOCRProvider` — mocked-API regression tests for the escaped divider, plus an
  Anthropic test proving uncertain-span offsets stay valid after unescaping.
- `test_system_prompt_forbids_markdown_escaping` — locks the prompt instruction in.

Full suite: 3142 passed, 11 integration skipped (Chroma down), coverage 87.97%.

## 1.5 Decisions and notes

- **Forward-only.** Entries stored before this fix are not retroactively re-cleaned (same policy as strikethrough
  stripping). A one-off repair pass over existing entries containing model-inserted escapes is possible but rewrites
  stored text and re-derives chunks/embeddings — deferred pending user sign-off.
- **Centering whitespace:** the pre-existing `strip_strikethrough` whitespace normalization trims leading/trailing
  line whitespace, so a *centered* divider is stored as `***` at line start. This session's fix addresses the
  backslashes only; line-position fidelity was already normalized away and is unchanged.
- **Voice path not touched.** The transcription/formatter providers could plausibly exhibit the same defect class;
  not investigated (bug report was photo/scan-specific).
- Docs updated: `docs/ocr-context.md` gained a "Markdown escapes (page fidelity)" section mirroring the strikethrough
  section's two-layer structure.
- Run artifacts (evaluation report, improvement plan) under `.engineering-team/runs/manual-20260715T093239Z/`.
- Doc-audit side finding (pre-existing, not fixed here): `CLAUDE.md` says prod OCR = Gemini while
  `docs/ocr-context.md` says prod runs `OCR_DUAL_PASS=true` (Anthropic primary) — needs reconciling.
