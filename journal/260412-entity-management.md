# Entity management: merge, rename, delete, merge review

**Date:** 2026-04-12
**Scope:** journal-server + journal-webapp (cross-cutting)
**Roadmap item:** Tier 2 item 8

## What shipped

Four entity management features, end-to-end:

### 1. Merge entities

Select 2+ entities in the list view, click "Merge selected", pick the survivor
in a modal. All mentions, relationships, and aliases from absorbed entities are
reassigned to the survivor. The absorbed entity's canonical name becomes an alias
on the survivor.

Merge history is recorded in `entity_merge_history` (migration 0008) so merges
can be audited or undone in future. Each row snapshots the absorbed entity's
name, type, description, and aliases at the time of merge.

### 2. Rename / edit

Edit button on entity detail view opens an inline form for canonical name,
entity type (dropdown), and description. `PATCH /api/entities/{id}`.

### 3. Delete

Delete button on entity detail view with `window.confirm()` dialog.
`DELETE /api/entities/{id}` cascades to mentions, relationships, and aliases
via foreign key constraints.

### 4. Merge review

The extraction service's stage-c (embedding similarity) now persists near-miss
matches (below the merge threshold but above threshold - 0.15) to
`entity_merge_candidates`. The entity list view shows a "Possible duplicates to
review" banner with a count badge. Each candidate can be accepted (triggers a
merge) or dismissed.

## Backend changes

- **Migration 0008:** `entity_merge_history` and `entity_merge_candidates` tables
- **EntityStore Protocol:** 7 new methods — `update_entity`, `delete_entity`,
  `merge_entities`, `create_merge_candidate`, `list_merge_candidates`,
  `resolve_merge_candidate`, `get_merge_history`
- **SQLiteEntityStore:** Full implementations for all 7 methods
- **REST API:** 6 new endpoints — PATCH/DELETE entities, POST merge,
  GET/PATCH merge-candidates, GET merge-history
- **Extraction service:** Updated `_resolve_entity` to return near-miss info
  as a 4th tuple element; caller persists merge candidates via
  `contextlib.suppress` (graceful fallback if table doesn't exist)
- **40 new tests** (724 total)

## Frontend changes

- **Types:** `EntityUpdateRequest`, `EntityMergeRequest/Response`,
  `MergeCandidate`, `MergeCandidatesResponse`, `MergeHistoryEntry/Response`
- **API client:** 6 new functions
- **Pinia store:** `updateCurrentEntity`, `removeEntity`, `mergeEntities`,
  merge candidate load/accept/dismiss
- **EntityDetailView:** Edit button + inline form, delete button + confirm
- **EntityListView:** Row checkboxes, selection toolbar, merge modal with
  survivor picker, merge review section
- **14 new tests** (462 total)

## Bug fixes

Two tuple-unpack bugs discovered during evaluation:
- `GET /api/entities?search=...` crashed — search filter unpacked 2 values from
  a 3-tuple (`list_entities_with_mention_counts` returns `(Entity, int, str)`)
- MCP `journal_list_entities` had the same bug

Both were present since the `last_seen` field was added to
`list_entities_with_mention_counts` but the callers weren't updated.

## Design decisions

- **Merge history over undo:** Chose to snapshot absorbed entities in a history
  table rather than implementing split/undo. History is cheap and provides audit
  trail. Full undo can be built on top later if needed.
- **Near-miss threshold:** `max(threshold - 0.15, 0.5)` for merge candidates.
  With default threshold 0.88, this means entities with 0.73-0.87 similarity
  are flagged for review. Wide enough to catch real duplicates, narrow enough
  to avoid noise.
- **Merge candidates persist across sessions:** Unlike the old in-memory
  warnings that disappeared after the extraction API call, candidates are now
  in SQLite and survive restarts.
- **Auto-resolve on merge:** When entity A is merged into B, any pending merge
  candidates involving A are automatically marked as 'accepted'.
