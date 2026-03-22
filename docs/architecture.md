# Architecture

## Overview

The Journal Analysis Tool follows a layered architecture with strict separation of concerns. External services are abstracted behind Protocol interfaces, enabling provider swapping without changes to core logic.

## Layers

### Interface Layer
Thin adapters that expose the service layer to external consumers:
- **MCP Server** (`mcp_server.py`) — 7 tools via FastMCP, streamable HTTP transport
- **CLI** (`cli.py`) — argparse-based command-line interface
- **API** — Future REST endpoints (out of scope for v0.1)

### Service Layer
Business logic orchestration:
- **IngestionService** — Coordinates OCR/transcription, text chunking, embedding generation, and dual-database storage
- **QueryService** — Routes queries to the appropriate backend (semantic search via ChromaDB, keyword search via FTS5, structured queries via SQLite)

### Provider Layer
Adapters for external APIs, each behind a Protocol interface:
- **OCRProvider** — `AnthropicOCRProvider` (Claude Opus 4.6 vision)
- **TranscriptionProvider** — `OpenAITranscriptionProvider` (gpt-4o-transcribe)
- **EmbeddingsProvider** — `OpenAIEmbeddingsProvider` (text-embedding-3-large)

### Storage Layer
- **EntryRepository** — SQLite with FTS5 for structured data and keyword search
- **VectorStore** — ChromaDB for semantic similarity search

## Data Flow

### Ingestion
```
Image/Audio → Provider (OCR/Whisper) → Raw Text
    → SQLite (entry + metadata)
    → Chunking (500 tokens, 100 overlap)
    → Embeddings (OpenAI, 1024 dims)
    → ChromaDB (chunks + embeddings + metadata)
```

### Query
```
Natural Language Query
    → Semantic: Embed query → ChromaDB similarity search → Enrich from SQLite
    → Keyword: FTS5 search on SQLite
    → Statistical: SQL aggregation on SQLite
```

## Database Schema

### SQLite
- `entries` — Core table (date, source_type, raw_text, word_count)
- `mood_scores` — Multi-dimensional mood tracking per entry
- `people`, `places`, `tags` — Entity tables with junction tables for many-to-many
- `source_files` — Original file metadata with SHA-256 dedup
- `entries_fts` — FTS5 virtual table with porter stemming

### ChromaDB
- Single collection `journal_entries` with cosine distance
- Documents: text chunks from entries
- Embeddings: 1024-dimensional OpenAI vectors
- Metadata: `entry_id`, `entry_date`, `chunk_index`

## Deployment

Docker Compose stack with two services:
- `journal` — Python app running MCP server
- `chromadb` — ChromaDB vector database

SQLite is stored on a named Docker volume. ChromaDB persists to its own volume.
