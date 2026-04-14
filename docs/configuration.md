# Configuration

All configuration is via environment variables. No config files are needed.

## Required

| Variable            | Description                                                                                                                                                                |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `JOURNAL_API_TOKEN` | Bearer token required on every REST and MCP request. The server refuses to start without it. Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `ANTHROPIC_API_KEY` | Anthropic API key — required when `OCR_PROVIDER=anthropic` (the default). Also used for entity extraction and mood scoring.                                                |
| `OPENAI_API_KEY`    | OpenAI API key for Whisper transcription and embeddings.                                                                                                                   |
| `GOOGLE_API_KEY`    | Google API key — required only when `OCR_PROVIDER=gemini`.                                                                                                                 |

See `docs/security.md` for the threat model and how auth fits in.

## Optional — deployment

| Variable                            | Default               | Description                                                                                                                                                            |
| ----------------------------------- | --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `DB_PATH`                           | `journal.db`          | Path to SQLite database file                                                                                                                                           |
| `CHROMADB_HOST`                     | `localhost`           | ChromaDB server hostname                                                                                                                                               |
| `CHROMADB_PORT`                     | `8000`                | ChromaDB server port                                                                                                                                                   |
| `MCP_HOST`                          | `0.0.0.0`             | MCP server bind address (in-container). The host-side port in `docker-compose.yml` is bound to `127.0.0.1` — see `docs/security.md`.                                   |
| `MCP_PORT`                          | `8000`                | MCP server port (use 8400 on media VM to avoid Gluetun conflict)                                                                                                       |
| `MCP_ALLOWED_HOSTS`                 | `127.0.0.1,localhost` | Comma-separated hostnames that DNS rebinding protection will accept as Host headers. Add any externally-facing hostname if you front the service with a reverse proxy. |
| `SLACK_BOT_TOKEN`                   |                       | Slack bot token for downloading files from Slack URLs                                                                                                                  |
| `API_CORS_ORIGINS`                  |                       | Comma-separated list of allowed CORS origins for the REST API (e.g., `http://localhost:5173`). Empty disables CORS.                                                    |
| `LOG_LEVEL`                         | `INFO`                | Logging level (DEBUG, INFO, WARNING, ERROR)                                                                                                                            |
| `JOURNAL_AUTHOR_NAME`               | `John`                | Name the entity extractor uses as the subject of first-person statements. See `docs/entity-tracking.md`.                                                               |
| `ENTITY_DEDUP_SIMILARITY_THRESHOLD` | `0.88`                | Cosine similarity threshold for the stage-c embedding dedup fallback. Raise to be stricter, lower to merge more aggressively.                                          |

## Optional — chunking

See `docs/architecture.md` → "Chunking Strategies" for the algorithm and tradeoffs.

| Variable                         | Default    | Applies to    | Description                                                                                               |
| -------------------------------- | ---------- | ------------- | --------------------------------------------------------------------------------------------------------- |
| `CHUNKING_STRATEGY`              | `semantic` | both          | `"fixed"` or `"semantic"`                                                                                 |
| `CHUNKING_MAX_TOKENS`            | `150`      | both          | Upper bound for chunk size                                                                                |
| `CHUNKING_OVERLAP_TOKENS`        | `40`       | fixed only    | Tokens carried between adjacent chunks                                                                    |
| `CHUNKING_MIN_TOKENS`            | `30`       | semantic only | Minimum chunk size; smaller segments are merged                                                           |
| `CHUNKING_BOUNDARY_PERCENTILE`   | `25`       | semantic only | Adjacent similarities at/below this percentile are cut positions                                          |
| `CHUNKING_DECISIVE_PERCENTILE`   | `10`       | semantic only | Cuts at/below this are clean (no overlap); between 10 and 25 are weak cuts with adaptive tail overlap     |
| `CHUNKING_EMBED_METADATA_PREFIX` | `true`     | both          | Prepend `"Date: YYYY-MM-DD. Weekday."` to each chunk before embedding (stored document stays un-prefixed) |

## Optional — OCR provider

| Variable       | Default      | Description                                                                                                   |
| -------------- | ------------ | ------------------------------------------------------------------------------------------------------------- |
| `OCR_PROVIDER` | `anthropic`  | Which vision API to use for handwriting OCR. `"anthropic"` (Claude) or `"gemini"` (Google Gemini).            |
| `OCR_MODEL`    | per-provider | Model name sent to the selected provider. Defaults: `claude-opus-4-6` (anthropic), `gemini-2.5-pro` (gemini). |

When using `gemini`, the context-priming glossary (`OCR_CONTEXT_DIR`) is not applied — Gemini uses only the base system
prompt.

## Models (defaults, overridable via env vars or config.py)

| Variable / Setting     | Default                              | Description                                      |
| ---------------------- | ------------------------------------ | ------------------------------------------------ |
| `OCR_MODEL`            | `claude-opus-4-6` / `gemini-2.5-pro` | Vision model for OCR (depends on `OCR_PROVIDER`) |
| `transcription_model`  | `gpt-4o-transcribe`                  | OpenAI model for transcription                   |
| `embedding_model`      | `text-embedding-3-large`             | OpenAI model for embeddings                      |
| `embedding_dimensions` | `1024`                               | Embedding vector dimensions (reduced from 3072)  |

## Docker Compose

When running via Docker Compose, set API keys in a `.env` file in the project root or export them as environment
variables:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
export SLACK_BOT_TOKEN=xoxb-...   # optional, for Slack file URL ingestion
export OCR_PROVIDER=anthropic     # or "gemini"
export GOOGLE_API_KEY=...         # required only when OCR_PROVIDER=gemini
docker compose up
```

## Media VM Deployment

The `docker-compose.yml` is configured for the media VM stack:

- **MCP server** on port 8400 (avoids Gluetun's port 8000)
- **ChromaDB** on port 8401 (internal 8000)
- Bind mounts to `/srv/media/config/journal/{data,chromadb}`
- Image pulled from `ghcr.io/johnmathews/journal-server:latest`

MCP endpoint: `http://<media-vm-ip>:8400/mcp`

Create data directories before first run:

```bash
mkdir -p /srv/media/config/journal/{data,chromadb}
```
