# OCR multi-entry page segmentation

**Date:** 2026-06-03

## Problem

A single photographed page that contains the tail of a previous journal entry
above a fresh dated entry was stored as one entry. The heading detector only
inspects the first 300 chars, so the new entry's date (sitting partway down
the page) was buried in the body text and never became the entry's filing
date. The same applied to back-to-back short entries on one page.

## Approach

Push the segmentation responsibility into the vision step (the only component
that sees the spatial layout — gaps between entries, indentation, etc.) and
let post-OCR code split on a known marker. Specifically:

- Updated `SYSTEM_PROMPT` (OCR) to instruct the model to emit the literal
  marker `<<<NEW ENTRY>>>` on its own line immediately before each new
  entry's date heading whenever a single image contains the start of more
  than one entry. The model does NOT emit the marker for the first entry
  on the page or for continuation pages.
- Added `split_text_into_entries(text, spans)` in `services/ingestion/image.py`
  that splits OCR output on the marker. The **orphan tail** above the first
  marker is discarded per the project policy chosen this round
  (alternatives considered: append to most recent prior entry; create as a
  fragment; prompt the user). Discard wins because almost all orphan tails
  are recently-stored content from a known previous entry — losing them is
  cheap.
- Refactored `ingest_image` to: OCR → split → loop creating one entry per
  surviving segment via a new `_create_entry_from_image_segment` helper.
  Each segment runs its own date extraction and heading detection in
  isolation, so two dated entries on one page file under their own dates.
  Returns the LAST entry (most recently dated segment), which is the entry
  the user typically intended to capture.
- Each entry gets its own `source_files` row referencing the shared image
  hash. `source_files.file_hash` is indexed but not UNIQUE in the schema,
  so this works. The upload-time `_is_duplicate` check at the top of
  `ingest_image` remains the only guard against re-uploads.

## Scope and tradeoffs

- **Multi-page uploads (`ingest_multi_page_entry`) untouched.** When the
  user explicitly batches multiple images as one entry, we honour that
  intent and do not segment, even if a page in the batch happens to
  contain the marker. Edge case: the marker would survive as literal text
  in the combined entry — flagged in the architecture doc; users with a
  true multi-entry-multi-page situation should upload pages individually.
- **Follow-up jobs (mood scoring, entity extraction) are queued for the
  returned (last) entry only**, by the existing image-ingestion worker.
  In the rare 3+-entries-on-one-page case, earlier segments do not get
  follow-ups via this upload — they will be picked up by any batch
  re-scoring run or can be triggered manually. If this becomes common, a
  small follow-up PR can change the worker to queue follow-ups per
  created entry; deferred for now to keep PR2 focused.
- **Return type unchanged (`Entry`, not `list[Entry]`).** Avoids breaking
  the MCP tool and the job worker callers. Sacrifices: the caller doesn't
  know about the secondary entries, only the primary one. The entries
  list view shows them.

## Tests

- `TestSplitTextIntoEntries` — 7 unit tests covering: no-delimiter
  passthrough, single-delimiter discard-orphan, two-delimiter two-entry,
  delimiter-at-start no-orphan, trailing-delimiter fallback, span
  re-anchoring into segment-local coords, span-in-orphan dropped.
- `TestIngestImageMultipleEntries` — 3 end-to-end tests using a mocked
  OCR provider that returns delimiter-marked text. Confirms entries are
  persisted, raw_text is clean of orphan and delimiter, returned entry
  is the latest.
- `test_system_prompt_mentions_entry_delimiter` (in `test_ocr.py`) — guards
  against future prompt edits silently removing the segmentation
  instruction.

## Sibling

PR1: OCR line-break reflow (prompt rewrite + reflow on Anthropic path) —
`fix/ocr-line-break-reflow`. Both PRs originated from the same discussion
about OCR page-to-text fidelity.
