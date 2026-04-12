# Implementation Plan: Journal Analysis Tool

**Date:** 2026-03-22

## Work Units

### Unit 1: Project Scaffolding (Critical)
**Priority:** Critical
**Dependencies:** None

Set up the Python project structure, dependencies, and development tooling.

**Changes:**
- `pyproject.toml` — Project metadata, dependencies, tool config
- `src/journal/__init__.py` — Package init
- `src/journal/py.typed` — PEP 561 marker
- `.gitignore` — Python/Docker/IDE ignores
- `CLAUDE.md` — Project conventions for Claude Code
- `Dockerfile` — Multi-stage build
- `docker-compose.yml` — Full stack (app + ChromaDB)
- `.github/workflows/ci.yml` — GitHub Actions CI

**Dependencies:**
- `anthropic` ~0.86
- `openai` ~2.29
- `chromadb-client` ~1.5
- `mcp[cli]` ~1.26
- `tiktoken` (for token counting/chunking)
- Dev: `pytest`, `pytest-asyncio`, `coverage`, `ruff`

**Acceptance criteria:** `uv sync` works, `pytest` runs (even with no tests), `docker compose build` succeeds

---

### Unit 2: Configuration and Logging (Critical)
**Priority:** Critical
**Dependencies:** Unit 1

**Changes:**
- `src/journal/config.py` — Pydantic-free config from env vars (API keys, DB paths, ChromaDB host, etc.)
- `src/journal/logging.py` — Structured logging setup
- `tests/conftest.py` — Shared fixtures (temp DB, mock clients)

**Acceptance criteria:** Config loads from env vars with sensible defaults, logging works

---

### Unit 3: Database Layer (Critical)
**Priority:** Critical
**Dependencies:** Unit 2

**Changes:**
- `src/journal/db/connection.py` — SQLite connection factory with PRAGMAs
- `src/journal/db/migrations.py` — Migration runner using `PRAGMA user_version`
- `src/journal/db/migrations/0001_initial_schema.sql` — Core schema (entries, mood_scores, people, places, tags, source_files, FTS5)
- `src/journal/db/repository.py` — Repository interface (Protocol) + SQLite implementation
- `tests/test_db/` — Tests for migrations, CRUD, FTS5 queries

**Repository interface methods:**
- `create_entry(date, source_type, raw_text, word_count) -> Entry`
- `get_entry(entry_id) -> Entry | None`
- `get_entries_by_date(date) -> list[Entry]`
- `list_entries(start_date?, end_date?, limit, offset) -> list[Entry]`
- `search_text(query, start_date?, end_date?) -> list[Entry]` (FTS5)
- `get_statistics(start_date?, end_date?) -> Statistics`
- `add_people(entry_id, names) -> None`
- `add_places(entry_id, names) -> None`
- `add_tags(entry_id, tags) -> None`
- `add_mood_score(entry_id, dimension, score, confidence?) -> None`
- `get_mood_trends(start_date?, end_date?, granularity) -> list[MoodTrend]`
- `get_topic_frequency(topic, start_date?, end_date?) -> int`

**Acceptance criteria:** All CRUD operations tested, FTS5 search works, migrations apply cleanly

---

### Unit 4: Provider Interfaces and Adapters (Critical)
**Priority:** Critical
**Dependencies:** Unit 2

**Changes:**
- `src/journal/providers/ocr.py` — OCR interface (Protocol) + Anthropic adapter
- `src/journal/providers/transcription.py` — Transcription interface (Protocol) + OpenAI Whisper adapter
- `src/journal/providers/embeddings.py` — Embeddings interface (Protocol) + OpenAI adapter
- `tests/test_providers/` — Tests with mocked API responses

**OCR interface:**
- `extract_text(image_data: bytes, media_type: str) -> str`

**Transcription interface:**
- `transcribe(audio_data: bytes, media_type: str, language?: str) -> str`

**Embeddings interface:**
- `embed_texts(texts: list[str]) -> list[list[float]]`
- `embed_query(query: str) -> list[float]`

**Acceptance criteria:** All adapters tested with mocked responses, interfaces are clean Protocols

---

### Unit 5: Vector Store Layer (High)
**Priority:** High
**Dependencies:** Unit 4

**Changes:**
- `src/journal/vectorstore/store.py` — VectorStore interface (Protocol) + ChromaDB implementation
- `tests/test_vectorstore/` — Tests (can use in-memory ChromaDB for testing)

**VectorStore interface:**
- `add_entry(entry_id, chunks: list[str], embeddings: list[list[float]], metadata: dict) -> None`
- `search(query_embedding: list[float], limit, filters?) -> list[SearchResult]`
- `delete_entry(entry_id) -> None`

**Acceptance criteria:** Add/search/delete operations tested, metadata filtering works

---

### Unit 6: Ingestion Service (High)
**Priority:** High
**Dependencies:** Units 3, 4, 5

**Changes:**
- `src/journal/services/ingestion.py` — Orchestrates: OCR/transcription -> chunking -> embedding -> store in both DBs
- `src/journal/services/chunking.py` — Text chunking with tiktoken (paragraph-aware, 500 token chunks, 100 token overlap)
- `tests/test_services/test_ingestion.py` — Tests with mocked providers

**Flow:**
1. Receive image/audio + date
2. Extract text (OCR or transcription)
3. Compute word count, store entry in SQLite
4. Chunk text
5. Generate embeddings for chunks
6. Store chunks + embeddings in ChromaDB with entry_id metadata
7. Return entry summary

**Acceptance criteria:** Full ingestion pipeline tested end-to-end with mocks, chunking produces correct overlap

---

### Unit 7: Query Service (High)
**Priority:** High
**Dependencies:** Units 3, 5

**Changes:**
- `src/journal/services/query.py` — Query service that combines SQLite and ChromaDB results
- `tests/test_services/test_query.py` — Tests

**Methods:**
- `search_entries(query, start_date?, end_date?, limit, offset) -> SearchResults`
- `get_entries_by_date(date) -> list[Entry]`
- `list_entries(start_date?, end_date?, limit, offset) -> list[Entry]`
- `get_statistics(start_date?, end_date?) -> Statistics`
- `get_mood_trends(start_date?, end_date?, granularity) -> MoodTrends`
- `get_topic_frequency(topic, start_date?, end_date?) -> TopicFrequency`

**Acceptance criteria:** All query types tested, semantic search uses vector store, frequency queries use FTS5

---

### Unit 8: MCP Server (High)
**Priority:** High
**Dependencies:** Units 6, 7

**Changes:**
- `src/journal/mcp_server.py` — FastMCP server with 7 tools, lifespan pattern, streamable HTTP
- `tests/test_mcp_server.py` — Tests for tool functions

**Tools:**
1. `journal_search_entries` — Semantic search
2. `journal_get_entries_by_date` — Date lookup
3. `journal_list_entries` — Chronological listing
4. `journal_get_statistics` — Quantitative stats
5. `journal_get_mood_trends` — Mood over time
6. `journal_get_topic_frequency` — Keyword/topic counting
7. `journal_ingest_entry` — Ingest image or voice note

**Acceptance criteria:** All tools tested, lifespan manages connections, server starts on streamable HTTP

---

### Unit 9: CLI Interface (Medium)
**Priority:** Medium
**Dependencies:** Units 6, 7

**Changes:**
- `src/journal/cli.py` — CLI using argparse (ingest, search, stats, list commands)
- `tests/test_cli.py` — Tests

**Acceptance criteria:** All subcommands work, output is human-readable

---

### Unit 10: Docker and Deployment (Medium)
**Priority:** Medium
**Dependencies:** Unit 8

**Changes:**
- `Dockerfile` — Multi-stage build (already scaffolded in Unit 1, finalize here)
- `docker-compose.yml` — Full stack with ChromaDB, journal app, volumes, healthchecks
- `docs/deployment.md` — Deployment documentation

**Acceptance criteria:** `docker compose up` starts the full stack, MCP server is reachable, data persists across restarts

---

### Unit 11: Documentation (Medium)
**Priority:** Medium
**Dependencies:** All previous units

**Changes:**
- `docs/architecture.md` — Architecture overview, component diagram, data flow
- `docs/api.md` — MCP tool reference (auto-generated descriptions)
- `docs/development.md` — Local dev setup, running tests, adding providers
- `docs/configuration.md` — Environment variables reference
- `README.md` — Project overview, quick start

**Acceptance criteria:** All docs are accurate, complete, and match the code
