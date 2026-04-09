# 260409 - Multi-Page Entries and REST API

## Summary

Added multi-page entry support, OCR correction workflow, and a REST API to the journal server. This lays the groundwork for a web frontend that can browse, view, and correct journal entries.

## Changes

### Multi-Page Entry Support
- New `entry_pages` table stores per-page `raw_text` with page ordering and source file reference.
- `journal_ingest_multi_page` MCP tool accepts multiple image URLs and combines them into a single entry.
- Adding pages to an existing entry re-concatenates text and triggers re-chunking/re-embedding.

### OCR Correction (raw_text vs final_text)
- Entries now have two text fields: `raw_text` (immutable OCR/transcription output) and `final_text` (editable copy used by all downstream features).
- Editing `final_text` triggers: delete old ChromaDB chunks, re-chunk, re-embed, store new chunks. FTS5 trigger auto-rebuilds the index.
- `journal_update_entry_text` MCP tool and `PATCH /api/entries/{id}` REST endpoint both use this flow.

### REST API
- 4 endpoints registered via `mcp.custom_route()` on the same port as the MCP server:
  - `GET /api/entries` -- paginated list with date filtering
  - `GET /api/entries/{id}` -- full entry detail
  - `PATCH /api/entries/{id}` -- update final_text (triggers re-embedding)
  - `GET /api/stats` -- journal statistics
- CORS support via `API_CORS_ORIGINS` environment variable.

### Schema Migration
- Migration 0002 adds `final_text` and `chunk_count` columns to `entries`, creates `entry_pages` table, and backfills `final_text` from `raw_text` for existing entries.

### MCP and CLI Updates
- New tools: `journal_ingest_multi_page`, `journal_update_entry_text`.
- CLI updated with corresponding commands.

## Key Design Decisions

- **`custom_route` over FastAPI**: The REST API uses `mcp.custom_route()` to share the same ASGI server and port as the MCP protocol. This avoids running a second server or adding FastAPI as a dependency.
- **`final_text` on the entries table**: Keeping `final_text` directly on `entries` (rather than a separate edits table) simplifies queries and avoids joins for the common read path. The tradeoff is that edit history is not preserved beyond the original `raw_text`.
- **Denormalized `chunk_count`**: Stored on `entries` to avoid counting ChromaDB documents on every list query. Updated during ingestion and re-chunking.
- **`entry_pages` for multi-page**: Per-page `raw_text` is stored separately so individual pages can be re-OCR'd or inspected without re-processing the entire entry.
