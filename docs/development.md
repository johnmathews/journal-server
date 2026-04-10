# Development Guide

## Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Docker (for ChromaDB)

## Setup

```bash
git clone https://github.com/johnmathews/journal-server.git
cd journal-server
uv sync
```

## Running Tests

```bash
# All tests
uv run pytest

# With coverage
uv run pytest --cov --cov-report=term-missing

# Specific test file
uv run pytest tests/test_db/test_repository.py -v

# Single test
uv run pytest tests/test_db/test_repository.py::TestFTS::test_search_text -v
```

## Linting

```bash
uv run ruff check src/ tests/
uv run ruff check src/ tests/ --fix  # Auto-fix
```

## Project Structure

```
src/journal/          — Source code (installed as 'journal' package)
tests/                — Tests (mirrors src/ structure)
docs/                 — Documentation
journal/              — Development journal entries
.engineering-team/    — Engineering team working docs (gitignored)
```

## Adding a New Provider

To swap or add a provider (e.g., switch OCR from Anthropic to OpenAI):

1. Create a new class implementing the relevant Protocol in `src/journal/providers/`
2. The Protocol defines the interface — see `OCRProvider`, `TranscriptionProvider`, or `EmbeddingsProvider`
3. Write tests with mocked API responses in `tests/test_providers/`
4. Update `config.py` if new configuration is needed
5. Wire the new provider in `mcp_server.py` and `cli.py`

## Database Migrations

Migrations are plain SQL files in `src/journal/db/migrations/`:

```
0001_initial_schema.sql
0002_add_new_feature.sql   # Add new migration files here
```

Naming: `NNNN_description.sql` where NNNN is the version number.

Migrations run automatically on startup. The current version is tracked via `PRAGMA user_version`.

## Local Development (Full Stack)

To develop the journal-server and journal-webapp together locally:

```bash
# 1. Start ChromaDB
docker compose -f docker-compose.dev.yml up -d

# 2. Configure environment
cp .env.example .env
# Edit .env — API keys only needed for ingestion, not for browsing/editing

# 3. Seed sample data (no API keys needed)
uv run journal seed

# 4. Start the backend (REST API + MCP on port 8400)
uv run python -m journal.mcp_server

# 5. In another terminal, start the webapp
cd ../journal-webapp
npm run dev
# Opens at http://localhost:5173, proxies /api/* to localhost:8400
```

### What needs API keys and what doesn't

| Feature              | Needs API keys? | Which key?     |
|----------------------|-----------------|----------------|
| List / browse entries | No              |                |
| Edit final_text      | No              |                |
| View statistics      | No              |                |
| Ingest image (OCR)   | Yes             | ANTHROPIC      |
| Ingest voice         | Yes             | OPENAI         |
| Semantic search      | Yes             | OPENAI         |
| Keyword search (FTS) | No              |                |
| Seed sample data     | No              |                |

### Seed data

The `seed` command creates 5 sample journal entries with realistic text. No API keys, no ChromaDB, no embeddings needed — just SQLite:

```bash
uv run journal seed              # all 5 samples
uv run journal seed --count 2    # just 2
```

Seeded entries won't have embeddings, so semantic search won't find them. To add embeddings, re-ingest with API keys. The `seed` command does compute `chunk_count` correctly from the chunker, so the webapp's "chunks" column shows the right value even without embeddings.

### Backfilling chunk_count

If entries exist with a stale `chunk_count = 0` — e.g. from seed data predating the chunk-count fix, or from database rows created before migration 0002 added the column — run:

```bash
uv run journal backfill-chunks
```

This re-runs the tokenizer/chunker over every entry's `final_text || raw_text` and updates the stored column. It does **not** regenerate embeddings (so no API keys are required) and it's idempotent — re-running reports everything as `Unchanged`.

```
Updated:   0
Unchanged: 5
Skipped:   0 (no text)
```

If you need to rebuild embeddings as well, re-ingest the entry via the REST API or CLI — the PATCH path in `update_entry_text()` re-chunks and re-embeds in one call.

## Local ChromaDB

The `docker-compose.dev.yml` runs ChromaDB on port 8401 (matching `.env.example`):

```bash
docker compose -f docker-compose.dev.yml up -d
```

Alternatively, run it directly:

```bash
docker run -d --name chromadb -p 8401:8000 chromadb/chroma:latest
```

## MCP Server Testing

Use the MCP inspector for interactive testing:

```bash
uv run mcp dev src/journal/mcp_server.py
```
