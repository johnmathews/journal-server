# Journal Analysis Tool

Personal journal insight engine that ingests handwritten pages (OCR) and voice notes (transcription), stores them in SQLite + ChromaDB, and answers natural language queries via MCP server, CLI, and API.

## Project Structure

Most originally-single-file modules (`api.py`, `cli.py`, `mcp_server.py`,
`db/repository.py`, `services/ingestion.py`, `services/jobs/runner.py`, plus the
`auth_api`, `entitystore`, and `entity_extraction` services) have been carved into
packages during the round-1 → round-3 refactors. See `docs/refactor-round-3.md` for
the canonical state and the per-split journal entries under
`journal/260507-*.md` and `journal/260508-*.md`.

```
src/journal/
  config.py             — Configuration from environment variables
  logging.py            — Structured logging setup
  models.py             — Shared data models (dataclasses)
  db/
    connection.py       — get_connection() with PRAGMAs (same-thread guard on)
    factory.py          — ConnectionFactory: per-thread SQLite connections
    migrations.py       — Migration runner (PRAGMA user_version)
    migrations/*.sql    — SQL migration files (currently 0001 → 0035)
    repository/         — SQLiteEntryRepository carved into protocol/store/core/
                          pages/chunks/search/mood/stats/analytics
    fitness_repository.py    — Fitness activities / daily wellness / auth state
    fitness_integrity.py     — Fitness data integrity checks
    storyline_repository.py  — Storylines + storyline-entity anchors
    jobs_repository.py       — Background jobs table
    user_repository.py       — Users, sessions, API keys
  providers/            — External-API adapters behind Protocol interfaces
    ocr.py              — OCR Protocol + Anthropic and Gemini adapters (prod = Gemini)
    transcription.py    — Transcription Protocol + OpenAI (gpt-4o-transcribe / whisper-1)
                          and Gemini adapters
    embeddings.py       — Embeddings Protocol + OpenAI text-embedding-3-large adapter
    extraction.py       — Entity extraction provider
    mood_scorer.py      — Mood scoring (Anthropic tool-use)
    formatter.py        — Transcript paragraph-break formatter (Anthropic Haiku)
    reranker.py         — Listwise reranker (Anthropic Haiku) for hybrid search
    answerer.py         — Grounded answer synthesis + multi-turn continue_conversation
                          (Sonnet; optional context_note + one-hop search_again tool)
    query_classifier.py — Binary question/search gate for the answer endpoint (Haiku)
    intent_classifier.py — Four-way conversation intent classifier
                          (lookup/aggregate/temporal/trend, Haiku) + heuristic fallback
  vectorstore/
    store.py            — VectorStore Protocol + ChromaDB implementation
  entitystore/          — Entity persistence carved into store/mentions/relationships/aliases
  services/
    ingestion/          — image/voice/text/url ingestion orchestrators
    query.py            — Query routing (semantic + FTS5 + stats)
    chunking.py         — Text chunking with tiktoken
    hybrid.py           — Hybrid BM25 + dense + RRF + listwise rerank pipeline
    conversations/      — Multi-turn chat reply: intent classify → per-intent
                          handlers (lookup/aggregate/temporal/trend) + passages
    entity_extraction/  — Entity extraction service (orchestrator + helpers)
    fitness/            — Strava/Garmin fetch, normalize, backfill + activity-type map;
                          credentials.py = Fernet-encrypted saved Garmin credentials
    storylines/         — StorylineEngine (judge-driven continue-or-break chaptering +
                          narrator), extension classifier, segments; wired via the
                          `bootstrap-storylines` CLI
    usage.py            — Per-job LLM token-usage collector (contextvar-scoped)
    jobs/               — Background job runner: workers/, runner (two-pool:
                          parallel Pool A + single-worker storyline Pool B),
                          run_job (usage-flush wrapper), save_pipeline,
                          notifier, retry
    auth.py / email.py  — Multi-user auth (sessions, API keys, password reset)
    notifications.py    — Toast + Pushover notification dispatch
    reload.py           — Live-reload hooks for context files / mood dimensions /
                          entity casing
  mcp_server/           — FastMCP server: bootstrap, app, runserver, tools/
                          (queries, ingestion, entities, jobs, fitness, storylines)
  api/                  — REST routes (dashboard, entries, entities, entity_merge,
                          fitness, fitness_garmin, fitness_jobs, fitness_strava,
                          ingestion, jobs, notifications, search, settings,
                          storylines, storylines_write, users, health — see
                          api/__init__.py for the authoritative list; auth routes
                          live in auth_api/)
  auth_api/             — Auth REST endpoints carved into core/account/profile/
                          api_keys/admin
  cli/                  — Typer CLI: __init__ (entry), entities, fitness, mood,
                          _services, _seed_samples
tests/                  — pytest tests mirroring src structure
docs/                   — Project documentation
journal/                — Development journal entries (YYMMDD-name.md)
```

## Commands

- `uv sync` — Install dependencies
- `uv run pytest` — Run tests (see "Running tests locally" below for the
  unit-vs-integration split)
- `uv run pytest --cov` — Run tests with coverage
- `uv run ruff check src/ tests/` — Lint
- `uv run journal` — Run CLI
- `docker compose up` — Run full stack (app + ChromaDB)

## Running tests locally

The suite has two tiers:

- **Unit tests** (~2950 tests): pure Python + in-memory SQLite. No
  external services. `uv run pytest` runs them and they always pass.
- **Integration tests** (`tests/integration/`, marked
  `@pytest.mark.integration`): need a real ChromaDB. `tests/integration/
  conftest.py` opens a TCP probe on `CHROMADB_HOST:CHROMADB_PORT` (default
  `localhost:8401` — the dev compose port; the legacy `CHROMA_HOST`/
  `CHROMA_PORT` names still work as a one-release fallback) and
  **auto-skips the suite
  with an actionable reason** if Chroma isn't reachable. So plain
  `uv run pytest` from a cold dev box now reports
  "2954 passed, 11 skipped" rather than 11 errors.

Three modes:

1. **Default (unit only, integration skipped if Chroma is down):**
   `uv run pytest`
2. **Run integration tests too:** bring up Chroma first, then run.
   ```bash
   docker compose -f docker-compose.dev.yml up -d   # Chroma on :8401
   uv run pytest                                    # all 2594 pass
   ```
3. **Match what CI does for the unit job (force-skip integration even
   if Chroma is up):** `uv run pytest -m "not integration"`. CI runs
   the integration job separately with `CHROMADB_PORT=8000` against its
   own service container — see `.github/workflows/ci-and-deploy.yml`.

`CHROMADB_PORT` defaults to `8401` locally because that's what
`docker-compose.dev.yml` exposes; the previous default of 8000 silently
made local integration runs fail even when Chroma was up via the dev
compose. Override the env var if you've brought up Chroma some other
way.

## Architecture Principles

- All external APIs (Anthropic, OpenAI, Google Gemini, ChromaDB) are behind Protocol interfaces
- Concrete adapters can be swapped without touching core logic
- SQLite for structured/quantitative queries, ChromaDB for semantic search
- FTS5 + dense retrieval + RRF fusion + listwise rerank for hybrid search
- MCP server is a thin interface layer — business logic lives in services/
- API routing follows URL-resource layout by default; write/job-creation routes are
  bundled in `api/ingestion.py` / `api/storylines_write.py` / `api/fitness_jobs.py`.
  See `docs/code-quality-principles.md`.

## Tech Stack

- Python 3.13, uv, pytest, ruff
- Google Gemini API (OCR primary in prod via `gemini-2.5-pro`, plus a Gemini
  transcription adapter and shadow-mode support)
- Anthropic SDK (Claude Opus 4.6 OCR adapter; Haiku 4.5 for mood scoring,
  transcript formatting, date-heading detection, and the listwise reranker)
- OpenAI SDK (`gpt-4o-transcribe` primary transcription with `whisper-1`
  fallback; `text-embedding-3-large` embeddings)
- ChromaDB (vector storage, cosine distance)
- SQLite (structured storage, FTS5)
- MCP SDK with FastMCP (streamable HTTP transport)

## Commit, Push, and CI

After committing, always push and watch GitHub Actions CI (`gh run watch`). If CI fails, read the
logs, fix the issue, run the full test suite locally, commit, push, and watch again. Do not
consider work done until CI is green. When fixing bugs, always write a failing test first that
reproduces the issue, then fix the code to make it pass.

## Documentation lifecycle

- Prefer shorter docs over long ones, but no hard length cap — let scope and detail required dictate length.
- When a doc is **closed** (work units shipped) or **superseded** (replaced by a newer doc): add a status header
  (`**Status:** closed YYYY-MM-DD.` or `**Status:** superseded by [...](...) (YYYY-MM-DD).`) to the top of the old
  doc, then `git mv` it into `docs/archive/` in the same commit and update inbound links from active docs.
- The active `docs/` listing should only contain currently-load-bearing material. Closed and superseded plans live in
  `docs/archive/` so the rationale is preserved without cluttering the index.
