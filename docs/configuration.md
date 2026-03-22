# Configuration

All configuration is via environment variables. No config files are needed.

## Required

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key for OCR (Claude Opus 4.6 vision) |
| `OPENAI_API_KEY` | OpenAI API key for Whisper transcription and embeddings |

## Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `journal.db` | Path to SQLite database file |
| `CHROMADB_HOST` | `localhost` | ChromaDB server hostname |
| `CHROMADB_PORT` | `8000` | ChromaDB server port |
| `MCP_HOST` | `0.0.0.0` | MCP server bind address |
| `MCP_PORT` | `8000` | MCP server port |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |

## Models (hardcoded defaults, changeable in config.py)

| Setting | Default | Description |
|---------|---------|-------------|
| `ocr_model` | `claude-opus-4-6` | Anthropic model for OCR |
| `transcription_model` | `gpt-4o-transcribe` | OpenAI model for transcription |
| `embedding_model` | `text-embedding-3-large` | OpenAI model for embeddings |
| `embedding_dimensions` | `1024` | Embedding vector dimensions (reduced from 3072) |
| `chunk_max_tokens` | `500` | Maximum tokens per text chunk |
| `chunk_overlap_tokens` | `100` | Token overlap between chunks |

## Docker Compose

When running via Docker Compose, set API keys in a `.env` file in the project root or export them as environment variables:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
docker compose up
```
