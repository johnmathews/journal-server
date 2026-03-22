# Development Guide

## Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Docker (for ChromaDB)

## Setup

```bash
git clone https://github.com/johnmathews/journal-agent.git
cd journal-agent
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

## Local ChromaDB

For development, run ChromaDB locally:

```bash
docker run -d --name chromadb -p 8000:8000 -v ./chroma-data:/data chromadb/chroma:1.5.5
```

## MCP Server Testing

Use the MCP inspector for interactive testing:

```bash
uv run mcp dev src/journal/mcp_server.py
```
