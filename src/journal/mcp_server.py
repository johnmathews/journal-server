"""MCP server for the journal analysis tool using FastMCP."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from journal.api import register_api_routes
from journal.config import load_config
from journal.db.connection import get_connection
from journal.db.migrations import run_migrations
from journal.db.repository import SQLiteEntryRepository
from journal.logging import setup_logging
from journal.providers.embeddings import OpenAIEmbeddingsProvider
from journal.providers.ocr import AnthropicOCRProvider
from journal.providers.transcription import OpenAITranscriptionProvider
from journal.services.chunking import build_chunker
from journal.services.ingestion import IngestionService
from journal.services.query import QueryService
from journal.vectorstore.store import ChromaVectorStore

log = logging.getLogger(__name__)

# Shared services — initialized once at startup, reused across all sessions and
# REST API requests. Both the MCP lifespan and the REST API routes access this.
_services: dict | None = None


def _init_services() -> dict:
    """Initialize shared services (DB, vector store, providers). Idempotent."""
    global _services
    if _services is not None:
        return _services

    setup_logging()
    config = load_config()

    log.info("Initializing services...")
    log.info("  DB path: %s", config.db_path)
    log.info("  ChromaDB: %s:%d", config.chromadb_host, config.chromadb_port)
    log.info("  MCP: %s:%d", config.mcp_host, config.mcp_port)

    # Database
    conn = get_connection(config.db_path)
    run_migrations(conn)
    repo = SQLiteEntryRepository(conn)
    log.info("  SQLite connected and migrated")

    # Vector store
    vector_store = ChromaVectorStore(
        host=config.chromadb_host,
        port=config.chromadb_port,
        collection_name=config.chromadb_collection,
    )
    log.info("  ChromaDB connected (collection=%s)", config.chromadb_collection)

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
    log.info("  Providers: OCR=%s, transcription=%s, embeddings=%s",
             config.ocr_model, config.transcription_model, config.embedding_model)

    chunker = build_chunker(config, embeddings)

    _services = {
        "ingestion": IngestionService(
            repository=repo,
            vector_store=vector_store,
            ocr_provider=ocr,
            transcription_provider=transcription,
            embeddings_provider=embeddings,
            chunker=chunker,
            slack_bot_token=config.slack_bot_token,
        ),
        "query": QueryService(
            repository=repo,
            vector_store=vector_store,
            embeddings_provider=embeddings,
        ),
    }

    entry_count = repo.count_entries()
    log.info("Services initialized (entries in DB: %d)", entry_count)
    return _services


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Yield shared services for MCP sessions."""
    yield _init_services()


mcp = FastMCP("journal", lifespan=lifespan)

# Register REST API routes — they access the shared _services dict directly.
register_api_routes(mcp, lambda: _services)


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
    log.info(
        "Tool call: journal_search_entries(query=%r, start_date=%s, end_date=%s)",
        query, start_date, end_date,
    )
    service = _get_query(ctx)
    results = service.search_entries(query, start_date, end_date, min(limit, 50), offset)

    if not results:
        return f"No journal entries found matching '{query}'."

    lines = [f"Found {len(results)} entries matching '{query}':\n"]
    for r in results:
        lines.append(
            f"--- {r.entry_date} (top relevance: {r.score:.0%}, "
            f"{len(r.matching_chunks)} matching chunk"
            f"{'s' if len(r.matching_chunks) != 1 else ''}) ---"
        )
        # Show every matching chunk (not just the top one) so LLM consumers
        # see all passages in this entry that were relevant to the query.
        for i, cm in enumerate(r.matching_chunks, start=1):
            snippet = cm.text[:200]
            ellipsis = "..." if len(cm.text) > 200 else ""
            lines.append(f"  match {i} ({cm.score:.0%}): {snippet}{ellipsis}")
        # Then the full parent entry for context.
        lines.append("")
        lines.append(r.text[:500])
        if len(r.text) > 500:
            lines.append(f"... ({len(r.text)} chars total)")
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
    log.info("Tool call: journal_get_entries_by_date(date=%s)", date)
    service = _get_query(ctx)
    entries = service.get_entries_by_date(date)

    if not entries:
        return f"No journal entries found for {date}."

    lines = [f"{len(entries)} entries for {date}:\n"]
    for e in entries:
        lines.append(f"--- Entry {e.id} ({e.source_type}, {e.word_count} words) ---")
        lines.append(e.final_text)
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
    log.info(
        "Tool call: journal_list_entries(start_date=%s, end_date=%s, limit=%d)",
        start_date, end_date, limit,
    )
    service = _get_query(ctx)
    entries = service.list_entries(start_date, end_date, min(limit, 50), offset)

    if not entries:
        return "No journal entries found."

    lines = [f"Showing {len(entries)} entries:\n"]
    for e in entries:
        preview = e.final_text[:100].replace("\n", " ")
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
    log.info("Tool call: journal_get_statistics(start_date=%s, end_date=%s)", start_date, end_date)
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
    log.info(
        "Tool call: journal_get_mood_trends(start_date=%s, end_date=%s, granularity=%s)",
        start_date, end_date, granularity,
    )
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
    log.info(
        "Tool call: journal_get_topic_frequency(topic=%r, start_date=%s, end_date=%s)",
        topic, start_date, end_date,
    )
    service = _get_query(ctx)
    freq = service.get_topic_frequency(topic, start_date, end_date)

    if freq.count == 0:
        return f"'{topic}' was not found in any journal entries."

    lines = [f"'{topic}' appears in {freq.count} entries:"]
    for e in freq.entries[:10]:
        preview = e.final_text[:80].replace("\n", " ")
        lines.append(f"  - {e.entry_date}: {preview}...")
    if freq.count > 10:
        lines.append(f"  ... and {freq.count - 10} more entries")
    return "\n".join(lines)


@mcp.tool()
def journal_ingest_from_url(
    source_type: str,
    url: str,
    media_type: str | None = None,
    date: str | None = None,
    language: str = "en",
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Ingest a journal entry by downloading an image or voice note from a URL.

    Preferred over journal_ingest_entry when the file is available at a URL,
    since it avoids base64-encoding large files as tool parameters.

    Args:
        source_type: Either "image" (for handwritten page OCR) or "voice" (for audio).
        url: URL to download the file from (must be accessible from the server).
        media_type: MIME type override. If omitted, inferred from the response header.
        date: Date of the journal entry (ISO 8601, e.g. "2026-03-22"). Defaults to today.
        language: Language code for voice transcription (default "en"). Ignored for images.
    """
    from datetime import date as date_type

    log.info(
        "Tool call: journal_ingest_from_url(source_type=%s, url=%s, date=%s)",
        source_type, url, date,
    )
    service = _get_ingestion(ctx)
    entry_date = date or date_type.today().isoformat()

    if source_type == "image":
        entry = service.ingest_image_from_url(url, entry_date, media_type)
    elif source_type == "voice":
        entry = service.ingest_voice_from_url(
            url, entry_date, media_type, language,
        )
    else:
        return f"Invalid source_type '{source_type}'. Must be 'image' or 'voice'."

    return (
        f"Entry ingested successfully.\n"
        f"  ID: {entry.id}\n"
        f"  Date: {entry.entry_date}\n"
        f"  Source: {entry.source_type}\n"
        f"  Words: {entry.word_count}\n"
        f"  Chunks: {entry.chunk_count}\n"
        f"  Preview: {entry.final_text[:200]}..."
    )


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

    log.info(
        "Tool call: journal_ingest_entry(source_type=%s, media_type=%s, date=%s, size=%d)",
        source_type, media_type, date, len(data_base64),
    )
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
        f"  Chunks: {entry.chunk_count}\n"
        f"  Preview: {entry.final_text[:200]}..."
    )


@mcp.tool()
def journal_ingest_multi_page(
    images_base64: list[str],
    media_types: list[str],
    date: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Ingest multiple page images as a single journal entry.

    Args:
        images_base64: List of base64-encoded image data, one per page in order.
        media_types: List of MIME types, one per image (e.g. ["image/jpeg", "image/jpeg"]).
        date: Date of the journal entry (ISO 8601). Defaults to today.
    """
    import base64
    from datetime import date as date_type

    log.info(
        "Tool call: journal_ingest_multi_page(pages=%d, date=%s)",
        len(images_base64), date,
    )
    service = _get_ingestion(ctx)
    entry_date = date or date_type.today().isoformat()

    if len(images_base64) != len(media_types):
        return "Error: images_base64 and media_types must have the same length."

    images = [
        (base64.b64decode(img), mt)
        for img, mt in zip(images_base64, media_types, strict=True)
    ]

    entry = service.ingest_multi_page_entry(images, entry_date)

    return (
        f"Multi-page entry ingested successfully.\n"
        f"  ID: {entry.id}\n"
        f"  Date: {entry.entry_date}\n"
        f"  Pages: {len(images)}\n"
        f"  Words: {entry.word_count}\n"
        f"  Chunks: {entry.chunk_count}\n"
        f"  Preview: {entry.final_text[:200]}..."
    )


@mcp.tool()
def journal_update_entry_text(
    entry_id: int,
    final_text: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Update the corrected text of a journal entry, triggering re-embedding.

    Use this to fix OCR errors. The original raw text is preserved; only the
    corrected version (final_text) is updated. All downstream features (search,
    analytics) will use the corrected text.

    Args:
        entry_id: The ID of the entry to update.
        final_text: The corrected text content.
    """
    log.info("Tool call: journal_update_entry_text(entry_id=%d)", entry_id)
    service = _get_ingestion(ctx)
    try:
        entry = service.update_entry_text(entry_id, final_text)
    except ValueError as e:
        return f"Error: {e}"

    return (
        f"Entry {entry.id} updated successfully.\n"
        f"  Words: {entry.word_count}\n"
        f"  Chunks: {entry.chunk_count}\n"
        f"  Preview: {entry.final_text[:200]}..."
    )


def main() -> None:
    """Run the MCP server with REST API and optional CORS."""
    import anyio
    import uvicorn
    from starlette.middleware.cors import CORSMiddleware

    config = load_config()
    mcp.settings.host = config.mcp_host
    mcp.settings.port = config.mcp_port

    if config.mcp_allowed_hosts:
        allowed_origins = [f"http://{h}" for h in config.mcp_allowed_hosts]
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=config.mcp_allowed_hosts,
            allowed_origins=allowed_origins,
        )
    else:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        )

    # Initialize services eagerly so REST API routes work immediately,
    # without waiting for the first MCP session to connect.
    _init_services()

    # Build the Starlette app from FastMCP (includes MCP routes + custom_routes)
    app = mcp.streamable_http_app()

    # Log registered routes for debugging
    for route in app.routes:
        methods = getattr(route, "methods", None)
        log.info("  Route: %s %s", route.path, methods or "(all)")

    # Add CORS middleware if configured
    if config.api_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.api_cors_origins,
            allow_methods=["GET", "PATCH", "OPTIONS"],
            allow_headers=["Content-Type"],
        )

    async def _serve() -> None:
        uvi_config = uvicorn.Config(
            app,
            host=config.mcp_host,
            port=config.mcp_port,
            log_level="info",
        )
        server = uvicorn.Server(uvi_config)
        await server.serve()

    anyio.run(_serve)


if __name__ == "__main__":
    main()
