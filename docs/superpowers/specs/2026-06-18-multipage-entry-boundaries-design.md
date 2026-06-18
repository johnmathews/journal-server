# Page-position-aware entry boundaries for image entries

**Status:** design, awaiting implementation plan.
**Date:** 2026-06-18.
**Scope:** full stack (`server/` + `webapp/`).

## Problem

When a journal entry is created from handwritten page images (one image per page, uploaded
in order), the entry's true text may not fill the first and last pages:

- The **first page** can begin with the *tail of a previous, already-recorded entry*, sitting
  above this entry's date heading. Everything above the date heading is not part of this entry.
- The **last page** can contain the *start of the next entry* below where this entry ends — a
  fresh date heading and the text under it. Everything from that heading down is not part of
  this entry.

Today neither case is handled for multi-page uploads. `ingest_multi_page_entry`
(`server/src/journal/services/ingestion/image.py`) OCRs each page **independently with no page
position context** and concatenates the results with `"\n".join(...)`. It never invokes any
boundary logic, so neighbor-entry text is stored verbatim and pollutes the reading view,
search, embeddings, and mood scoring.

The single-image path (`ingest_image` → `ingest_image_entries` → `split_text_into_entries`)
does something different and now-unwanted: it splits a page on `<<<NEW ENTRY>>>` markers and
files **one separate entry per segment**, while **silently discarding** the leading orphan tail.

## Decisions (locked during brainstorming)

1. **Failure mode = keep everything, mark boundaries.** `raw_text` always stays 100% verbatim.
   Neighbor text is never deleted; it is marked out-of-bounds and greyed in the UI. Zero data loss.
2. **Boundary scope = exclude from everything but `raw_text`.** `final_text` (reading view),
   chunks/embeddings, FTS search, and mood are all derived from the **in-bounds slice** only.
   Out-of-bounds text is preserved but inert.
3. **Mechanism = role-aware OCR + deterministic Python trim.** The vision model is told each
   page's role and brackets the target entry with explicit begin/end markers; pure Python computes
   the offsets. The risky judgment (vision/layout) lives in the model; the irreversible cut is
   deterministic and unit-testable.
4. **One entry per upload, always.** Every upload — one image or many — produces exactly **one**
   entry. A second date heading on a page is treated as a *neighbor to grey out*, never as a
   separate entry to file. This **removes** the existing multi-entry-per-page fan-out
   (`ingest_image_entries` / `_create_entry_from_image_segment` / `split_text_into_entries` /
   `ENTRY_DELIMITER`), intentionally superseding the behavior documented in
   `server/journal/260603-ocr-multi-entry-segmentation.md`. Single-image and multi-image uploads
   converge on one ingestion path.
5. **Webapp v1 = adjust + reset.** Greyed out-of-bounds regions, paragraph-granular handles to
   move where the entry starts/ends, and a "use full page" reset. Persisted via PATCH.

## Core model: the content window

One new concept, modeled exactly like the existing uncertain spans
(`entry_uncertain_spans`, migration `0005`): a **content window**
`[content_start_char, content_end_char)` — a half-open character range into the entry's
`raw_text`.

- `raw_text` is verbatim and immutable (unchanged from today), with the marker tokens stripped.
- `content = raw_text[content_start_char:content_end_char]`.
- `NULL` window (both columns NULL) = the whole text. Every existing entry, plus all voice/text
  entries, default to NULL → **zero backfill required**.
- All derived artifacts (`final_text`, chunks, embeddings, FTS, mood) come from `content`.
- The UI greys the regions of `raw_text` *outside* the window.

## Marker scheme: explicit begin/end brackets

Replace the asymmetric `<<<NEW ENTRY>>>` heuristic with two explicit bracket tokens the model
places around **the entry being created**:

- `<<<ENTRY BEGINS>>>` — emitted on its own line immediately before the entry's first line,
  **only** when text belonging to a previous entry sits above it on the page.
- `<<<ENTRY ENDS>>>` — emitted on its own line immediately after the entry's last line, **only**
  when a different (new) entry begins below it on the page.

This is unambiguous for every page role and trivial to parse: in the combined text, the content
window is everything between the first `<<<ENTRY BEGINS>>>` (start defaults to 0 if absent) and
the first subsequent `<<<ENTRY ENDS>>>` (end defaults to text length if absent). Both tokens are
control tokens — stripped from `raw_text`; offsets index the stripped text.

## Server design

### OCR role plumbing — `providers/ocr.py`

Add a `PageRole` enum (`FIRST | MIDDLE | LAST | ONLY`) and an optional parameter to the OCR
protocol:

```python
def extract(self, image: bytes, media_type: str, page_role: PageRole | None = None) -> OCRResult: ...
```

`page_role=None` reproduces the **current** prompt exactly (backward compatible for any caller
that doesn't pass a role). Both adapters (Anthropic, Gemini) append a role-specific clause to the
shared `SYSTEM_PROMPT`:

- **FIRST** — "First page of an entry that continues onto later pages. If text belonging to a
  *previous* entry sits above this entry's first line, emit `<<<ENTRY BEGINS>>>` on its own line
  immediately before this entry's first line. Never emit `<<<ENTRY ENDS>>>` (the entry continues
  past this page)."
- **MIDDLE** — "A middle page of one ongoing entry — pure continuation. Emit neither marker."
- **LAST** — "The last page of the entry; the entry ends on this page. If a *different* entry
  begins below it (e.g. a fresh date heading), emit `<<<ENTRY ENDS>>>` on its own line immediately
  after this entry's last line. Never emit `<<<ENTRY BEGINS>>>`."
- **ONLY** — single image: "If a previous entry's tail sits above, emit `<<<ENTRY BEGINS>>>`
  before this entry's first line; if another entry begins below, emit `<<<ENTRY ENDS>>>` after
  this entry's last line."

### Deterministic boundary computation — new module `services/ingestion/boundaries.py`

Pure function, no model calls, fully unit-testable. Input: the per-page OCR `(text, spans)`
results paired with their roles, in page order. Output: `(combined_text, content_start,
content_end, combined_spans)` where `combined_text` has every marker token stripped, the offsets
index into it, and `combined_spans` are the uncertain spans re-anchored to it.

Algorithm:
1. For each page, strip leading/trailing whitespace (preserving the existing single-`\n` join
   rationale below) and shift the page's uncertain spans into combined coordinates — folding the
   existing `_strip_and_shift_page_spans` logic plus removal of the marker tokens.
2. Join pages with a single `"\n"`.
3. `content_start` = (index just past the first `<<<ENTRY BEGINS>>>`) or `0` if none.
4. `content_end` = (index of the first `<<<ENTRY ENDS>>>` at/after `content_start`) or
   `len(combined_text)` if none.
5. Strip the marker tokens from `combined_text` and adjust `content_start`/`content_end` and any
   spans that followed a removed token. (Cleaner alternative, decided at plan time: strip markers
   first, recording each removed token's position, then compute offsets against the stripped text.)
6. Clamp so `0 <= content_start <= content_end <= len(combined_text)`; if markers are crossed or
   malformed, fall back to the full text (`0, len`) and log a warning — never raise.

Page combination still joins stripped page texts with a single `"\n"` (the existing
chunking-budget rationale in `image.py` is preserved).

### Unified ingestion path — `services/ingestion/image.py`

Both public entry points delegate to one shared private method:

```python
def _ingest_pages(self, images: list[tuple[bytes, str]], date: str, *,
                  skip_mood: bool, on_progress, user_id: int) -> Entry: ...
```

- Assigns each page a role: N≥2 → `FIRST`, `MIDDLE…`, `LAST`; N==1 → `ONLY`.
- OCRs each page with its role, runs the duplicate check per page (unchanged), calls the boundary
  module, persists `content_start_char`/`content_end_char` on the entry, stores per-page verbatim
  `raw_text` in `entry_pages` (unchanged), and runs heading detection + `_process_text` on
  `raw_text[content_start:content_end]`.
- `ingest_image(image, media_type, date, …)` → `_ingest_pages([(image, media_type)], date, …)`.
- `ingest_multi_page_entry(images, date, …)` → `_ingest_pages(images, date, …)`.
- **Remove** `ingest_image_entries`, `_create_entry_from_image_segment`,
  `split_text_into_entries`, and the `ENTRY_DELIMITER` constant.

### Job worker — `services/jobs/workers/image_ingestion.py`

- Collapse the `len(images) == 1` branch: always call the single-entry path; `created` is always
  one entry.
- Remove the per-entry follow-up fan-out loop (`for created_entry in entries`) and the
  `entry_ids` / id-suffixed key logic — there is exactly one entry, so follow-up jobs use the
  unsuffixed keys directly.

### Storage — migration `0033`

Add two nullable columns to `entries`:

```sql
ALTER TABLE entries ADD COLUMN content_start_char INTEGER;
ALTER TABLE entries ADD COLUMN content_end_char   INTEGER;
```

Half-open, `raw_text` coordinates, same convention as `entry_uncertain_spans`. NULL = full text.
Per the migration-testing rule: query prod for the actual shape of `entries` before finalizing,
and ensure the migration is safe to re-run after a partial failure. `Entry` dataclass
(`server/src/journal/models.py`) gains `content_start_char: int | None = None` and
`content_end_char: int | None = None`; repository read/write maps the columns.

### API

- `_entry_to_dict` (`api/_shared.py`) gains `content_boundary: {char_start, char_end} | null`,
  always present (mirroring how `uncertain_spans` is always present).
- `PATCH /api/entries/{id}` (`api/entries.py`) additionally accepts `content_start_char` and
  `content_end_char`. When supplied, the entry's window is updated, `final_text` is re-derived from
  the new slice (heading detection applied), and the **existing** save pipeline reruns
  (`reprocess_embeddings` + `entity_extraction` + `mood_score_entry`). Response includes the same
  job IDs as today plus the updated `content_boundary`.
- Validation: `0 <= content_start_char < content_end_char <= len(raw_text)`; both-or-neither;
  sending explicit `null`s clears the window (back to full text).

## Webapp design

### Types & client (`src/types/entry.ts`, `src/api/entries.ts`)

- `EntryDetail` gains `content_boundary: { char_start: number; char_end: number } | null`.
- A PATCH client function to send `content_start_char` / `content_end_char` (extends the existing
  `updateEntryText` pattern, or a sibling `updateEntryBoundary`).

### Rendering (`src/composables/useDiffHighlight.ts`)

Extend the existing overlay machinery (the same approach as `applyUncertainOverlay`) with an
out-of-bounds segment kind. Text outside `content_boundary` in the Review/original view renders
with a greyed/struck class (e.g. `opacity-40 line-through decoration-gray-400`). The reading view
continues to render `final_text` (already in-bounds, so nothing to grey there).

### Confirm / adjust UI (`src/views/EntryDetailView.vue`)

When `content_boundary` is non-null:

- Render `raw_text` with the out-of-bounds regions greyed.
- Show paragraph-granular handles — "entry starts here ▲" / "entry ends here ▼" — at paragraph
  breaks, letting the user move the window if the model trimmed at the wrong line.
- A "use full page" reset clears the window (PATCH with nulls).
- Confirm/adjust issues the PATCH and surfaces the resulting re-processing jobs via the existing
  `jobsStore` tracking. Reuse `useEntryEditor`'s dirty/save scaffolding for state.

## Testing plan (failing test first, per the bug-fix workflow)

**Server (pytest):**
- `boundaries.py` unit tests across every role and shape: begins-only (FIRST), ends-only (LAST),
  both (ONLY), neither, multi-page first/middle/last, malformed/crossed markers (fall back to
  full text), empty page. Pure-Python, no model.
- Ingestion test with a fake OCR provider that emits role-tagged begin/end markers: asserts
  `content_start_char`/`content_end_char` are persisted and that `final_text`, chunks, and mood
  derive from the in-bounds slice (out-of-bounds text absent from chunks/FTS).
- Worker test: a single image with a trailing neighbor produces exactly **one** entry (regression
  guarding the removal of multi-entry fan-out); follow-up jobs use unsuffixed keys.
- API: `PATCH` with boundary offsets re-derives `final_text` and reruns the pipeline; serializer
  emits `content_boundary`; validation rejects out-of-range / partial offsets.

**Webapp (Vitest):**
- Overlay greys exactly the out-of-bounds segments and leaves in-bounds text plain.
- Confirm/adjust PATCHes the correct offsets; "use full page" clears them.
- Type/contract test that `content_boundary` round-trips through the store.

## Out of scope

- Filing multiple distinct entries from one upload (explicitly removed — see decision 4).
- Mid-page boundaries finer than the model's marker placement plus the user's paragraph-granular
  manual adjustment.
- Backfilling boundaries onto historical entries (they default to NULL = full text).

## Files touched (anticipated)

**server/**
- `src/journal/db/migrations/0033_entry_content_window.sql` (new)
- `src/journal/models.py` (`Entry` fields)
- `src/journal/providers/ocr.py` (`PageRole`, begin/end markers, prompt clauses, `extract`
  signature, both adapters)
- `src/journal/services/ingestion/boundaries.py` (new)
- `src/journal/services/ingestion/image.py` (unified `_ingest_pages`; remove fan-out + delimiter)
- `src/journal/services/jobs/workers/image_ingestion.py` (single-entry collapse)
- `src/journal/db/repository/` (persist/read the two columns)
- `src/journal/api/_shared.py` (`content_boundary` serialization)
- `src/journal/api/entries.py` (PATCH accepts + validates boundary, re-derive)
- `tests/` mirroring the above
- `journal/260603-ocr-multi-entry-segmentation.md` (note it is superseded) + a new journal entry

**webapp/**
- `src/types/entry.ts`, `src/api/entries.ts`
- `src/composables/useDiffHighlight.ts`
- `src/views/EntryDetailView.vue`
- `src/stores/entries.ts`
- corresponding Vitest specs
