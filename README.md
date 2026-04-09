# Journal Analysis Tool

A personal journal insight engine that ingests handwritten journal pages and voice notes, then answers natural language
queries about them.

## What It Does

- **Ingests** handwritten journal pages (OCR via Claude Opus 4.6) and voice notes (transcription via OpenAI Whisper)
- **Stores** entries in dual databases: SQLite for structured queries, ChromaDB for semantic search
- **Answers** natural language questions like "Which friends did I meet in February?" or "What makes me happy?"
- **Interfaces**: MCP server (for AI assistants), CLI, and API endpoints

## Quick Start

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) for dependency management
- API keys for Anthropic and OpenAI
- Docker (for ChromaDB and deployment)

### Local Development

```bash
# Clone and install
git clone https://github.com/johnmathews/journal-server.git
cd journal-server
uv sync

# Set up environment
cp .env.example .env  # Edit with your API keys

# Run tests
uv run pytest

# Start ChromaDB locally
docker run -d --name chromadb -p 8000:8000 -v ./chroma-data:/data chromadb/chroma:1.5.5

# Use the CLI
uv run journal ingest page.jpg --date 2026-03-22
uv run journal search "meetings with Atlas"
uv run journal stats
```

### Docker Deployment

```bash
# Set API keys
export ANTHROPIC_API_KEY=your-key
export OPENAI_API_KEY=your-key

# Start the full stack
docker compose up -d
```

This starts:

- **Journal MCP server** on port 8000 (streamable HTTP)
- **ChromaDB** on port 8001

## Architecture

```
                    MCP Client (Nanoclaw)
                          |
                    MCP Server (FastMCP)
                          |
              +-----------+-----------+
              |                       |
        Query Service          Ingestion Service
              |                       |
    +---------+---------+    +--------+--------+
    |         |         |    |        |        |
  SQLite   ChromaDB  Embed  OCR   Whisper   Embed
  (FTS5)   (vectors)  API   API    API      API
```

All external APIs are behind provider-agnostic interfaces (Python Protocols), making it easy to swap providers.

## Documentation

- [Architecture](docs/architecture.md) — System design and data flow
- [Configuration](docs/configuration.md) — Environment variables reference
- [Development](docs/development.md) — Local setup and contributing
- [API Reference](docs/api.md) — MCP tool documentation

## Cost

~$3.52/month for ~3 handwritten pages/day + 10 min voice notes. See [project-brief.md](project-brief.md) for detailed
estimates.
