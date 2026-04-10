# Configuration

All configuration is via environment variables. No config files are needed.

## Required

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key for OCR (Claude Opus 4.6 vision) |
| `OPENAI_API_KEY` | OpenAI API key for Whisper transcription and embeddings |

## Optional — deployment

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `journal.db` | Path to SQLite database file |
| `CHROMADB_HOST` | `localhost` | ChromaDB server hostname |
| `CHROMADB_PORT` | `8000` | ChromaDB server port |
| `MCP_HOST` | `0.0.0.0` | MCP server bind address |
| `MCP_PORT` | `8000` | MCP server port (use 8400 on media VM to avoid Gluetun conflict) |
| `SLACK_BOT_TOKEN` | | Slack bot token for downloading files from Slack URLs |
| `API_CORS_ORIGINS` | | Comma-separated list of allowed CORS origins for the REST API (e.g., `http://localhost:5173`). Empty disables CORS. |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |

## Optional — chunking

See `docs/architecture.md` → "Chunking Strategies" for the algorithm and tradeoffs.

| Variable                          | Default    | Applies to    | Description |
|-----------------------------------|------------|---------------|-------------|
| `CHUNKING_STRATEGY`               | `semantic` | both          | `"fixed"` or `"semantic"` |
| `CHUNKING_MAX_TOKENS`             | `150`      | both          | Upper bound for chunk size |
| `CHUNKING_OVERLAP_TOKENS`         | `40`       | fixed only    | Tokens carried between adjacent chunks |
| `CHUNKING_MIN_TOKENS`             | `30`       | semantic only | Minimum chunk size; smaller segments are merged |
| `CHUNKING_BOUNDARY_PERCENTILE`    | `25`       | semantic only | Adjacent similarities at/below this percentile are cut positions |
| `CHUNKING_DECISIVE_PERCENTILE`    | `10`       | semantic only | Cuts at/below this are clean (no overlap); between 10 and 25 are weak cuts with adaptive tail overlap |
| `CHUNKING_EMBED_METADATA_PREFIX`  | `true`     | both          | Prepend `"Date: YYYY-MM-DD. Weekday."` to each chunk before embedding (stored document stays un-prefixed) |

## Models (hardcoded defaults, changeable in config.py)

| Setting | Default | Description |
|---------|---------|-------------|
| `ocr_model` | `claude-opus-4-6` | Anthropic model for OCR |
| `transcription_model` | `gpt-4o-transcribe` | OpenAI model for transcription |
| `embedding_model` | `text-embedding-3-large` | OpenAI model for embeddings |
| `embedding_dimensions` | `1024` | Embedding vector dimensions (reduced from 3072) |

## Docker Compose

When running via Docker Compose, set API keys in a `.env` file in the project root or export them as environment variables:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
export SLACK_BOT_TOKEN=xoxb-...  # optional, for Slack file URL ingestion
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
