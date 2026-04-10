# DELETE /api/entries/{id}

Added a DELETE endpoint so the webapp can remove journal entries end-to-end.
The webapp side of this work lives in journal-webapp under the same date.

## Layering

The delete flow mirrors the existing update flow: repository → service → HTTP.

1. `SQLiteEntryRepository.delete_entry(entry_id) -> bool` — issues
   `DELETE FROM entries WHERE id = ?`, commits, and reports whether a row was
   actually removed via `cursor.rowcount`. All related tables
   (`entry_pages`, `entry_people`, `entry_places`, `entry_tags`, `mood_scores`,
   `source_files`) already carry `ON DELETE CASCADE` foreign keys from the
   initial migration, and FTS5 cleanup is handled by the pre-existing
   `entries_ad` AFTER DELETE trigger added in the correction migration. So
   the SQL is genuinely one statement — no orchestration needed on the
   structured side.

2. `IngestionService.delete_entry(entry_id) -> bool` — fetches the entry first
   so we can return a meaningful `False` for 404s, then purges the entry's
   chunks from ChromaDB **before** deleting the SQLite row. Ordering matters:
   if the vector store call blows up, the SQLite row is still present and
   the caller gets a 500. If we did it the other way round and SQLite
   succeeded but ChromaDB failed, we'd leave orphaned vectors that the
   query service would still surface in semantic search — silently wrong.

3. `DELETE /api/entries/{entry_id}` — wired into the existing
   `entry_detail` route by adding `"DELETE"` to its methods list and
   dispatching to a new `_delete_entry` helper. Response shape is
   `{"deleted": true, "id": <n>}` on success, `{"error": "..."}` with 404
   if the service returned `False`.

## Tests

Four new tests in `tests/test_api.py::TestDeleteEntry`:

- happy path: returns 200, SQLite row is gone, `mock_vector_store.delete_entry`
  was called exactly once with the entry id
- 404 path: missing id, no vector store call
- cascade: entry with two `entry_pages` — after delete, `get_entry_pages(id)` is empty
- list view reflects the deletion: create 3 entries, delete one, list returns 2

All 237 server tests still pass; ruff clean.

## Docs

- `docs/api.md`: added a "DELETE /api/entries/{id}" section after the PATCH
  endpoint with request/response examples and the 404 shape
- `docs/architecture.md`: added a "Deletion" data-flow block alongside the
  existing "OCR Correction" block, making the vector-store-first ordering
  visible to future readers

## What this does not do

- No soft delete / undo. The row is gone immediately. Handwritten entries
  are backed by the scanned image on disk, so re-ingestion is always an
  option if someone deletes the wrong thing — but there's no in-app undo.
- No bulk delete endpoint. Not needed yet; one request per entry is fine
  for the volumes this tool handles.
- No audit log. If a "when/why was this deleted" question ever becomes
  interesting, `deleted_at` + a soft-delete flag would be the cheap next
  step.
