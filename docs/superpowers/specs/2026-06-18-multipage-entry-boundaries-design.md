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
position context** and concatenates the results with `"\n".join(...)`. It never invokes the
`<<<NEW ENTRY>>>` splitting logic that the single-image path uses, so neighbor-entry text is
stored verbatim and pollutes the reading view, search, embeddings, and mood scoring.

The single-image path (`ingest_image` → `split_text_into_entries`) *does* trim, but by
**silently discarding** the orphan tail — no record, no recovery.

## Decisions (locked during brainstorming)

1. **Failure mode = keep everything, mark boundaries.** `raw_text` always stays 100% verbatim.
   Neighbor text is never deleted; it is marked out-of-bounds and greyed in the UI. Zero data loss.
2. **Boundary scope = exclude from everything but `raw_text`.** `final_text` (reading view),
   chunks/embeddings, FTS search, and mood are all derived from the **in-bounds slice** only.
   Out-of-bounds text is preserved but inert.
3. **Mechanism = role-aware OCR + deterministic Python trim.** The vision model is told each
   page's role and marks neighbor boundaries with the existing `<<<NEW ENTRY>>>` token; pure
   Python computes the offsets. The risky judgment (vision/layout) lives in the model; the
   irreversible cut is deterministic and unit-testable.
4. **Unify single-image and multi-image paths.** Single-image uploads switch to the same
   keep+mark model, replacing today's hard discard. One behavior to explain regardless of page count.
5. **Webapp v1 = adjust + reset.** Greyed out-of-bounds regions, paragraph-granular handles to
   move where the entry starts/ends, and a "use full page" reset. Persisted via PATCH.

## Core model: the content window

One new concept, modeled exactly like the existing uncertain spans
(`entry_uncertain_spans`, migration `0005`): a **content window**
`[content_start_char, content_end_char)` — a half-open character range into the entry's
`raw_text`.

- `raw_text` is verbatim and immutable (unchanged from today).
- `content = raw_text[content_start_char:content_end_char]`.
- `NULL` window (both columns NULL) = the whole text. Every existing entry, plus all voice/text
  entries, default to NULL → **zero backfill required**.
- All derived artifacts (`final_text`, chunks, embeddings, FTS, mood) come from `content`.
- The UI greys the regions of `raw_text` *outside* the window.

## Server design

### Storage — migration `0033`

Add two nullable columns to `entries`:

```sql
ALTER TABLE entries ADD COLUMN content_start_char INTEGER;
ALTER TABLE entries ADD COLUMN content_end_char   INTEGER;
```

Half-open, `raw_text` coordinates, same convention as `entry_uncertain_spans`
(`char_start` inclusive, `char_end` exclusive). NULL = full text.

Per the migration-testing rule: query prod for the actual shape of `entries` before finalizing,
and ensure the migration is safe to re-run after a partial failure. `Entry` dataclass
(`server/src/journal/models.py`) gains `content_start_char: int | None = None` and
`content_end_char: int | None = None`.

### OCR role plumbing — `providers/ocr.py`

Add a `PageRole` enum (`FIRST | MIDDLE | LAST | ONLY`) and an optional parameter to the OCR
protocol:

```python
def extract(self, image: bytes, media_type: str, page_role: PageRole | None = None) -> OCRResult: ...
```

`page_role=None` reproduces the **current** prompt exactly (backward compatible). Both adapters
(Anthropic, Gemini) append a role-specific clause to the existing `SYSTEM_PROMPT`, reusing the
existing `<<<NEW ENTRY>>>` marker (no new marker token):

- **FIRST** — "This is the first page of an entry that may continue onto later pages. It may begin
  with the tail of a previous, already-recorded entry sitting above this entry's date heading. If
  so, emit `<<<NEW ENTRY>>>` on its own line immediately before *this* entry's date heading. Emit
  no other markers."
- **MIDDLE** — "This is a middle page of a single ongoing entry — a pure continuation. Do NOT emit
  any `<<<NEW ENTRY>>>` markers."
- **LAST** — "This is the last page of the entry; the entry ends on this page. If a *new* entry
  begins below where it ends (e.g. a fresh date heading), emit `<<<NEW ENTRY>>>` on its own line
  immediately before that new entry's date heading. Emit at most one marker."
- **ONLY** — current single-image semantics: the page may contain both a leading previous-entry
  tail and a trailing next-entry start.

The marker is a control token, never stored in `raw_text`.

### Deterministic boundary computation — new module `services/ingestion/boundaries.py`

Pure function, no model calls, fully unit-testable. Input: the per-page OCR texts paired with
their roles. Output: `(combined_text, content_start, content_end)` where `combined_text` has all
markers stripped and the offsets index into it.

Per-page rules:

- **FIRST page:** if it contains ≥1 marker, `content_start` = position just after the **last**
  marker on that page (drop the previous-entry tail above it). The role-FIRST prompt emits the
  marker immediately before this entry's date heading, so the last marker is the boundary closest
  to real content — robust even if the model over-marks.
- **LAST page:** if it contains ≥1 marker, `content_end` = position just before the **first**
  marker on that page (drop the next-entry start below it).
- **MIDDLE page:** markers ignored (none expected).
- **ONLY page (single image):** apply both the FIRST rule (leading tail) and the LAST rule
  (trailing next-entry) to the one page. Pathological "three entries crammed on one image" is an
  accepted edge: content = text between the first and last marker; documented, not specially handled.

Page combination still joins stripped page texts with a single `"\n"` (the existing
chunking-budget rationale in `image.py` is preserved). Marker removal is folded into the existing
`_strip_and_shift_page_spans` step so uncertain-span offsets stay anchored to the final
marker-stripped `raw_text`.

### Wiring — `services/ingestion/image.py`

- `ingest_multi_page_entry`: assign each page a role (`FIRST`/`MIDDLE`/`LAST` for N≥2; `ONLY` for
  N=1), pass it to `extract`, call the boundary module, persist `content_start_char` /
  `content_end_char` on the entry, and run heading detection + `_process_text` on
  `raw_text[content_start:content_end]` rather than the full combined text.
- `ingest_image` (single-image): pass `ONLY`, compute the window via the same boundary module, and
  **store the window instead of discarding** the tail. `split_text_into_entries`'s discard behavior
  is replaced by keep+mark. (Note: the multi-entry-per-image fan-out, where one image legitimately
  contains several distinct entries to be filed separately, is out of scope here and retains its
  existing split semantics if still needed — to be confirmed during planning by auditing callers
  of `split_text_into_entries`.)

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
- `boundaries.py` unit tests across every role and shape: tail-above (FIRST), next-below (LAST),
  both (ONLY), none, multi-marker over-marking, no-marker, empty page. Pure-Python, no model.
- Ingestion test with a fake OCR provider that emits role-tagged `<<<NEW ENTRY>>>` markers:
  asserts `content_start_char`/`content_end_char` are persisted and that `final_text`, chunks, and
  mood derive from the in-bounds slice (out-of-bounds text absent from chunks/FTS).
- Single-image regression: tail above the date is now **kept + windowed**, not discarded.
- API: `PATCH` with boundary offsets re-derives `final_text` and reruns the pipeline; serializer
  emits `content_boundary`; validation rejects out-of-range / partial offsets.

**Webapp (Vitest):**
- Overlay greys exactly the out-of-bounds segments and leaves in-bounds text plain.
- Confirm/adjust PATCHes the correct offsets; "use full page" clears them.
- Type/contract test that `content_boundary` round-trips through the store.

## Out of scope

- Detecting multiple *distinct* entries in one upload and filing them separately (the existing
  multi-entry fan-out, if retained, is untouched).
- Mid-page boundaries finer than the model's marker placement plus the user's paragraph-granular
  manual adjustment.
- Backfilling boundaries onto historical entries (they default to NULL = full text).

## Files touched (anticipated)

**server/**
- `src/journal/db/migrations/0033_entry_content_window.sql` (new)
- `src/journal/models.py` (`Entry` fields)
- `src/journal/providers/ocr.py` (`PageRole`, prompt clauses, `extract` signature, both adapters)
- `src/journal/services/ingestion/boundaries.py` (new)
- `src/journal/services/ingestion/image.py` (role assignment, wiring, single-image unification)
- `src/journal/db/repository/` (persist/read the two columns)
- `src/journal/api/_shared.py` (`content_boundary` serialization)
- `src/journal/api/entries.py` (PATCH accepts + validates boundary, re-derive)
- `tests/` mirroring the above

**webapp/**
- `src/types/entry.ts`, `src/api/entries.ts`
- `src/composables/useDiffHighlight.ts`
- `src/views/EntryDetailView.vue`
- `src/stores/entries.ts`
- corresponding Vitest specs
