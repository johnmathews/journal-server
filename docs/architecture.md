# Architecture

**Status:** active. **Last updated:** 2026-05-09. **Supersedes:** none.

## Overview

The Journal Analysis Tool follows a layered architecture with strict separation of concerns. External services are
abstracted behind Protocol interfaces, enabling provider swapping without changes to core logic.

## Primary Usage

The main interface is via Slack. The [Nanoclaw](https://github.com/johnmathews/nanoclaw-ai-assistant) AI assistant
monitors a Slack channel where the user sends photos of handwritten journal pages. Nanoclaw connects as an MCP client to
this service's MCP server, triggering OCR ingestion and enabling natural language queries against the journal archive.

## How Search Works

Semantic search uses an **embedding model** (OpenAI `text-embedding-3-large`), not an LLM. The embedding model converts
text into a vector — a list of 1024 numbers representing the meaning of the text. At query time, the query string is
embedded into a vector and ChromaDB finds stored chunks with the nearest vectors by cosine distance. This is a
mathematical nearest-neighbor lookup, not language model reasoning.

This means no model reads, interprets, or summarizes your journal entries during search. You get back raw journal text
ranked by vector similarity. A query like "times I felt grateful" will match entries containing "I was really thankful"
because those phrases produce similar vectors, even though they share no keywords — but there is no comprehension
happening, just geometric proximity.

The distinction matters because of how the two interfaces work:

- **Via MCP** — an LLM client (e.g. Claude via Nanoclaw) decides what to search for, calls the search tool, and
  interprets the ranked results for you. The intelligence is in the calling LLM, not in this service.
- **Via CLI** — you run `journal search "times I felt grateful"` and get raw ranked results printed to your terminal. You
  interpret them yourself.

In both cases, this service does the same thing: embed the query, find nearest vectors, return ranked text. The
difference is whether an LLM is in the loop to interpret the results.

## Layers

### Interface Layer

Thin adapters that expose the service layer to external consumers. All three were converted from single files into
packages during the 2026-05-07/08 refactor round; references below are to the package roots.

- **MCP Server** (`mcp_server/`) — ~19 FastMCP tools across `tools/` (entities, ingestion, jobs, queries) on a
  streamable HTTP transport. Bootstrap, runserver, and app-wiring live in `mcp_server/{bootstrap,runserver,app}.py`.
  The primary interface for LLM-based MCP clients.
- **REST API** (`api/`) — many dozens of routes registered via `mcp.custom_route()`, split across
  `api/{entries,ingestion,settings,users,health,dashboard,search,jobs,entities,entity_merge,notifications}.py`. Same
  port as the MCP server; used by the webapp frontend and direct API-key consumers.
- **Auth REST surface** (`auth_api/`) — login/logout, registration, profile, API keys, admin reload endpoints.
  Split across `auth_api/{core,account,profile,api_keys,admin,_shared}.py`. See `docs/auth.md`.
- **CLI** (`cli/`) — argparse-based command-line interface. All argparse setup and `cmd_*` handlers for the simple
  subcommands live in `cli/__init__.py`; per-resource handler modules `cli/entities.py` and `cli/mood.py` hold the
  larger entity- and mood-related commands, with shared service wiring in `cli/_services.py` and seed data in
  `cli/_seed_samples.py`. Subcommands as of 2026-05-09 (16 total): `ingest`, `ingest-multi`, `search`, `list`,
  `stats`, `health`, `backfill-chunks`, `rechunk`, `backfill-mood`, `eval-chunking`, `seed`, `migrate-chromadb`,
  `extract-entities`, `backfill-entity-embeddings`, `repair-entity-names`, `renormalise-entity-casing`.

### Service Layer

Business logic orchestration:

- **IngestionService** — Coordinates OCR/transcription, text chunking, embedding generation, and dual-database storage
- **QueryService** — Routes queries to the appropriate backend (semantic search via ChromaDB, keyword search via FTS5,
  structured queries via SQLite)

### Provider Layer

Adapters for external APIs, each behind a Protocol interface:

- **OCRProvider** — `AnthropicOCRProvider` (Claude) or `GeminiOCRProvider` (Google Gemini), selected via `OCR_PROVIDER`
  env var
- **TranscriptionProvider** — Protocol with two concrete adapters (`OpenAITranscribeProvider`,
  `GeminiTranscribeProvider`) plus two composable wrappers (`RetryingTranscriptionProvider` for transient-error retries
  and `whisper-1` fallback, `ShadowTranscriptionProvider` for parallel diffing of two providers). The runtime stack is
  assembled by `build_transcription_provider()` from env vars: `Shadow(Retrying(Primary, fallback=whisper-1), Shadow)`.
  See `docs/transcription-providers.md`.
- **EmbeddingsProvider** — `OpenAIEmbeddingsProvider` (text-embedding-3-large)

### Storage Layer

- **EntryRepository** — SQLite with FTS5 for structured data and keyword search
- **VectorStore** — ChromaDB for semantic similarity search

## Data Model: raw_text vs final_text

Each entry has two text fields:

- **`raw_text`** — Immutable OCR or transcription output. Never modified after ingestion. Preserves the original provider
  output for audit and comparison.
- **`final_text`** — Starts as a copy of `raw_text`. This is the text used by all downstream features: chunking,
  embeddings, FTS5 indexing, search, and word count.

Editing `final_text` (e.g., to correct OCR errors) triggers re-chunking, re-embedding, and FTS5 rebuild for that entry.
The original `raw_text` remains unchanged.

### Multi-Page Entries

Multiple images can be ingested into a single entry. Each image is OCR'd independently, and the results are combined into
one entry:

- The `entry_pages` table stores per-page `raw_text` and page ordering
- The entry's `raw_text` and `final_text` are the concatenation of all page texts
- Adding pages to an existing entry triggers the same re-chunking and re-embedding flow

## Chunking Strategies

Chunking — splitting a journal entry into overlapping fragments before embedding — is the single biggest lever on
retrieval quality. The service supports two strategies, selected via the `CHUNKING_STRATEGY` environment variable.

### `fixed` — `FixedTokenChunker`

Paragraph-first packing with a tiktoken budget, sentence-level fallback for long paragraphs, fixed overlap.
Deterministic, no external API calls. Defined in `services/chunking.py`.

Algorithm:

1. If the whole text fits in `chunking_max_tokens`, return it as one chunk.
2. Split on blank lines into paragraphs; greedily pack them into chunks up to the max.
3. When flushing a chunk, carry `chunking_overlap_tokens` worth of trailing paragraphs into the next chunk.
4. If a single paragraph exceeds the max, fall back to sentence-level packing within that paragraph.

Good for: predictable behaviour, no dependency on an embedding provider, cheap rechunking. Bad for:
stream-of-consciousness prose where topic boundaries don't align with paragraph breaks.

### `semantic` — `SemanticChunker` (default)

Content-adaptive chunker that cuts where meaning actually shifts. One extra `embed_texts` call per ingested entry.
Defined in `services/chunking.py`.

Algorithm:

1. Split into sentences via `pysbd` (handles abbreviations, decimals, em-dashes).
2. Batch-embed every sentence through the configured `EmbeddingsProvider`.
3. Compute adjacent-sentence cosine similarity using numpy.
4. Apply **two percentile thresholds**:
   - `chunking_boundary_percentile` (default 25) — adjacent similarities at or below this percentile are cut positions.
   - `chunking_decisive_percentile` (default 10) — cuts at or below this are "clean" (no tail overlap). Cuts between the
     two are "weak" — the boundary sentence gets **duplicated into the next chunk as transition context** (adaptive tail
     overlap).
5. Enforce `chunking_min_tokens` by merging undersized segments into their nearest neighbour.
6. Enforce `chunking_max_tokens` by falling back to fixed-token packing for oversized segments.

Adaptive overlap is the key refinement: decisive topic shifts get a hard cut, ambiguous transitions get a soft one. That
keeps most embeddings tight while preserving context for sentences that span two topics.

### Metadata prefix

Independent of strategy, when `CHUNKING_EMBED_METADATA_PREFIX=true` (default on), each chunk is embedded with a
`"Date: YYYY-MM-DD. Weekday.\n\n"` header prepended. The stored document in ChromaDB is still the un-prefixed chunk text,
so downstream consumers get clean content — but the embedding vector carries date-sensitive signal that helps queries
like "what did I write about Atlas in February" match the right entries.

### `rechunk` CLI

Swapping strategy would leave the existing ChromaDB chunks reflecting the old strategy. The `journal rechunk` command
fixes that: it iterates every entry, deletes its vectors, and regenerates them using the currently-configured strategy.
Use `--dry-run` to preview counts without writing or calling the embeddings API.

### `eval-chunking` CLI

Chunking quality without ground truth is measurable via intrinsic metrics over the stored corpus:

- **Cohesion** — mean pairwise cosine similarity of sentences within each chunk (higher = chunks are internally
  consistent).
- **Separation** — `1 − cosine` between adjacent chunks within an entry (higher = chunks are actually distinct from each
  other).
- **Ratio** — `cohesion / (1 − separation)`, a single number to optimise.

Tuning loop:

```bash
for pct in 15 20 25 30 35; do
  CHUNKING_STRATEGY=semantic CHUNKING_BOUNDARY_PERCENTILE=$pct \
    uv run journal rechunk
  CHUNKING_STRATEGY=semantic CHUNKING_BOUNDARY_PERCENTILE=$pct \
    uv run journal eval-chunking --json
done
```

Pick the value with the highest ratio and set it as the default in `config.py`.

## Data Flow

### Ingestion

```
Image/Audio → Provider (OCR / Transcription) → Raw Text
    → SQLite (entry with raw_text + final_text, entry_pages for images)
    → Chunking (strategy: fixed or semantic) using final_text
    → Embeddings (OpenAI, 1024 dims; optional date-metadata prefix)
    → ChromaDB (chunks + embeddings + metadata)
```

### OCR Correction

```
Edit final_text → Update SQLite → Delete old ChromaDB chunks
    → Re-chunk final_text → Re-embed → Store new chunks
    → FTS5 trigger auto-rebuilds index
```

### Deletion

```
DELETE /api/entries/{id} → IngestionService.delete_entry()
    → ChromaDB.delete_entry(id)  (purge vector chunks first)
    → SQLite DELETE FROM entries WHERE id = ?
    → Foreign-key cascades drop entry_pages, entry_people, entry_places,
      entry_tags, mood_scores, source_files
    → FTS5 AFTER DELETE trigger removes the row from the full-text index
```

### Query

```
Natural Language Query
    → Semantic: Embed query (OpenAI) → Vector nearest-neighbor search (ChromaDB) → Enrich from SQLite
    → Keyword: FTS5 full-text search on SQLite
    → Statistical: SQL aggregation on SQLite
```

Note: only the semantic path calls an external AI model (the embedding model). Keyword and statistical queries are purely
local database operations. No LLM is involved in any query path — see "How Search Works" above.

## Database Schema

### SQLite

The schema has accreted across 22 migrations (`db/migrations/0001..0022`). Production runs at `user_version = 22` as
of 2026-05-09. Tables in current use:

**Core entries / pipeline**
- `entries` — Core table (user_id, date, source_type, raw_text, final_text, word_count, chunk_count, entity_extraction_stale)
- `entry_pages` — Per-page OCR text for multi-page entries
- `entry_chunks` — Per-chunk text with character offsets (used by the webapp chunk-overlay)
- `entry_uncertain_spans` — Per-entry uncertain OCR/transcription spans (yellow Review-toggle highlights)
- `source_files` — Original file metadata with SHA-256 dedup
- `entries_fts` (+ `entries_fts_*`) — FTS5 virtual table over `final_text` with porter stemming

**Auth / multi-tenant** (migrations 0011, 0012)
- `users`, `user_sessions` (session tokens stored hashed via migration 0012), `api_keys`, `user_preferences`

**Entity tracking** (migrations 0004, 0008, 0011, 0018-0022)
- `entities`, `entity_aliases`, `entity_mentions`, `entity_relationships`
- `entity_merge_history`, `entity_merge_candidates`, `entity_pair_decisions`
- Plus quarantine columns on `entities` (0018) and on the merge-history snapshot (0019)

**Mood scoring** (initial schema + 0014)
- `mood_scores` — sparse `(entry_id, dimension)` storage; `rationale` column added in 0014

**Jobs / runtime config** (migrations 0006, 0010, 0015, 0017)
- `jobs` — single in-process job queue (`type`, `status`, `params_json`, `progress_*`, `result_json`, `status_detail`, `user_id`)
- `runtime_settings` — DB-backed runtime overrides (toggleable from webapp)
- `pricing` — editable per-model cost table (12 rows in prod)

**Legacy (intentionally retained but unused)**
- `people`, `places`, `tags` — pre-entity-tracking tables from the initial schema; no current code reads or writes them.

### ChromaDB

- Single collection `journal_entries` with cosine distance
- Documents: text chunks from entries
- Embeddings: 1024-dimensional OpenAI vectors
- Metadata: `entry_id`, `entry_date`, `chunk_index`, `user_id`

## Initialization

Services (DB, vector store, providers) are initialized eagerly at server startup in `main()`, before the HTTP server
begins accepting requests. This ensures the REST API is immediately functional without waiting for the first MCP client
session to connect.

The same services dict is shared between MCP sessions (via the lifespan context) and REST API routes (via a module-level
reference). Initialization is idempotent — the `_init_services()` function guards against duplicate setup.

## Deployment

Docker Compose stack with three services running on the `media` VM. New images are pulled and restarted manually
(`docker compose pull && docker compose up -d`); there is no Ansible playbook in the loop.

- `journal-server` — Python app running MCP server + REST API. Host port `8400`. The repo's `compose.yml` pins this to
  `127.0.0.1:8400` for the loopback-only stance recommended in `docs/security.md`; the production compose on `media`
  currently exposes `0.0.0.0:8400` (LAN-reachable inside the home network — public exposure is only via the Cloudflare
  Tunnel that fronts the webapp on `:8402`).
- `journal-chromadb` — ChromaDB vector database (host port `8401`). Custom image (`ghcr.io/johnmathews/journal-chromadb`)
  with `curl` baked in for a working healthcheck.
- `journal-webapp` — Vue.js frontend served by nginx (host port `8402`), proxies `/api/*` to journal-server.

**Public exposure** is via Cloudflare Tunnel — there is no reverse proxy on `media`. See
`docs/production-deployment.md` for runbook details.

**CI/CD pipeline:** On push to `main`, GitHub Actions runs tests and linting, then builds and pushes both Docker images
to `ghcr.io/johnmathews/journal-server` and `ghcr.io/johnmathews/journal-webapp`.

**Data persistence:** SQLite and ChromaDB data are bind-mounted to `/srv/media/config/journal/` on the host.

**Endpoints (loopback or behind tunnel):** MCP `http://127.0.0.1:8400/mcp`, REST `http://127.0.0.1:8400/api/`,
Web UI `http://127.0.0.1:8402/`.
