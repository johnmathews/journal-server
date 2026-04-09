# First Live Ingestion & Bug Fixes

First journal entry successfully ingested via the Slack -> Nanoclaw -> MCP pipeline on 2026-04-09.
A photo of a handwritten page (dated 2026-02-15) was sent in Slack, and the full pipeline ran:
OCR (11s) -> SQLite storage -> embedding (2s) -> ChromaDB. 842 characters, 160 words, 1 chunk.

## Bugs Fixed

### Duplicate log lines (per-session handler accumulation)

The streamable-HTTP transport creates a new `lifespan` context per MCP client session, not once at
server startup. `setup_logging()` was called inside `lifespan()`, adding a new stderr handler each
time. After 5 sessions, every log message was repeated 14 times.

Fix: Made `setup_logging()` idempotent (early return if handlers exist). Guarded service
initialization in `lifespan()` with a module-level `_services` singleton so DB connections,
migration checks, and ChromaDB connections happen once and are reused across sessions.

### Per-session re-initialization

Same root cause as above. Every new session reconnected to SQLite, re-ran migration checks, and
reconnected to ChromaDB. Fixed by the same singleton guard.

## Improvements

### Tool call logging

Added `log.info("Tool call: <name>(...)")` to all 7 MCP tool functions. Previously, logs only
showed `Processing request of type CallToolRequest` (from the SDK) with no indication of which
tool was called or with what parameters.

### Smaller chunk sizes

Reduced defaults from 500/100 to 150/40 (max_tokens/overlap_tokens). A typical A5 handwritten
page is ~150-250 words (~200-330 tokens), so the old 500-token limit meant every page was a single
chunk. Semantic search couldn't distinguish between different topics on the same page. With 150
tokens, pages split into 2-3 chunks at paragraph boundaries, improving search precision for
topic-level retrieval.

Note: existing entry 1 retains its original single chunk. Only new entries use the new sizes.

## Documentation

Updated deployment docs to reflect actual workflow (CI pushes to ghcr.io, manually pulled on
media VM). Added primary usage description (Slack -> Nanoclaw -> MCP flow). Removed misleading
Ansible reference from docker-compose.yml.
