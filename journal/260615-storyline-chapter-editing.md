# 260615 — Storyline chapter editing (Phase A)

## What shipped

Phase A of manual storyline chapter editing. Users can now directly reshape the
chapter timeline of any storyline — split an over-long chapter, merge two that
belong together, add a new chapter at the front of the timeline, move a date
boundary, or delete a chapter — without waiting for an LLM suggestion engine
(Phase B, deferred).

### API surface added (server-side)

Five new REST endpoints in `src/journal/api/storylines_write.py`:

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/storylines/{id}/chapters` | Add a chapter (new-latest or closed ranged) |
| POST | `/api/storylines/{id}/chapters/{cid}/split` | Split at a date |
| POST | `/api/storylines/{id}/chapters/merge` | Merge adjacent chapters |
| PATCH | `/api/storylines/{id}/chapters/{cid}` | Rename and/or move window |
| DELETE | `/api/storylines/{id}/chapters/{cid}` | Delete, absorbing or gapping |

Five mirroring MCP tools in `src/journal/mcp_server/tools/storylines.py`:
`journal_add_storyline_chapter`, `journal_split_storyline_chapter`,
`journal_merge_storyline_chapters`, `journal_update_storyline_chapter`,
`journal_delete_storyline_chapter`.

Repository ops in `src/journal/db/storyline_repository.py`:
`add_chapter`, `split_chapter`, `merge_chapters`, `update_chapter_window`,
`delete_chapter`.

No schema change — built on migration 0030's `storyline_chapters` table.

## Key design decisions

**1. Manual first, suggestions later.**
The LLM boundary-suggestion engine (Phase B) layers on top of these endpoints.
Phase A delivers pure deterministic operations that are useful immediately and
provide the foundation Phase B needs. See the
[design spec](../docs/superpowers/specs/2026-06-15-storyline-chapter-editing-design.md)
and [implementation plan](../docs/superpowers/plans/2026-06-15-storyline-chapter-editing.md).

**2. Book-like timeline by default; `allow_gap` override.**
Chapters tile the storyline span with no gaps and no overlaps by default: editing
one boundary ripples the touching neighbor automatically. Overlaps are always
rejected (an entry in two windows would be cited in both chapters, corrupting
provenance). Users who intentionally want a gap — a storyline that only covers
certain periods — pass `allow_gap=true`.

**3. Exactly one open chapter, always the latest.**
The open chapter (NULL `end_date`) is the live, forward-growing chapter. Every
operation preserves this invariant: a split on the open chapter keeps the right
half open; a merge that includes the open chapter produces an open result;
deleting the open chapter promotes the previous neighbor to open.

**4. Auto-regeneration of affected chapters.**
Every structural edit immediately enqueues the existing `run_storyline_generation`
job for each chapter whose window changed. No stale content is persisted — the
chapters show as "generating…" until fresh. The generation service itself is
unchanged; only the API layer decides which chapters to enqueue.

**5. Discrete REST endpoints, not a single diff-style PUT.**
Each operation is its own endpoint with a narrow contract. This keeps validation
logic local, makes error messages actionable, and lets clients express intent
explicitly rather than diffing two full chapter lists.

**6. PATCH handles both rename and window edit.**
`PATCH /api/storylines/{id}/chapters/{cid}` is overloaded: a body with only
`title` is a metadata-only rename (no regeneration, returns a flat chapter dict).
A body with any date field triggers the window-edit path (ripples neighbors,
returns `{chapters, job_ids}`). Both fields may be combined. This preserves
back-compat for the existing rename route while adding the new capability.

## Re-sequencing

Split and "add in the middle" shift later chapters' `seq` up by one; merge and
delete shift the tail down. Because `UNIQUE(storyline_id, seq)` would collide
during an in-place shift, re-sequencing runs inside a single transaction with a
temporary negative-offset pass (write new seqs as negatives, then flip to
positive). All multi-row mutations are transactional.

## Tests

- `tests/test_storyline_repository.py` — extended with chapter-editing ops:
  split (open vs closed source), merge (open-ness propagation, contiguity check),
  add (new-latest closes former open, ranged inserts into gap), update_chapter_window
  (ripple, allow_gap, overlap rejection), delete (absorb, allow_gap, last-chapter
  rejection).
- `tests/test_api_storylines_write.py` — one test class per new endpoint covering
  happy paths, validation cases, and 404/503 guards.
- `tests/test_mcp_tools_storylines.py` — one test class per new tool covering
  success, not-found, and validation paths.
