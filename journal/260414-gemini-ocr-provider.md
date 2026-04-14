# Gemini OCR provider — switchable via env var

## What changed

Added Google Gemini as an alternative OCR provider for handwriting recognition,
selectable at runtime via the `OCR_PROVIDER` env var.

### Motivation

Benchmarks (AIMultiple handwriting recognition, late 2025) show Gemini 3 Pro
scoring higher than Claude on cursive handwriting recognition specifically.
For the journal's use case — sharp, high-res photos of handwritten pages on
lined/plain paper, simple layout — Gemini may produce fewer uncertain spans
and better raw text quality. Adding it as a switchable provider allows a
real-world A/B comparison without code changes.

### Implementation

- **`GeminiOCRProvider`** in `providers/ocr.py` — uses `google-genai` SDK,
  same `SYSTEM_PROMPT` and `⟪/⟫` uncertainty sentinels as the Anthropic
  provider. The downstream pipeline (sentinel parser, `uncertain_spans`,
  webapp Review toggle) works identically regardless of provider.
- **`build_ocr_provider(config)`** factory function — reads `config.ocr_provider`
  and constructs the right provider. Both `mcp_server.py` and `cli.py` now
  call this instead of constructing `AnthropicOCRProvider` directly.
- **Config changes**: `OCR_PROVIDER` (`anthropic`|`gemini`), `GOOGLE_API_KEY`,
  `OCR_MODEL` (optional override, defaults per-provider).
- ~~**Limitation**: Gemini provider does not use the context-priming glossary
  (`OCR_CONTEXT_DIR`) — only the base system prompt is sent.~~ **Resolved 2026-04-14**: Gemini
  now receives the full context glossary + anti-hallucination instructions, identical to Anthropic.

### Testing

11 new tests: 5 for `GeminiOCRProvider` (protocol conformance, extraction,
sentinel parsing, system prompt, extract_text wrapper) and 6 for
`build_ocr_provider` (both providers, default models, explicit override,
unknown provider error). Full suite: 763 passed.
