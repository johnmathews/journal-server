"""MCP server for the journal analysis tool using FastMCP."""

import atexit
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from journal.providers.formatter import FormatterProtocol

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from journal.api import register_api_routes
from journal.auth import get_current_user_id
from journal.auth_api import register_admin_routes, register_auth_routes
from journal.config import load_config
from journal.db.connection import get_connection
from journal.db.jobs_repository import SQLiteJobRepository
from journal.db.migrations import run_migrations
from journal.db.repository import SQLiteEntryRepository
from journal.entitystore.store import SQLiteEntityStore
from journal.logging import setup_logging
from journal.providers.embeddings import OpenAIEmbeddingsProvider
from journal.providers.extraction import AnthropicExtractionProvider
from journal.providers.ocr import build_ocr_provider
from journal.providers.transcription import OpenAITranscriptionProvider
from journal.services.backfill import backfill_mood_scores
from journal.services.chunking import build_chunker
from journal.services.entity_extraction import EntityExtractionService
from journal.services.ingestion import IngestionService
from journal.services.jobs import JobRunner
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
    #
    # `check_same_thread=False` lets the background JobRunner worker
    # thread share this process-wide connection with the REST/MCP
    # request handlers. Safety rests on one invariant: the JobRunner
    # uses a single-worker ThreadPoolExecutor, so at most one
    # background thread ever writes through this connection. Combined
    # with WAL + NORMAL synchronous, that is the documented safe
    # configuration for cross-thread SQLite use.
    #
    # See `journal.services.jobs.JobRunner` docstring and
    # `journal.db.connection.get_connection` docstring before changing
    # this — bumping `max_workers` above 1 is a serious change that
    # requires redesigning the threading model first.
    conn = get_connection(config.db_path, check_same_thread=False)
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
    ocr = build_ocr_provider(config)
    transcription = OpenAITranscriptionProvider(
        api_key=config.openai_api_key,
        model=config.transcription_model,
        confidence_threshold=config.transcription_confidence_threshold,
    )
    embeddings = OpenAIEmbeddingsProvider(
        api_key=config.openai_api_key,
        model=config.embedding_model,
        dimensions=config.embedding_dimensions,
    )
    log.info("  Providers: OCR=%s%s (%s), transcription=%s, embeddings=%s",
             config.ocr_provider,
             " [dual-pass]" if config.ocr_dual_pass else "",
             config.ocr_model or "default",
             config.transcription_model, config.embedding_model)
    if config.preprocess_images:
        log.info("  Image preprocessing: enabled")

    chunker = build_chunker(config, embeddings)

    entity_store = SQLiteEntityStore(conn)
    extraction_provider = AnthropicExtractionProvider(
        api_key=config.anthropic_api_key,
        model=config.entity_extraction_model,
        max_tokens=config.entity_extraction_max_tokens,
    )

    # One in-process stats collector for the lifetime of the server.
    # `QueryService` methods record a sample on every call; `/health`
    # reads a snapshot on demand.
    from journal.services.stats import InMemoryStatsCollector

    stats_collector = InMemoryStatsCollector()

    # Optional mood-scoring pipeline. Loaded only when the user
    # explicitly opts in via `JOURNAL_ENABLE_MOOD_SCORING=true`.
    # Mis-configured dimensions fail loudly at startup — silent
    # degradation to "no scoring" is a worse failure mode than a
    # server refusing to start.
    mood_scoring_service: Any = None
    mood_dimensions: tuple = ()
    if config.enable_mood_scoring:
        from journal.providers.mood_scorer import AnthropicMoodScorer
        from journal.services.mood_dimensions import load_mood_dimensions
        from journal.services.mood_scoring import MoodScoringService

        mood_dimensions = load_mood_dimensions(config.mood_dimensions_path)
        mood_scorer = AnthropicMoodScorer(
            api_key=config.anthropic_api_key,
            model=config.mood_scorer_model,
            max_tokens=config.mood_scorer_max_tokens,
        )
        mood_scoring_service = MoodScoringService(
            scorer=mood_scorer,
            repository=repo,
            dimensions=mood_dimensions,
        )
        log.info(
            "Mood scoring enabled: model=%s, dimensions=%d",
            config.mood_scorer_model,
            len(mood_dimensions),
        )
    else:
        log.info(
            "Mood scoring disabled "
            "(JOURNAL_ENABLE_MOOD_SCORING unset or false)"
        )

    # User repository — created early so entity extraction can look up
    # per-user display names for the LLM author prompt.
    from journal.db.user_repository import SQLiteUserRepository

    user_repo = SQLiteUserRepository(conn)

    entity_extraction_service = EntityExtractionService(
        repository=repo,
        entity_store=entity_store,
        extraction_provider=extraction_provider,
        embeddings_provider=embeddings,
        author_name=config.journal_author_name,
        dedup_similarity_threshold=config.entity_dedup_similarity_threshold,
        user_repo=user_repo,
    )

    # Runtime settings — editable from the webapp without restart.
    from journal.services.runtime_settings import RuntimeSettings

    def _build_formatter(cfg, rs):  # type: ignore[no-untyped-def]
        """Build a transcript formatter if the runtime setting is enabled."""
        if not rs.get("transcript_formatting"):
            return None
        from journal.providers.formatter import AnthropicFormatter
        return AnthropicFormatter(
            api_key=cfg.anthropic_api_key,
            model=cfg.transcript_formatter_model,
        )

    def _on_runtime_setting_change(key: str, value: Any) -> None:
        """Side-effect callback: rebuild OCR provider when relevant settings change."""
        if key in ("ocr_dual_pass", "ocr_provider"):
            from dataclasses import replace

            # Build a temporary Config with the runtime value overridden
            # so build_ocr_provider sees the new setting.
            patched = replace(config, **{key: value})
            # Also apply the other OCR-related runtime setting
            other_key = "ocr_dual_pass" if key == "ocr_provider" else "ocr_provider"
            patched = replace(patched, **{other_key: runtime_settings.get(other_key)})
            new_ocr = build_ocr_provider(patched)
            ingestion_service._ocr = new_ocr
            log.info("OCR provider rebuilt due to runtime setting change: %s=%r", key, value)
        elif key == "preprocess_images":
            ingestion_service._preprocess_images = value
            log.info("Preprocessing %s via runtime settings", "enabled" if value else "disabled")
        elif key == "enable_mood_scoring":
            if value:
                from journal.providers.mood_scorer import AnthropicMoodScorer
                from journal.services.mood_dimensions import load_mood_dimensions
                from journal.services.mood_scoring import MoodScoringService

                dims = load_mood_dimensions(config.mood_dimensions_path)
                scorer = AnthropicMoodScorer(
                    api_key=config.anthropic_api_key,
                    model=config.mood_scorer_model,
                    max_tokens=config.mood_scorer_max_tokens,
                )
                svc = MoodScoringService(
                    scorer=scorer,
                    repository=repo,
                    dimensions=dims,
                )
                ingestion_service._mood_scoring = svc
                job_runner._mood_scoring = svc
                log.info("Mood scoring enabled via runtime settings")
            else:
                ingestion_service._mood_scoring = None
                job_runner._mood_scoring = None
                log.info("Mood scoring disabled via runtime settings")
        elif key == "transcript_formatting":
            if value:
                from journal.providers.formatter import AnthropicFormatter

                ingestion_service._formatter = AnthropicFormatter(
                    api_key=config.anthropic_api_key,
                    model=config.transcript_formatter_model,
                )
                log.info("Transcript formatting enabled via runtime settings")
            else:
                ingestion_service._formatter = None
                log.info("Transcript formatting disabled via runtime settings")

    runtime_settings = RuntimeSettings(conn, config, on_change=_on_runtime_setting_change)
    log.info("  Runtime settings loaded")

    # Ingestion service — created before the JobRunner so the runner
    # can delegate image-ingestion jobs to it on the background thread.
    ingestion_service = IngestionService(
        repository=repo,
        vector_store=vector_store,
        ocr_provider=ocr,
        transcription_provider=transcription,
        embeddings_provider=embeddings,
        chunker=chunker,
        slack_bot_token=config.slack_bot_token,
        embed_metadata_prefix=config.chunking_embed_metadata_prefix,
        preprocess_images=runtime_settings.get("preprocess_images"),
        mood_scoring=mood_scoring_service,
        formatter=_build_formatter(config, runtime_settings),
    )

    # Jobs infrastructure: repository + single-worker runner. Must
    # share `conn` (opened with check_same_thread=False above). The
    # runner serialises worker writes to one thread at a time.
    job_repository = SQLiteJobRepository(conn)
    reconciled = job_repository.reconcile_stuck_jobs()
    log.info(
        "  Jobs: reconciled %d stuck job(s) from previous process",
        reconciled,
    )
    job_runner = JobRunner(
        job_repository=job_repository,
        entity_extraction_service=entity_extraction_service,
        mood_backfill_callable=backfill_mood_scores,
        mood_scoring_service=mood_scoring_service,
        entry_repository=repo,
        ingestion_service=ingestion_service,
    )
    log.info("  Jobs: JobRunner started (single-worker executor)")

    # Shutdown hook — FastMCP's lifespan is per-session, not
    # per-process, so `atexit` is the honest hook here. `wait=False`
    # so an unresponsive job cannot block process exit; the
    # reconcile_stuck_jobs call on the next boot will clean up any
    # row left mid-flight.
    def _shutdown_job_runner() -> None:
        # Deliberately quiet: atexit runs arbitrarily late (often
        # after pytest or uvicorn has closed stdout/stderr), so any
        # `log.info` here reliably triggers a spurious "I/O on
        # closed file" print from the stdlib logging handler. The
        # JobRunner already logs its own shutdown lifecycle.
        job_runner.shutdown(wait=False)

    atexit.register(_shutdown_job_runner)

    # Auth infrastructure — auth service, optional email.
    # (user_repo already created above for entity extraction.)
    from journal.services.auth import AuthService
    from journal.services.email import EmailService

    auth_service = AuthService(
        user_repo=user_repo,
        secret_key=config.secret_key,
        session_expiry_days=config.session_expiry_days,
    )
    log.info("  Auth service initialized")

    email_service: EmailService | None = None
    if config.smtp_username and config.smtp_password:
        email_service = EmailService(
            smtp_host=config.smtp_host,
            smtp_port=config.smtp_port,
            smtp_username=config.smtp_username,
            smtp_password=config.smtp_password,
            from_email=config.smtp_from_email,
        )
        log.info("  Email service initialized (from=%s)", config.smtp_from_email)
    else:
        log.info("  Email service disabled (SMTP credentials not configured)")

    _services = {
        "ingestion": ingestion_service,
        "query": QueryService(
            repository=repo,
            vector_store=vector_store,
            embeddings_provider=embeddings,
            stats=stats_collector,
        ),
        "entity_store": entity_store,
        "entity_extraction": entity_extraction_service,
        "job_repository": job_repository,
        "job_runner": job_runner,
        "config": config,
        "runtime_settings": runtime_settings,
        "stats": stats_collector,
        "mood_dimensions": mood_dimensions,
        "mood_scoring": mood_scoring_service,
        # Auth services — used by auth_api.py routes.
        "auth_service": auth_service,
        "email_service": email_service,
        "user_repo": user_repo,
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
register_auth_routes(mcp, lambda: _services)
register_admin_routes(mcp, lambda: _services)


def _get_query(ctx: Context) -> QueryService:
    return ctx.request_context.lifespan_context["query"]


def _get_ingestion(ctx: Context) -> IngestionService:
    return ctx.request_context.lifespan_context["ingestion"]


def _get_entity_extraction(ctx: Context) -> EntityExtractionService:
    return ctx.request_context.lifespan_context["entity_extraction"]


def _get_entity_store(ctx: Context) -> SQLiteEntityStore:
    return ctx.request_context.lifespan_context["entity_store"]


def _get_job_runner(ctx: Context) -> JobRunner:
    return ctx.request_context.lifespan_context["job_runner"]


def _get_job_repository(ctx: Context) -> SQLiteJobRepository:
    return ctx.request_context.lifespan_context["job_repository"]


def _user_id(ctx: Context) -> int:
    """Return the authenticated user_id for the current MCP request."""
    return get_current_user_id()


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
    user_id = _user_id(ctx)
    results = service.search_entries(
        query, start_date, end_date, min(limit, 50), offset, user_id=user_id,
    )

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
    user_id = _user_id(ctx)
    entries = service.get_entries_by_date(date, user_id=user_id)

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
    user_id = _user_id(ctx)
    entries = service.list_entries(start_date, end_date, min(limit, 50), offset, user_id=user_id)

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
    user_id = _user_id(ctx)
    stats = service.get_statistics(start_date, end_date, user_id=user_id)

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
    user_id = _user_id(ctx)
    trends = service.get_mood_trends(start_date, end_date, granularity, user_id=user_id)

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
    user_id = _user_id(ctx)
    freq = service.get_topic_frequency(topic, start_date, end_date, user_id=user_id)

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
def journal_ingest_media_from_url(
    source_type: str,
    url: str,
    media_type: str | None = None,
    date: str | None = None,
    language: str = "en",
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Ingest a SINGLE journal page image or voice note by URL.

    Use this for media files (images for OCR, audio for transcription).
    For plain text entries, use `journal_ingest_text` instead.

    Preferred over journal_ingest_media when the file is available at a URL,
    since it avoids base64-encoding large files as tool parameters.

    IMPORTANT: If you have multiple photos that are pages of the *same* journal
    entry (e.g. a two-page handwritten entry split across two images), do NOT
    call this tool once per image — that creates a separate entry per photo.
    Use `journal_ingest_multi_page_from_url` instead so all pages are combined
    into one entry.

    Args:
        source_type: Either "image" (for handwritten page OCR) or "voice" (for audio).
        url: URL to download the file from (must be accessible from the server).
        media_type: MIME type override. If omitted, inferred from the response header.
        date: Date of the journal entry (ISO 8601, e.g. "2026-03-22"). Defaults to today.
        language: Language code for voice transcription (default "en"). Ignored for images.
    """
    from datetime import date as date_type

    log.info(
        "Tool call: journal_ingest_media_from_url(source_type=%s, url=%s, date=%s)",
        source_type, url, date,
    )
    service = _get_ingestion(ctx)
    user_id = _user_id(ctx)
    entry_date = date or date_type.today().isoformat()

    if source_type == "image":
        entry = service.ingest_image_from_url(url, entry_date, media_type, user_id=user_id)
    elif source_type == "voice":
        entry = service.ingest_voice_from_url(
            url, entry_date, media_type, language, user_id=user_id,
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
def journal_ingest_media(
    source_type: str,
    data_base64: str,
    media_type: str,
    date: str | None = None,
    language: str = "en",
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Ingest a journal entry from a base64-encoded image or voice note.

    Use this for media files (images for OCR, audio for transcription).
    For plain text entries, use `journal_ingest_text` instead.
    When the file is available at a URL, prefer `journal_ingest_media_from_url`.

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
        "Tool call: journal_ingest_media(source_type=%s, media_type=%s, date=%s, size=%d)",
        source_type, media_type, date, len(data_base64),
    )
    service = _get_ingestion(ctx)
    user_id = _user_id(ctx)
    data = base64.b64decode(data_base64)
    entry_date = date or date_type.today().isoformat()

    if source_type == "image":
        entry = service.ingest_image(data, media_type, entry_date, user_id=user_id)
    elif source_type == "voice":
        entry = service.ingest_voice(data, media_type, entry_date, language, user_id=user_id)
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
def journal_ingest_text(
    text: str,
    date: str | None = None,
    source_type: str = "text_entry",
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Create a journal entry from plain text.

    Use this when you already have the text content (typed, dictated, or
    pasted). No OCR or transcription is performed — the text is stored
    directly, chunked, embedded, and indexed.

    For handwritten page images use `journal_ingest_media` or
    `journal_ingest_media_from_url`. For audio recordings use those same
    tools with source_type="voice".

    Args:
        text: The journal entry text content.
        date: Date of the journal entry (ISO 8601, e.g. "2026-03-22").
            Defaults to today.
        source_type: Entry source type. Defaults to "text_entry".
    """
    from datetime import date as date_type

    log.info(
        "Tool call: journal_ingest_text(date=%s, source_type=%s, chars=%d)",
        date, source_type, len(text),
    )
    service = _get_ingestion(ctx)
    user_id = _user_id(ctx)
    entry_date = date or date_type.today().isoformat()

    try:
        entry = service.ingest_text(
            text, entry_date, source_type, skip_mood=True, user_id=user_id,
        )
    except ValueError as e:
        return f"Error: {e}"

    return (
        f"Text entry created successfully.\n"
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
    user_id = _user_id(ctx)
    entry_date = date or date_type.today().isoformat()

    if len(images_base64) != len(media_types):
        return "Error: images_base64 and media_types must have the same length."

    images = [
        (base64.b64decode(img), mt)
        for img, mt in zip(images_base64, media_types, strict=True)
    ]

    entry = service.ingest_multi_page_entry(images, entry_date, user_id=user_id)

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
def journal_ingest_multi_page_from_url(
    urls: list[str],
    media_types: list[str] | None = None,
    date: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Ingest multiple page images (by URL) as a single multi-page journal entry.

    Use this tool when a single journal entry spans multiple photos — for
    example, a two-page handwritten entry where each page was photographed
    separately. All images are downloaded, OCR'd page-by-page, and combined
    into ONE entry with one page record per image. This is the preferred
    way to ingest multi-page entries from MCP clients like Slack-driven
    agents, since it avoids base64-encoding large files.

    Slack file URLs (files.slack.com) are automatically authenticated via
    the server's SLACK_BOT_TOKEN.

    Args:
        urls: Ordered list of page image URLs, one per page.
        media_types: Optional per-URL MIME type overrides. If provided,
            must be the same length as `urls`. Omit entirely to infer
            each page's MIME type from the response Content-Type header
            (usually correct for Slack and most CDNs).
        date: Date of the journal entry (ISO 8601, e.g. "2026-03-22").
            Defaults to today.
    """
    from datetime import date as date_type

    log.info(
        "Tool call: journal_ingest_multi_page_from_url(pages=%d, date=%s)",
        len(urls), date,
    )
    service = _get_ingestion(ctx)
    user_id = _user_id(ctx)
    entry_date = date or date_type.today().isoformat()

    if media_types is not None and len(media_types) != len(urls):
        return "Error: media_types and urls must have the same length when media_types is provided."

    # Service layer accepts list[str | None] | None; list[str] is a valid
    # instance of that type, so no conversion is needed.
    try:
        entry = service.ingest_multi_page_entry_from_urls(
            urls, entry_date, media_types, user_id=user_id,  # type: ignore[arg-type]
        )
    except ValueError as e:
        return f"Error: {e}"

    return (
        f"Multi-page entry ingested successfully.\n"
        f"  ID: {entry.id}\n"
        f"  Date: {entry.entry_date}\n"
        f"  Pages: {len(urls)}\n"
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
    user_id = _user_id(ctx)
    try:
        entry = service.update_entry_text(entry_id, final_text, user_id=user_id)
    except ValueError as e:
        return f"Error: {e}"

    return (
        f"Entry {entry.id} updated successfully.\n"
        f"  Words: {entry.word_count}\n"
        f"  Chunks: {entry.chunk_count}\n"
        f"  Preview: {entry.final_text[:200]}..."
    )


@mcp.tool()
def journal_extract_entities(
    entry_id: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    stale_only: bool = False,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Run the entity extraction batch job over one or more entries.

    Args:
        entry_id: If provided, run extraction for this single entry only.
        start_date: Filter entries from this date (ISO 8601). Optional.
        end_date: Filter entries until this date (ISO 8601). Optional.
        stale_only: When True, only process entries flagged as stale
            (text updated since the last extraction run).
    """
    log.info(
        "Tool call: journal_extract_entities("
        "entry_id=%s, start_date=%s, end_date=%s, stale_only=%s)",
        entry_id, start_date, end_date, stale_only,
    )
    service = _get_entity_extraction(ctx)
    user_id = _user_id(ctx)
    try:
        if entry_id is not None:
            results = [service.extract_from_entry(entry_id)]
        else:
            results = service.extract_batch(
                start_date=start_date,
                end_date=end_date,
                stale_only=stale_only,
                user_id=user_id,
            )
    except ValueError as e:
        return f"Error: {e}"

    if not results:
        return "No entries matched the filter — nothing to extract."

    total_new = sum(r.entities_created for r in results)
    total_matched = sum(r.entities_matched for r in results)
    total_mentions = sum(r.mentions_created for r in results)
    total_rels = sum(r.relationships_created for r in results)
    warnings = [w for r in results for w in r.warnings]

    lines = [
        f"Extraction complete for {len(results)} entries:",
        f"  Entities created: {total_new}",
        f"  Entities matched: {total_matched}",
        f"  Mentions recorded: {total_mentions}",
        f"  Relationships recorded: {total_rels}",
    ]
    if warnings:
        lines.append(f"  Warnings: {len(warnings)}")
        for w in warnings[:20]:
            lines.append(f"    - {w}")
        if len(warnings) > 20:
            lines.append(f"    ... and {len(warnings) - 20} more")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Async batch-job tool wrappers.
#
# These three tools drive the same JobRunner that the REST endpoints
# use. The batch tools block on the MCP call until the job reaches a
# terminal state — because they poll the jobs table rather than wait
# on a future, they work across the shared process-wide executor the
# same way the webapp's REST polling does. Failed jobs still return a
# structured dict (not an exception) so Claude can read the error
# message and respond to the user.
# ----------------------------------------------------------------------


def _job_to_tool_dict(job: Any) -> dict[str, Any]:
    """Serialise a Job dataclass for MCP tool responses."""
    return {
        "id": job.id,
        "type": job.type,
        "status": job.status,
        "params": job.params,
        "progress_current": job.progress_current,
        "progress_total": job.progress_total,
        "result": job.result,
        "error_message": job.error_message,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


def _poll_job_until_terminal(
    job_repository: SQLiteJobRepository,
    job_id: str,
    *,
    poll_interval: float = 0.5,
    timeout: float = 3600.0,
) -> dict[str, Any]:
    """Block until `job_id` reaches a terminal state or timeout.

    Polls `job_repository.get(job_id)` on a fixed cadence. A stuck or
    very long-running job will eventually time out — the default
    matches the webapp's tolerance for long batches.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = job_repository.get(job_id)
        if job is None:
            return {
                "status": "failed",
                "job_id": job_id,
                "result": None,
                "error_message": (
                    f"Job {job_id} disappeared from the repository"
                ),
            }
        if job.status in ("succeeded", "failed"):
            return {
                "status": job.status,
                "job_id": job.id,
                "result": job.result,
                "error_message": job.error_message,
            }
        time.sleep(poll_interval)
    return {
        "status": "timeout",
        "job_id": job_id,
        "result": None,
        "error_message": (
            f"Job did not reach a terminal state within {timeout}s"
        ),
    }


@mcp.tool()
def journal_extract_entities_batch(
    entry_id: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    stale_only: bool = False,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Run entity extraction as an async batch job and wait for it to finish.

    This is the batch-job wrapper around the synchronous
    `journal_extract_entities` tool: it enqueues work onto the shared
    JobRunner, then polls the jobs table until a terminal state is
    reached. Use this when Claude wants the same progress/error
    semantics the webapp uses.

    NOTE: the tool BLOCKS until the job reaches a terminal state.
    Large batches may take minutes — expect long-running tool calls.

    Args:
        entry_id: If set, extract from this single entry only.
        start_date: Filter entries from this date (ISO 8601). Optional.
        end_date: Filter entries until this date (ISO 8601). Optional.
        stale_only: When True, only process entries flagged as stale.

    Returns:
        ``{"status", "job_id", "result", "error_message"}``. On
        success, ``result`` is the summary dict produced by the
        extraction runner. On failure, the tool returns a structured
        dict — it does NOT raise — so the caller can read the error
        message and respond to the user.
    """
    log.info(
        "Tool call: journal_extract_entities_batch("
        "entry_id=%s, start_date=%s, end_date=%s, stale_only=%s)",
        entry_id, start_date, end_date, stale_only,
    )
    runner = _get_job_runner(ctx)
    job_repository = _get_job_repository(ctx)
    user_id = _user_id(ctx)

    params: dict[str, Any] = {}
    if entry_id is not None:
        params["entry_id"] = int(entry_id)
    if start_date is not None:
        params["start_date"] = start_date
    if end_date is not None:
        params["end_date"] = end_date
    if stale_only:
        params["stale_only"] = True

    try:
        job = runner.submit_entity_extraction(params, user_id=user_id)
    except ValueError as exc:
        return {
            "status": "failed",
            "job_id": None,
            "result": None,
            "error_message": str(exc),
        }

    return _poll_job_until_terminal(job_repository, job.id)


@mcp.tool()
def journal_backfill_mood_scores_batch(
    mode: str,
    start_date: str | None = None,
    end_date: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Run a mood-score backfill as an async batch job and wait for it.

    Same execution model as `journal_extract_entities_batch` — the
    call enqueues a job on the shared JobRunner and polls the jobs
    table until a terminal state is reached.

    NOTE: the tool BLOCKS until the job reaches a terminal state.
    Large backfills may take a long time — expect long-running tool
    calls for `mode="force"` over wide date ranges.

    Args:
        mode: Either ``"stale-only"`` (idempotent — score only
            entries missing a current dimension) or ``"force"``
            (rescore every entry in the date range).
        start_date: Restrict the backfill to entries from this date
            forward (ISO 8601). Optional.
        end_date: Restrict the backfill to entries up to this date
            (ISO 8601). Optional.

    Returns:
        ``{"status", "job_id", "result", "error_message"}``. On
        success, ``result`` is the summary dict produced by the
        backfill runner. On failure, the tool returns a structured
        dict — it does NOT raise.
    """
    log.info(
        "Tool call: journal_backfill_mood_scores_batch("
        "mode=%s, start_date=%s, end_date=%s)",
        mode, start_date, end_date,
    )
    runner = _get_job_runner(ctx)
    job_repository = _get_job_repository(ctx)
    user_id = _user_id(ctx)

    params: dict[str, Any] = {"mode": mode}
    if start_date is not None:
        params["start_date"] = start_date
    if end_date is not None:
        params["end_date"] = end_date

    try:
        job = runner.submit_mood_backfill(params, user_id=user_id)
    except ValueError as exc:
        return {
            "status": "failed",
            "job_id": None,
            "result": None,
            "error_message": str(exc),
        }

    return _poll_job_until_terminal(job_repository, job.id)


@mcp.tool()
def journal_get_job_status(
    job_id: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Return the current state of a batch job.

    Non-blocking — returns whatever is in the jobs table right now.
    Pair with `journal_extract_entities_batch` /
    `journal_backfill_mood_scores_batch` if you need a
    fire-and-forget alternative to the blocking batch tools.

    Args:
        job_id: The UUID returned by a batch-job submission.

    Returns:
        A dict with the full serialised job shape (``id``, ``type``,
        ``status``, ``params``, progress counters, ``result``,
        ``error_message``, timestamps). If the job is not found the
        returned dict has ``{"error": "Job not found", "job_id": ...}``.
    """
    log.info("Tool call: journal_get_job_status(job_id=%s)", job_id)
    job_repository = _get_job_repository(ctx)
    user_id = _user_id(ctx)
    job = job_repository.get(job_id, user_id=user_id)
    if job is None:
        return {"error": "Job not found", "job_id": job_id}
    return _job_to_tool_dict(job)


@mcp.tool()
def journal_list_entities(
    entity_type: str | None = None,
    limit: int = 50,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """List extracted entities, optionally filtered by type.

    Args:
        entity_type: One of 'person', 'place', 'activity', 'organization',
            'topic', 'other'. Omit to list all types.
        limit: Max results (default 50).
    """
    log.info(
        "Tool call: journal_list_entities(entity_type=%s, limit=%d)",
        entity_type, limit,
    )
    store = _get_entity_store(ctx)
    user_id = _user_id(ctx)
    rows = store.list_entities_with_mention_counts(
        entity_type=entity_type, limit=min(limit, 200), offset=0, user_id=user_id,
    )
    if not rows:
        return "No entities found."
    lines = [f"Showing {len(rows)} entities:"]
    for entity, count, _last_seen in rows:
        aliases = f" (aliases: {', '.join(entity.aliases)})" if entity.aliases else ""
        lines.append(
            f"  [{entity.id}] {entity.entity_type}: {entity.canonical_name}"
            f" — {count} mentions{aliases}"
        )
    return "\n".join(lines)


@mcp.tool()
def journal_get_entity_mentions(
    entity_id: int,
    limit: int = 50,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Return every mention of a specific entity across the journal.

    Args:
        entity_id: The entity to look up.
        limit: Max mentions to return (default 50).
    """
    log.info(
        "Tool call: journal_get_entity_mentions(entity_id=%d, limit=%d)",
        entity_id, limit,
    )
    store = _get_entity_store(ctx)
    user_id = _user_id(ctx)
    entity = store.get_entity(entity_id, user_id=user_id)
    if entity is None:
        return f"Entity {entity_id} not found."
    mentions = store.get_mentions_for_entity(entity_id, limit=limit, user_id=user_id)
    if not mentions:
        return f"No mentions recorded for {entity.canonical_name}."
    lines = [f"{len(mentions)} mentions of {entity.canonical_name}:"]
    for m in mentions:
        lines.append(
            f"  entry {m.entry_id}: \"{m.quote}\" (confidence {m.confidence:.2f})"
        )
    return "\n".join(lines)


@mcp.tool()
def journal_get_entity_relationships(
    entity_id: int,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Return the outgoing and incoming relationships for an entity.

    Args:
        entity_id: The entity whose edges to return.
    """
    log.info(
        "Tool call: journal_get_entity_relationships(entity_id=%d)",
        entity_id,
    )
    store = _get_entity_store(ctx)
    user_id = _user_id(ctx)
    entity = store.get_entity(entity_id, user_id=user_id)
    if entity is None:
        return f"Entity {entity_id} not found."
    outgoing, incoming = store.get_relationships_for_entity(entity_id, user_id=user_id)
    if not outgoing and not incoming:
        return f"No relationships recorded for {entity.canonical_name}."
    lines = [f"Relationships for {entity.canonical_name}:"]
    if outgoing:
        lines.append(f"  Outgoing ({len(outgoing)}):")
        for r in outgoing:
            other = store.get_entity(r.object_entity_id, user_id=user_id)
            other_name = other.canonical_name if other else f"#{r.object_entity_id}"
            lines.append(
                f"    -> {r.predicate} -> {other_name} "
                f"(entry {r.entry_id}, conf {r.confidence:.2f})"
            )
    if incoming:
        lines.append(f"  Incoming ({len(incoming)}):")
        for r in incoming:
            other = store.get_entity(r.subject_entity_id, user_id=user_id)
            other_name = other.canonical_name if other else f"#{r.subject_entity_id}"
            lines.append(
                f"    <- {r.predicate} <- {other_name} "
                f"(entry {r.entry_id}, conf {r.confidence:.2f})"
            )
    return "\n".join(lines)


def main() -> None:
    """Run the MCP server with REST API, session/key auth, and optional CORS."""
    import anyio
    import uvicorn
    from starlette.middleware.cors import CORSMiddleware

    from journal.auth import build_auth_middleware_stack

    config = load_config()

    # Fail-closed: refuse to start without a secret key for session
    # tokens and signed URLs. Generate one with:
    #     python -c "import secrets; print(secrets.token_urlsafe(32))"
    if not config.secret_key:
        raise RuntimeError(
            "JOURNAL_SECRET_KEY is not set. The auth system requires "
            "a secret key — generate one with:\n"
            '    python -c "import secrets; print(secrets.token_urlsafe(32))"\n'
            "and add it to your .env file as JOURNAL_SECRET_KEY=..."
        )

    # DNS rebinding protection is always on. `mcp_allowed_hosts` defaults
    # to loopback in config.py, so there is no path that disables it.
    mcp.settings.host = config.mcp_host
    mcp.settings.port = config.mcp_port
    allowed_origins = [f"http://{h}" for h in config.mcp_allowed_hosts]
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=config.mcp_allowed_hosts,
        allowed_origins=allowed_origins,
    )
    log.info(
        "MCP transport security: DNS rebinding protection ON, allowed hosts=%s",
        config.mcp_allowed_hosts,
    )

    # Initialize services eagerly so REST API routes work immediately,
    # without waiting for the first MCP session to connect.
    services = _init_services()

    # Build the Starlette app from FastMCP (includes MCP routes + custom_routes)
    app = mcp.streamable_http_app()

    # Log registered routes for debugging
    for route in app.routes:
        methods = getattr(route, "methods", None)
        log.info("  Route: %s %s", route.path, methods or "(all)")

    # Middleware stack: CORS outermost so that 401/403 responses still
    # carry Access-Control-Allow-Origin headers — otherwise the browser
    # swallows them as a CORS error.
    #
    # Request flow:
    #   client -> CORS -> AuthenticationMiddleware -> RequireAuth -> route
    if config.api_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.api_cors_origins,
            allow_methods=["GET", "PATCH", "DELETE", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "Authorization"],
            allow_credentials=True,
        )

    # Session + API key authentication middleware. Replaces the old
    # single bearer token approach with per-user auth.
    auth_service = services["auth_service"]
    app = build_auth_middleware_stack(app, auth_service)
    log.info("Auth middleware installed (session + API key)")

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
