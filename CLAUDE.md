# Journal Analysis Tool

Personal journal insight engine that ingests handwritten pages (OCR) and voice notes (transcription), stores them in SQLite + ChromaDB, and answers natural language queries via MCP server, CLI, and API.

## Project Structure

```
src/journal/
  config.py          — Configuration from environment variables
  logging.py         — Structured logging setup
  models.py          — Shared data models (dataclasses)
  db/                — SQLite database layer
    connection.py    — Connection factory with PRAGMAs
    migrations.py    — Migration runner (PRAGMA user_version)
    migrations/*.sql — SQL migration files
    repository.py    — Repository Protocol + SQLite implementation
  providers/         — External API adapters behind Protocol interfaces
    ocr.py           — OCR Protocol + Anthropic adapter
    transcription.py — Transcription Protocol + OpenAI Whisper adapter
    embeddings.py    — Embeddings Protocol + OpenAI adapter
  vectorstore/       — Vector database layer
    store.py         — VectorStore Protocol + ChromaDB implementation
  services/          — Business logic
    ingestion.py     — Ingest images/audio -> text -> chunks -> embeddings -> store
    query.py         — Query routing (semantic + FTS5 + stats)
    chunking.py      — Text chunking with tiktoken
  mcp_server.py      — FastMCP server (streamable HTTP)
  api.py             — REST API endpoints via mcp.custom_route()
  cli.py             — CLI interface
tests/               — pytest tests mirroring src structure
docs/                — Project documentation
journal/             — Development journal entries (YYMMDD-name.md)
```

## Commands

- `uv sync` — Install dependencies
- `uv run pytest` — Run tests
- `uv run pytest --cov` — Run tests with coverage
- `uv run ruff check src/ tests/` — Lint
- `uv run journal` — Run CLI
- `docker compose up` — Run full stack (app + ChromaDB)

## Architecture Principles

- All external APIs (Anthropic, OpenAI, ChromaDB) are behind Protocol interfaces
- Concrete adapters can be swapped without touching core logic
- SQLite for structured/quantitative queries, ChromaDB for semantic search
- FTS5 for exact keyword frequency queries
- MCP server is a thin interface layer — business logic lives in services/

## Tech Stack

- Python 3.13, uv, pytest, ruff
- Anthropic SDK (OCR via Claude Opus 4.6)
- OpenAI SDK (Whisper transcription, text-embedding-3-large)
- ChromaDB (vector storage, cosine distance)
- SQLite (structured storage, FTS5)
- MCP SDK with FastMCP (streamable HTTP transport)
