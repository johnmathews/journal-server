# Entry content window

**Date:** 2026-06-18

## Problem

Handwritten journal pages rarely align neatly with entry boundaries. A first page can begin with the tail of a
previous, already-stored entry sitting above this entry's date heading. A last page can end with a fresh date heading
below where this entry finishes. Both cases caused neighbour text to pollute `final_text`, FTS5 search, embeddings,
and mood scoring.

The earlier multi-entry segmentation approach (see
[260603-ocr-multi-entry-segmentation.md](260603-ocr-multi-entry-segmentation.md)) handled the single-image case by
splitting on a `<<<NEW ENTRY>>>` marker and filing separate entries — but silently discarded leading orphan text and
left multi-page uploads completely unhandled. It also created an asymmetry: one upload could produce multiple entries,
which complicated follow-up job dispatch and the data model.

## Design

Full spec at [`docs/superpowers/specs/2026-06-18-multipage-entry-boundaries-design.md`](../docs/superpowers/specs/2026-06-18-multipage-entry-boundaries-design.md).

### Begin/end marker scheme + PageRole

The OCR provider now receives a `PageRole` (FIRST / MIDDLE / LAST / ONLY) for every page. The role drives an addendum
appended to the system prompt:

- **FIRST** — if a previous entry's tail sits above this entry's first line, emit `<<<ENTRY BEGINS>>>` immediately
  before that line. Never emit `<<<ENTRY ENDS>>>`.
- **MIDDLE** — pure continuation; emit neither marker.
- **LAST** — if a different entry begins below where this one ends, emit `<<<ENTRY ENDS>>>` immediately after this
  entry's last line. Never emit `<<<ENTRY BEGINS>>>`.
- **ONLY** (single-page upload) — emit `<<<ENTRY BEGINS>>>` before the first line if there's a leading tail, emit
  `<<<ENTRY ENDS>>>` after the last line if there's a trailing neighbour.

`assign_roles(n)` in `services/ingestion/boundaries.py` derives the role list for an `n`-page upload (n=1 → [ONLY],
n≥2 → [FIRST, MIDDLE*, LAST]).

### Content window model

After OCR, the page texts are combined (stripped + single-`\n` join) and passed to `extract_content_window()`. This
pure-Python function:

1. Scans for `<<<ENTRY BEGINS>>>` / `<<<ENTRY ENDS>>>` tokens in a single pass.
2. Strips the tokens from the text, producing a clean `raw_text`.
3. Records the half-open `[start, end)` char offsets into the clean text where the target entry begins and ends.
4. Re-anchors uncertain spans into the clean coordinates, dropping any that fell inside a removed marker.

**Semantics:**

- `raw_text` is stored verbatim (markers stripped, neighbour text kept). Zero data loss.
- `content_start_char` / `content_end_char` form a half-open window `[start, end)` into `raw_text`.
- `NULL` / `NULL` means the whole `raw_text` is in-bounds (entries with no neighbour text, or pre-feature entries).
- `final_text`, chunks, embeddings, FTS5 indexing, and mood scoring are all derived exclusively from the in-bounds
  slice `raw_text[start:end]`.
- The `content_boundary` field on entry API responses exposes `{char_start, char_end}` or `null`.

### One entry per upload

The multi-entry fan-out (`ingest_image_entries`, `_create_entry_from_image_segment`, `split_text_into_entries`,
`ENTRY_DELIMITER`) was removed. Every image upload — single or multi-page — now produces exactly **one** entry via a
unified `_ingest_pages` path. A second date heading on a page is treated as a neighbour to grey out, never as a
separate entry to file.

### Derived artifacts use the in-bounds slice

The ingestion pipeline extracts the date heading, runs heading detection, computes word count, generates chunks,
embeddings, FTS5 entries, and the mood score — all from `raw_text[start:end]`, not from the full `raw_text`. The
webapp reads view renders `final_text` (already in-bounds). Only `raw_text` (and its `uncertain_spans`) address the
full page text.

## PATCH adjust/clear contract

`PATCH /api/entries/{id}` accepts `content_start_char` and `content_end_char`. The two fields must be supplied
together:

- **Both integers** — adjust: validate `0 <= start < end <= len(raw_text)`, persist the window, re-derive
  `final_text` from the new slice, and queue the save-entry pipeline (re-embed + entity extraction + mood).
- **Both null** — clear: reset to `NULL / NULL` (full text in-bounds), re-derive, and requeue the pipeline.
- **One null, one integer** — rejected (400) with an explicit error message.

## Tests

- `TestAssignRoles` — role assignment for n=0..5 pages.
- `TestExtractContentWindow` — ~18 unit tests: begins-only, ends-only, both, neither, multi-marker, inverted window
  fallback, span re-anchoring, newline handling after `ENTRY_BEGINS`.
- Ingestion integration tests: single page with leading tail, single page with trailing neighbour, both, neither;
  multi-page first+middle+last; asserts `content_start_char`, `content_end_char`, `final_text`, and chunk content.
- Worker regression test: one image with trailing neighbour → exactly one entry (guards fan-out removal).
- PATCH API tests: adjust, clear, partial-null rejection, out-of-range rejection, pipeline queued once.
