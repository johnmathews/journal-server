# Storyline rename endpoint (PATCH /api/storylines/{id})

**Date:** 2026-06-11

## What

Added the ability to rename an existing storyline, so the webapp's
`/storylines/:id` view can let users update the title (it already supported
editing anchors). Previously `name` was set only at create time and was
immutable thereafter.

- `PATCH /api/storylines/{id}` — body `{name: str}`. Trims the name; rejects an
  empty result with 400. 200 with the updated storyline summary
  (`{id, name, anchors, ...}`); 404 if the storyline doesn't belong to the
  caller; 503 if storylines aren't wired. Metadata-only — it does **not** touch
  the stored panels or queue a regeneration, so curated/narrative text survives
  a rename.

## How / where

- `db/storyline_repository.py::update_storyline_name(storyline_id, name, user_id)`
  — `UPDATE ... SET name = ?, updated_at = now WHERE id = ? AND user_id = ?`.
  Returns the refreshed row, or `None` when `rowcount == 0` (wrong user / missing
  id), which the route maps to its already-handled 404.
- `api/storylines_write.py::update_storyline` — the new route lives beside the
  existing `DELETE` (same `/api/storylines/{id}` path, different method) in the
  write module, matching the precedent that all storyline mutations (POST, PUT
  anchors, DELETE) live there rather than in the read module.

The PATCH route follows the same in-body `parse_json="raw"` parsing pattern as the
sibling write routes so the 503/404 checks keep precedence over body-shape 400s.

## Tests

- `test_storyline_repository.py::TestStorylineCRUD` — `update_storyline_name`
  renames + trims + bumps `updated_at`; wrong-user returns `None` and leaves the
  name unchanged.
- `test_api_storylines_write.py::TestUpdateStoryline` — happy path (persisted via
  a follow-up GET), whitespace trim, no-job-kicked, empty/missing name 400,
  non-object body 400, unknown-id 404.

Full unit suite: 2592 passed. Docs updated in `docs/storylines.md` (also corrected
a stale `api/ingestion.py` reference to `api/storylines_write.py` in the same
section). Webapp side documented in
`journal-webapp/journal/260611-storyline-title-edit-and-browser-back.md`.
