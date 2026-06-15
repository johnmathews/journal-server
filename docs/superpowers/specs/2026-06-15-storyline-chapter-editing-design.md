# 1. Storyline chapter editing — Design

**Date:** 2026-06-15
**Status:** active
**Builds on:** [2026-06-13 Storyline Chapters design](2026-06-13-storyline-chapters-design.md)
(Phase 1 — schema, per-chapter generation, chapter rail — is shipped; prod, local,
and `main` are all at `user_version = 30`).

**Scope of this spec:** **Phase A** — manual, deterministic chapter editing
(move boundary, split, merge, add, delete) with automatic per-chapter
regeneration, exposed over discrete REST + MCP endpoints and an inline
chapter-rail UI. **Phase B** — the LLM suggestion engine (`/chapters/suggest`),
the draggable timeline editor, and the "time to cut a chapter" nudge — is
recorded here for context (section 8) but is **not** in scope for this spec's
implementation plan.

Cross-cutting: touches both `server/` (repository, generation wiring, API, MCP)
and `webapp/` (detail view, store, types). Expect two commits, one per repo.

## 1.1 Relationship to the prior spec

The 2026-06-13 spec deferred all editing to a Phase 2 built around an LLM
suggestion engine and a single `PUT /api/storylines/{id}/chapters` "commit an
edited set" endpoint, and it explicitly listed manual split/merge as out of
scope. This spec supersedes that framing for the **manual** layer: it makes
direct, user-driven editing the foundation (Phase A) and re-homes the LLM
suggestion work as a later layer (Phase B) that sits on top of these endpoints.
The data model from the prior spec is unchanged.

# 2. Problem

After Phase 1, a storyline is a rail of one or more chapters, but the chapters
are effectively fixed: a user can rename and regenerate a chapter, but cannot
change its date window, divide an over-long chapter, combine two that belong
together, or introduce a chapter over a chosen period. The date window is set
only at create/migration time. The user wants direct control over the chapter
timeline: reshape, split, merge, and add chapters by hand.

# 3. Locked decisions

Settled during brainstorming; the fixed premises of this design:

1. **Build manual first, suggestions later.** Phase A delivers deterministic
   manual operations; the LLM suggestion engine + timeline editor (Phase B) layer
   on top afterward.
2. **Book-like timeline by default, with a manual override.** Chapters tile the
   storyline span with no gaps and no overlaps, `seq` order matching date order.
   An explicit `allow_gap` override lets the user intentionally leave an
   uncovered range. **Overlaps are always rejected** (an entry in two windows
   would be cited in both chapters).
3. **Auto-regenerate affected chapters.** Every structural edit immediately kicks
   the existing per-chapter generation job for each chapter whose window changed;
   the UI shows them as "generating…" until fresh. No stale state is persisted.
4. **Exactly one open chapter, always the latest.** The open chapter (NULL
   `end_date`) is the live, forward-growing chapter. Every operation preserves
   this invariant.
5. **Inline chapter-rail UI.** Editing controls live on the existing left chapter
   rail (a per-chapter `⋯` menu plus an `+ Add chapter` button) with small
   modals — no separate management screen in Phase A.
6. **Discrete REST endpoints**, not a single diff-style `PUT`. Each operation is
   its own endpoint, mirrored by an MCP tool.

# 4. Data model

**No schema change.** Migration 0030's `storyline_chapters`
(`id`, `storyline_id`, `seq`, `title`, `start_date`, `end_date`, `state`,
`last_generated_at`, `summary_embedding_json`, timestamps) already carries
everything Phase A needs. There is **no migration 0031**.

## 4.1 Invariants enforced by every operation

- `seq` is 1-based and contiguous within a storyline; `seq` order equals
  date order.
- Exactly one chapter has `state = 'open'` (enforced by the existing partial
  unique index); it is the highest-`seq` chapter and has `end_date IS NULL`.
- Closed chapters have both `start_date` and `end_date` set.
- Dates are inclusive `YYYY-MM-DD` days. Adjacent closed chapters are contiguous
  when `chapter[n].end_date == chapter[n+1].start_date - 1 day`.
- By default the chapter set is gapless and non-overlapping. `allow_gap = true`
  on an edit/delete permits a gap; overlaps are rejected unconditionally.

## 4.2 Re-sequencing

Split and "add in the middle" shift later chapters' `seq` up by one; merge and
delete shift the tail down. Because `UNIQUE(storyline_id, seq)` would collide
during an in-place shift, re-sequencing runs inside a single transaction using a
temporary negative-offset pass (write the new seqs as `-(seq)` then flip to
positive), or an equivalent collision-free reorder. All multi-row mutations are
transactional so a partial failure leaves the storyline consistent.

# 5. Operation semantics

All five operations resolve the chapter's storyline (for `user_id` ownership and
anchors), mutate rows transactionally, then enqueue generation jobs for every
affected chapter. The boundary convention throughout: a "cut date" `D` becomes
the **start** of the later chapter; the earlier chapter's `end_date` becomes the
day before `D`.

## 5.1 Move boundary — `PATCH` dates

Change a chapter's `start_date` and/or `end_date`. The shared edge of the
adjacent neighbor ripples to stay contiguous (moving Ch2's start also moves Ch1's
end, and vice versa). The open chapter's `end_date` cannot be set — it stays
NULL; only its start may move. With `allow_gap = true`, the edited boundary
detaches from the neighbor and a gap is allowed. Overlaps are rejected.
Affected chapters (the edited one and any rippled neighbor) regenerate.

## 5.2 Split — `POST …/chapters/{cid}/split`

Body `{date}`, where `date` is inside the chapter's window. Produces two
contiguous chapters: left `[start, date-1]`, right `[date, end]`. If the source
chapter was open, the **right** half stays open and the left becomes closed;
otherwise both are closed. Later chapters shift `seq` up by one. Both halves
regenerate.

## 5.3 Merge — `POST …/chapters/merge`

Body `{chapter_ids}` — two or more **adjacent** chapters (validated as a
contiguous `seq` run). Produces one chapter spanning the union of their windows,
keeping the lowest `seq` and (by default) the earliest chapter's title. The
result is **open** if any input was open, else closed. Tail chapters shift `seq`
down. The merged chapter regenerates once over the combined window.

## 5.4 Add — `POST …/chapters`

Two flavors selected by body shape:

- **Start a new latest chapter** `{start_date}` (the common case): closes the
  current open chapter at `start_date - 1` and opens a fresh chapter
  `[start_date, NULL)` as the new highest `seq`. This is the manual equivalent of
  Phase B's "suggest a cut."
- **Add over a range** `{start_date, end_date}`: inserts a closed chapter over a
  currently-uncovered range (requires `allow_gap` semantics — the range must not
  overlap an existing chapter). `seq` is assigned by date order; later chapters
  shift up. The new chapter regenerates.

## 5.5 Delete — `DELETE …/chapters/{cid}`

Removes a chapter; by default its date range is absorbed by the previous neighbor
(its `end_date` extends to cover the deleted range) so the timeline stays
gapless. With `allow_gap = true`, the range is left empty instead. Deleting the
open chapter promotes the previous chapter to open (`end_date → NULL`). Deleting
the only chapter is rejected (a storyline always has at least one chapter). The
neighbor that absorbed the range regenerates; if a gap was left, nothing
regenerates.

## 5.6 Validation summary

Each endpoint returns `400` with an actionable message on: overlap, gap without
`allow_gap`, non-adjacent merge set, split date outside the window, attempting to
set an end on the open chapter, or deleting the last chapter.

# 6. API surface

Chapters are nested under a storyline; routes live in
`src/journal/api/storylines_write.py`. Every mutation returns the affected
chapter summary/summaries plus the enqueued generation `job_id`s.

| route | method | purpose |
|-------|--------|---------|
| `/api/storylines/{id}/chapters` | POST | add (new-latest `{start_date}` or ranged `{start_date,end_date}`) |
| `/api/storylines/{id}/chapters/{cid}/split` | POST | `{date}` → split into two |
| `/api/storylines/{id}/chapters/merge` | POST | `{chapter_ids}` → merge adjacent |
| `/api/storylines/{id}/chapters/{cid}` | PATCH | extend existing rename route to also accept `{start_date?, end_date?, allow_gap?}` |
| `/api/storylines/{id}/chapters/{cid}` | DELETE | `{allow_gap?}` → delete |

The existing `GET /api/storylines/{id}` (chapter summaries) and
`GET /api/storylines/{id}/chapters/{cid}` (panels) are unchanged and reflect the
new structure automatically. The storyline-level `regenerate` route is untouched.

## 6.1 MCP parity

Mirror each mutation as an MCP tool in
`src/journal/mcp_server/tools/storylines.py`, following the existing annotation
conventions (`destructiveHint` on delete; the others are write tools):
`journal_add_storyline_chapter`, `journal_split_storyline_chapter`,
`journal_merge_storyline_chapters`, `journal_update_storyline_chapter`
(dates + rename), `journal_delete_storyline_chapter`.

# 7. Implementation

## 7.1 Repository (`db/storyline_repository.py`)

Add transactional methods alongside the Phase 1 chapter CRUD:
`split_chapter(chapter_id, date)`, `merge_chapters(chapter_ids)`,
`add_chapter(storyline_id, start_date, end_date=None)`,
`update_chapter_window(chapter_id, start_date, end_date, allow_gap)`,
`delete_chapter(chapter_id, allow_gap)`. Each returns the affected
`StorylineChapter` row(s) and performs the re-sequencing from 4.2 in one
transaction. A private `_validate_invariants(storyline_id)` helper (or
equivalent guard inside each method) enforces section 4.1.

## 7.2 Generation wiring

No change to `StorylineGenerationService` — it already regenerates by
`chapter_id` over the chapter's window. The API layer enqueues
`run_storyline_generation` jobs (one per affected chapter) after the repository
mutation commits, reusing `mode="replace"` for closed chapters and `"append"`
for the open chapter, exactly as the existing per-chapter regenerate route does.
Where an operation affects multiple chapters (split, ripple), jobs may share a
`parent_job_id` for UI consolidation.

## 7.3 Webapp

- **`types/storyline.ts`:** request types for each operation.
- **`api/storylines.ts`:** client functions for the five endpoints.
- **`stores/storylines.ts`:** actions `addChapter`, `splitChapter`,
  `mergeChapters`, `updateChapterDates`, `deleteChapter`; each applies the
  returned structure, marks affected chapters generating, and refreshes when
  jobs complete (reusing the existing job-polling the regenerate flow uses).
- **`views/StorylineDetailView.vue` + components:** a per-rail-item `⋯` menu
  (Edit dates / Split here / Merge with next / Delete) and a top-of-rail
  `+ Add chapter` button. Small modals: a date picker for edit/add/split; a
  confirm for merge/delete. Affected chapters render a "generating…" state until
  their jobs finish. Single-chapter storylines keep working unchanged.

# 8. Phase B (recorded for context, not in scope)

Built on Phase A's endpoints:
`POST /api/storylines/{id}/chapters/suggest` (heuristic + LLM proposes
boundaries + titles, persists nothing), an optional
`PUT /api/storylines/{id}/chapters` batch-commit/diff endpoint, the
`ChapterTimelineEditor` (density timeline + draggable cuts), the
extension-classifier → "suggest a cut" nudge on the open chapter.

# 9. Testing & docs

## 9.1 Server (pytest, in-memory SQLite, unit tier stays green)

- Repository: re-sequencing correctness; the one-open-chapter invariant across
  every operation; contiguity maintained by default; overlap rejection; gap
  permitted only with `allow_gap`; transactional rollback on invalid input;
  deleting the last chapter rejected.
- Per-operation behavior: split open vs closed (open-ness lands on the right
  half); merge open-ness propagation; add new-latest closes the prior open
  chapter; delete absorbs into the previous neighbor (and leaves a gap with
  override).
- API route tests for each endpoint, including the validation cases in 5.6, and
  that jobs are enqueued for the right chapters. MCP tool tests for parity.

## 9.2 Webapp (Vitest, coverage ≥85% statements/branches/functions/lines)

Store actions (each operation updates state + triggers generating state) and the
rail menu / modal components (open the right modal, submit the right payload,
render the generating state).

## 9.3 Docs & journal

Update `server/docs/` and `webapp/docs/` storyline pages and the API-contract doc
for the new endpoints; dated journal entries (`YYMMDD-name.md`) in
`server/journal/` and `webapp/journal/`.

# 10. Out of scope (this spec)

- The Phase B suggestion engine, timeline editor, and nudge banner (section 8).
- Per-chapter anchors (anchors stay storyline-level, per the prior spec).
- Overlapping chapter windows (always rejected).
- Any schema change — Phase A is purely additive on migration 0030.
