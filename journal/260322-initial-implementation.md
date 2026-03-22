# Initial Implementation

**Date:** 2026-03-22

## What Was Done

Built the complete v0.1 of the Journal Analysis Tool from a project brief. The system ingests handwritten journal pages (OCR) and voice notes (transcription), stores them in dual databases, and answers natural language queries.

## Architecture Decisions

### Dual Storage (SQLite + ChromaDB)
SQLite handles structured/quantitative queries (statistics, date lookups, word counts) and keyword search via FTS5. ChromaDB handles semantic similarity search. FTS5 was added to complement vector search for exact keyword frequency queries ("how often do I mention Vienna?") — vectors are approximate for this, FTS5 is exact.

### Provider Abstraction via Protocols
All external APIs (Anthropic OCR, OpenAI Whisper, OpenAI Embeddings) are behind Python Protocol interfaces. Concrete adapters can be swapped by implementing the Protocol and wiring the new class in config. No changes to service layer needed.

### Embedding Dimensions
Chose 1024 dimensions (reduced from text-embedding-3-large's default 3072) to save storage with minimal quality loss. This is configurable.

### MCP Transport
Streamable HTTP (not stdio) because the server runs in Docker Compose and needs to be reachable over the network by Nanoclaw. SSE is deprecated as of MCP spec 2025-03-26.

### SQLite Migrations
Simple migration runner using `PRAGMA user_version` + numbered SQL files. No ORM (SQLAlchemy) or migration framework (Alembic) — overkill for a personal tool with a small schema.

### ChromaDB Client
Using `chromadb-client` (lightweight HTTP client) instead of the full `chromadb` package. The server runs in Docker; the app only needs the client.

## Technology Versions
- `anthropic` 0.86.0 (Claude Opus 4.6 for OCR)
- `openai` 2.29.0 (gpt-4o-transcribe for voice, text-embedding-3-large for embeddings)
- `chromadb-client` 1.5.5
- `mcp` 1.26.0 (FastMCP)
- `tiktoken` for token counting/chunking

## Test Coverage
76 tests passing covering:
- Database migrations and CRUD (26 tests)
- Provider adapters with mocked APIs (12 tests)
- Vector store operations (7 tests)
- Text chunking (6 tests)
- Ingestion pipeline (5 tests)
- Query service (8 tests)
- MCP server logic (10 tests)
- CLI (2 tests)

## What's Not Done Yet
- Mood scoring during ingestion (LLM-based analysis of entry mood)
- People/place extraction during ingestion (NER or LLM-based)
- Query routing via LLM (interpreting natural language queries to decide SQL vs vector vs both)
- GitHub Actions container build + push to ghcr.io
- Integration tests with real APIs
- API endpoint interface (REST, for future web UI)
