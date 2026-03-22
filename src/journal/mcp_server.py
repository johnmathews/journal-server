"""MCP server for the journal analysis tool using FastMCP."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.fastmcp import Context, FastMCP

from journal.config import load_config
from journal.db.connection import get_connection
from journal.db.migrations import run_migrations
from journal.db.repository import SQLiteEntryRepository
from journal.logging import setup_logging
from journal.providers.embeddings import OpenAIEmbeddingsProvider
from journal.providers.ocr import AnthropicOCRProvider
from journal.providers.transcription import OpenAITranscriptionProvider
from journal.services.ingestion import IngestionService
from journal.services.query import QueryService
from journal.vectorstore.store import ChromaVectorStore

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Initialize services on startup, clean up on shutdown."""
    setup_logging()
    config = load_config()

    # Database
    conn = get_connection(config.db_path)
    run_migrations(conn)
    repo = SQLiteEntryRepository(conn)

    # Vector store
    vector_store = ChromaVectorStore(
        host=config.chromadb_host,
        port=config.chromadb_port,
        collection_name=config.chromadb_collection,
    )

    # Providers
    ocr = AnthropicOCRProvider(
        api_key=config.anthropic_api_key,
        model=config.ocr_model,
        max_tokens=config.ocr_max_tokens,
    )
    transcription = OpenAITranscriptionProvider(
        api_key=config.openai_api_key,
        model=config.transcription_model,
    )
    embeddings = OpenAIEmbeddingsProvider(
        api_key=config.openai_api_key,
        model=config.embedding_model,
        dimensions=config.embedding_dimensions,
    )

    # Services
    ingestion_service = IngestionService(
        repository=repo,
        vector_store=vector_store,
        ocr_provider=ocr,
        transcription_provider=transcription,
        embeddings_provider=embeddings,
        chunk_max_tokens=config.chunk_max_tokens,
        chunk_overlap_tokens=config.chunk_overlap_tokens,
    )
    query_service = QueryService(
        repository=repo,
        vector_store=vector_store,
        embeddings_provider=embeddings,
    )

    log.info("Journal MCP server initialized")

    try:
        yield {
            "ingestion": ingestion_service,
            "query": query_service,
        }
    finally:
        conn.close()
        log.info("Journal MCP server shut down")


mcp = FastMCP("journal", lifespan=lifespan)


def _get_query(ctx: Context) -> QueryService:
    return ctx.request_context.lifespan_context["query"]


def _get_ingestion(ctx: Context) -> IngestionService:
    return ctx.request_context.lifespan_context["ingestion"]


@mcp.tool()
def journal_search_entries(
    query: str,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 10,
    offset: int = 0,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Search journal entries using semantic similarity.

    Args:
        query: Natural language query (e.g. "times I felt happy", "meetings with Atlas")
        start_date: Filter entries from this date (ISO 8601, e.g. "2026-01-01"). Optional.
        end_date: Filter entries until this date (ISO 8601, e.g. "2026-03-01"). Optional.
        limit: Max results to return (1-50, default 10).
        offset: Pagination offset for retrieving more results.
    """
    service = _get_query(ctx)
    results = service.search_entries(query, start_date, end_date, min(limit, 50), offset)

    if not results:
        return f"No journal entries found matching '{query}'."

    lines = [f"Found {len(results)} entries matching '{query}':\n"]
    for r in results:
        lines.append(f"--- {r.entry_date} (relevance: {r.score:.0%}) ---")
        # Show the matching chunk for context, then the full entry
        if r.chunk_text and r.chunk_text != r.raw_text:
            lines.append(f"Best match: ...{r.chunk_text[:200]}...")
        lines.append(r.raw_text[:500])
        if len(r.raw_text) > 500:
            lines.append(f"... ({len(r.raw_text)} chars total)")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def journal_get_entries_by_date(
    date: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Get all journal entries for a specific date.

    Args:
        date: Date in ISO 8601 format (e.g. "2026-03-22").
    """
    service = _get_query(ctx)
    entries = service.get_entries_by_date(date)

    if not entries:
        return f"No journal entries found for {date}."

    lines = [f"{len(entries)} entries for {date}:\n"]
    for e in entries:
        lines.append(f"--- Entry {e.id} ({e.source_type}, {e.word_count} words) ---")
        lines.append(e.raw_text)
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def journal_list_entries(
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 20,
    offset: int = 0,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """List journal entries in reverse chronological order.

    Args:
        start_date: Filter from this date (ISO 8601). Optional.
        end_date: Filter until this date (ISO 8601). Optional.
        limit: Max results (1-50, default 20).
        offset: Pagination offset.
    """
    service = _get_query(ctx)
    entries = service.list_entries(start_date, end_date, min(limit, 50), offset)

    if not entries:
        return "No journal entries found."

    lines = [f"Showing {len(entries)} entries:\n"]
    for e in entries:
        preview = e.raw_text[:100].replace("\n", " ")
        lines.append(f"- {e.entry_date} | {e.source_type} | {e.word_count} words | {preview}...")
    return "\n".join(lines)


@mcp.tool()
def journal_get_statistics(
    start_date: str | None = None,
    end_date: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Get journal statistics: entry count, frequency, average length, date range.

    Args:
        start_date: Start of period (ISO 8601). Defaults to all time.
        end_date: End of period (ISO 8601). Defaults to today.
    """
    service = _get_query(ctx)
    stats = service.get_statistics(start_date, end_date)

    lines = ["Journal Statistics:"]
    lines.append(f"  Total entries: {stats.total_entries}")
    start = stats.date_range_start or "N/A"
    end = stats.date_range_end or "N/A"
    lines.append(f"  Date range: {start} to {end}")
    lines.append(f"  Total words: {stats.total_words:,}")
    lines.append(f"  Average words per entry: {stats.avg_words_per_entry:.0f}")
    lines.append(f"  Entries per month: {stats.entries_per_month:.1f}")
    return "\n".join(lines)


@mcp.tool()
def journal_get_mood_trends(
    start_date: str | None = None,
    end_date: str | None = None,
    granularity: str = "week",
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Analyze mood trends over time from journal entries.

    Args:
        start_date: Start of period (ISO 8601). Defaults to 3 months ago.
        end_date: End of period (ISO 8601). Defaults to today.
        granularity: Time grouping - "day", "week", or "month" (default "week").
    """
    service = _get_query(ctx)
    trends = service.get_mood_trends(start_date, end_date, granularity)

    if not trends:
        return "No mood data available for the specified period."

    lines = [f"Mood trends by {granularity}:\n"]
    for t in trends:
        bar = "+" * max(1, int((t.avg_score + 1) * 5))
        line = f"  {t.period} | {t.dimension}: {t.avg_score:+.2f} ({t.entry_count} entries) {bar}"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool()
def journal_get_topic_frequency(
    topic: str,
    start_date: str | None = None,
    end_date: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Count how often a topic, person, or place appears in journal entries.

    Args:
        topic: Topic, person, place, or theme to search for (e.g. "Vienna", "Atlas", "work").
        start_date: Start of period (ISO 8601). Optional.
        end_date: End of period (ISO 8601). Optional.
    """
    service = _get_query(ctx)
    freq = service.get_topic_frequency(topic, start_date, end_date)

    if freq.count == 0:
        return f"'{topic}' was not found in any journal entries."

    lines = [f"'{topic}' appears in {freq.count} entries:"]
    for e in freq.entries[:10]:
        preview = e.raw_text[:80].replace("\n", " ")
        lines.append(f"  - {e.entry_date}: {preview}...")
    if freq.count > 10:
        lines.append(f"  ... and {freq.count - 10} more entries")
    return "\n".join(lines)


@mcp.tool()
def journal_ingest_entry(
    source_type: str,
    data_base64: str,
    media_type: str,
    date: str | None = None,
    language: str = "en",
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Ingest a journal entry from an image or voice note.

    Args:
        source_type: Either "image" (for handwritten page OCR) or "voice" (for audio transcription).
        data_base64: Base64-encoded file data.
        media_type: MIME type (e.g. "image/jpeg", "image/png", "audio/mp3", "audio/m4a").
        date: Date of the journal entry (ISO 8601, e.g. "2026-03-22"). Defaults to today.
        language: Language code for voice transcription (default "en"). Ignored for images.
    """
    import base64
    from datetime import date as date_type

    service = _get_ingestion(ctx)
    data = base64.b64decode(data_base64)
    entry_date = date or date_type.today().isoformat()

    if source_type == "image":
        entry = service.ingest_image(data, media_type, entry_date)
    elif source_type == "voice":
        entry = service.ingest_voice(data, media_type, entry_date, language)
    else:
        return f"Invalid source_type '{source_type}'. Must be 'image' or 'voice'."

    return (
        f"Entry ingested successfully.\n"
        f"  ID: {entry.id}\n"
        f"  Date: {entry.entry_date}\n"
        f"  Source: {entry.source_type}\n"
        f"  Words: {entry.word_count}\n"
        f"  Preview: {entry.raw_text[:200]}..."
    )


def main() -> None:
    """Run the MCP server."""
    config = load_config()
    mcp.run(transport="streamable-http", host=config.mcp_host, port=config.mcp_port)


if __name__ == "__main__":
    main()
