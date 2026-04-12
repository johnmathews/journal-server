# Entry Creation API + Webapp UI

**Date:** 2026-04-12
**Scope:** journal-server + journal-webapp (cross-cutting)

## What shipped

Three new ways to create journal entries from the webapp, where previously the
only creation path was the CLI:

### Server — three REST endpoints

1. **POST /api/entries/ingest/text** — accepts JSON `{text, entry_date?, source_type?}`,
   creates entry synchronously (chunk + embed, no OCR). Returns 201 with entry + optional
   `mood_job_id`.

2. **POST /api/entries/ingest/file** — accepts multipart `.md`/`.txt` file upload. Reads
   content, stores as text entry with `source_type="import"`. Preserves original filename
   and SHA256 hash in `source_files` for dedup. Returns 201.

3. **POST /api/entries/ingest/images** — accepts multipart image upload (JPEG, PNG, GIF,
   WebP). Queues an async OCR job via `JobRunner`. Returns 202 + `{job_id, status}`.
   Job runs OCR per page, combines text, chunks, embeds, stores. Progress reported as
   page N of M.

### Server — supporting infrastructure

- **`IngestionService.ingest_text()`** — new method for text/file entries. No OCR provider
  needed. Accepts `skip_mood` to let API handlers defer scoring.
- **`skip_mood` on `_process_text`** — when True, skips inline mood scoring. The API
  endpoints pass `skip_mood=True` then submit a `mood_score_entry` job for async scoring.
- **`on_progress` callback on `ingest_multi_page_entry`** — fires after each page OCR
  completes, feeding the job progress bar.
- **Two new job types in JobRunner:**
  - `ingest_images` — full image ingestion pipeline on background thread
  - `mood_score_entry` — scores a single entry's mood dimensions
- **`python-multipart` dependency** — for Starlette form parsing
- **`api_utils.py`** — `parse_multipart_request` helper wrapping Starlette's form API
- **Migration 0007** — recreates `entries` table to remove the `CHECK(source_type IN
  ('ocr', 'voice'))` constraint. Preserves FTS on `final_text`, all triggers including
  the entity stale-flag trigger from migration 0004.

### Webapp — `/entries/new` view

- **CreateEntryView** — three-tab interface with shared date picker (defaults to today,
  editable)
- **TextEntryPanel** — textarea with live word count, "Create Entry" button
- **FileImportPanel** — drag-drop zone for `.md`/`.txt`, content preview panel
- **ImageUploadPanel** — drag-drop for images, thumbnail previews, up/down reorder buttons,
  remove button, progress bar with job polling, auto-navigate to new entry on completion
- **Sidebar** — "New Entry" link with + icon
- **EntryListView** — "New Entry" button in header
- **Router** — `/entries/new` placed before `/entries/:id`

## Key decisions

1. **Sync for text, async for images.** Text entry is fast (~1s for embed), images are slow
   (~10-20s for OCR). Different HTTP patterns match the UX expectations.
2. **Mood scoring deferred to background job.** Avoids blocking the 201 response with an
   LLM call. Image jobs run mood inline since they're already async.
3. **Up/down arrows for reorder instead of drag-to-reorder.** Native HTML5 DnD doesn't work
   on touch devices. SortableJS was planned but arrows shipped first as the simpler path.
   Can upgrade to SortableJS later if needed.
4. **No markdown stripping for file import.** Markdown is preserved as-is. The chunker and
   embedding model handle it fine.

## Test coverage

- Server: 24 new tests (7 service, 17 API), 654 total passing
- Webapp: 47 new tests (API client + 3 component spec files), 413 total passing

## What's next

- Dashboard mood chart (3b frontend — backend fully shipped, just needs the Chart.js view)
- First entity extraction run (item 1 — unblocks all of Tier 2)
- Could upgrade image reorder to SortableJS for touch/drag support
