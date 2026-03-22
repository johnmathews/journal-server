# Journal Analysis Tool

A personal journal insight engine that ingests journal entries and answers natural language queries about them.

The code for this project can be public but the journal entries themselves - the text, the images, the databses, must
stay private and be gitignored.

The github remote repo can be called "journal-agent".

## Ingestion

Two sources of journal data:

1. **Voice notes** - Transcribed using the OpenAI Whisper API (USD 0.006/min).
2. **Journal page images** - Text extracted using an LLM with vision capabilities. Currently Anthropic Claude Opus 4.6
   via the Anthropic API (required due to difficult handwriting). May switch to OpenAI or other providers in future.

All LLM and external API integrations must be abstracted behind provider-agnostic interfaces so that switching between
providers (e.g. Anthropic to OpenAI) requires only a new adapter, not changes to core logic.

## Storage

- **Structured database** for quantitative queries (frequency, averages, variance, counts).
- **Vector database with embeddings** for semantic queries (mood, topics, people, themes).
- **Embedding model**: OpenAI `text-embedding-3-large` via the OpenAI API. The embedding provider must also be abstracted
  behind an interface to allow switching models or providers.

## Query Examples

- "Which friends did I meet in February?"
- "Am I in a happier mood on Mondays compared to Thursdays?"
- "How often do I speak about Vienna?"
- "Who is Atlas?"
- "How many entries do I write on average per month?"
- "Quantify the variance in journal entry length."
- "What do I spend most time doing?"
- "What makes me most sad?"
- "What makes me happy?"

## Interfaces

- **API endpoints** - For a web UI frontend (out of scope for now).
- **MCP server** - For the Nanoclaw personal AI assistant, accessed from within a Slack channel.
- **CLI** - For direct command-line interaction.

## Architecture Principles

- Loose coupling throughout: ingestion, storage, querying, and interfaces should be independently swappable.
- All external service integrations (LLMs, speech-to-text, embeddings) must sit behind provider-agnostic interfaces.
  Concrete implementations are adapters that can be swapped without touching core logic.
- Flexibility for evolving requirements. Features and usage patterns will develop over time.

## Tech Stack and Tooling

- Python 3.13 (latest LTS)
- `uv` for dependency management
- `pytest` for testing, `coverage` for test coverage
- Logging throughout all components
- Documentation in `/docs`, dev journal in `/journal`

## External APIs and Authentication

All external APIs use pay-per-token/pay-per-use pricing via API keys. No subscription-based authentication is used for
programmatic access (Claude Max is used only for development via Claude Code, not for runtime API calls).

| API                          | Auth Method | Key Environment Variable |
| ---------------------------- | ----------- | ------------------------ |
| Anthropic (OCR)              | API key     | `ANTHROPIC_API_KEY`      |
| OpenAI (Whisper, embeddings) | API key     | `OPENAI_API_KEY`         |

## Cost Estimates

Based on ~3 A5 pages/day of small handwriting (~1,000 words/day of journal text).

### OCR via Anthropic Opus 4.6 API

| Period  | Input tokens | Input cost ($5/M) | Output tokens | Output cost ($25/M) | Total  |
| ------- | ------------ | ----------------- | ------------- | ------------------- | ------ |
| Daily   | ~3,900       | $0.02             | ~1,500        | $0.04               | $0.06  |
| Monthly | ~117,000     | $0.59             | ~45,000       | $1.13               | $1.71  |
| Yearly  | ~1.4M        | $7.12             | ~548K         | $13.69              | $20.81 |

Prompt caching (shared system prompt) can reduce input costs by up to 90%.

### Embeddings via OpenAI text-embedding-3-large

| Period  | Tokens   | Cost ($0.13/M) |
| ------- | -------- | -------------- |
| Monthly | ~45,000  | $0.006         |
| Yearly  | ~550,000 | $0.07          |

### Voice transcription via OpenAI Whisper

| Duration   | Cost ($0.006/min)      |
| ---------- | ---------------------- |
| 10 min/day | $0.06/day, $1.80/month |
| 30 min/day | $0.18/day, $5.40/month |

### Total estimated monthly cost

| Component                 | Monthly cost |
| ------------------------- | ------------ |
| OCR (Opus)                | ~$1.71       |
| Embeddings                | ~$0.01       |
| Whisper (10 min/day est.) | ~$1.80       |
| Vector DB (self-hosted)   | $0.00        |
| **Total**                 | **~$3.52**   |

No hidden costs. All APIs are pay-per-use with no minimums. Vector database is self-hosted within the Docker Compose
stack. The only recurring costs are the API calls above.

## Deployment

- Deployed as a service within a Docker Compose stack.
- Container image built via GitHub Actions and stored on ghcr.io.
