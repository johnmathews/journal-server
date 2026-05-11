"""Service bootstrap for the MCP server.

Owns the `_init_services()` constructor and the `lifespan` context
manager. The `_services` module-level dict is the shared singleton —
the REST API route registrations and the MCP `lifespan_context` both
read it.

The runtime-settings on-change callback is a closure inside
`_init_services` (4 captured locals — `config`, `runtime_settings`,
`ingestion_service`, `job_runner`). Extracting it to its own module
would require either threading every capture through a factory's
parameters or sharing a mutable services dict; neither pays back
without dedicated callback unit tests, so the closure stays inline.
See `docs/refactor-mcp-server-plan.md` decision 4.
"""

import atexit
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from journal.config import load_config
from journal.db.connection import get_connection
from journal.db.factory import ConnectionFactory
from journal.db.jobs_repository import SQLiteJobRepository
from journal.db.migrations import run_migrations
from journal.db.repository import SQLiteEntryRepository
from journal.entitystore.store import SQLiteEntityStore
from journal.logging import setup_logging
from journal.providers.embeddings import OpenAIEmbeddingsProvider
from journal.providers.extraction import AnthropicExtractionProvider
from journal.providers.ocr import build_ocr_provider
from journal.providers.reranker import build_reranker
from journal.providers.transcription import build_transcription_provider
from journal.services.backfill import backfill_mood_scores
from journal.services.chunking import build_chunker
from journal.services.entity_extraction import EntityExtractionService
from journal.services.hybrid import HybridConfig
from journal.services.ingestion import IngestionService
from journal.services.jobs import JobRunner
from journal.services.query import QueryService
from journal.vectorstore.store import ChromaVectorStore

log = logging.getLogger(__name__)

# Shared services — initialized once at startup, reused across all sessions and
# REST API requests. Both the MCP lifespan and the REST API routes access this.
_services: dict | None = None


def _build_fitness_callables(
    *,
    fitness_repo: Any,
    config: Any,
    notification_service: Any,
) -> dict[str, Any]:
    """Wire Strava + Garmin fetch + normalize for the JobRunner.

    Returns the four callables the JobRunner constructor expects
    (``fetch_strava_callable``, ``fetch_garmin_callable``,
    ``normalize_strava_callable``, ``normalize_garmin_callable``).

    Strava is wired only when ``STRAVA_CLIENT_ID`` and
    ``STRAVA_CLIENT_SECRET`` are set (one OAuth app per server, shared
    across users). Garmin is wired unconditionally — credentials are
    per-user from W6 onwards, sourced from ``fitness_auth_state``, not
    from env vars. A user without a Garmin auth row produces a clean
    ``auth_broken`` sync rather than a runtime error. Callers pass the
    dict via ``**`` so a Strava-less server still wires up Garmin.

    The repository is constructed in :func:`_init_services` and threaded
    through here so the API layer (W9) can read from the same instance
    without re-wrapping the connection.
    """
    from journal.models import FitnessAuthState
    from journal.providers.garmin import GarminConnectGarminProvider
    from journal.providers.strava import StravalibStravaProvider, Tokens
    from journal.services.fitness.backfill import (
        backfill_garmin,
        backfill_strava,
    )
    from journal.services.fitness.fetch import (
        GarminFetchService,
        StravaFetchService,
    )
    from journal.services.fitness.normalize import (
        normalize_garmin,
        normalize_strava,
    )
    out: dict[str, Any] = {
        "fetch_strava_callable": None,
        "fetch_garmin_callable": None,
        "normalize_strava_callable": None,
        "normalize_garmin_callable": None,
        "backfill_strava_callable": None,
        "backfill_garmin_callable": None,
    }

    strava_configured = bool(
        config.strava_client_id and config.strava_client_secret,
    )
    if strava_configured:
        def _strava_provider_factory(
            auth: FitnessAuthState,
        ) -> StravalibStravaProvider:
            user_id = auth.user_id

            def _persist(tokens: Tokens) -> None:
                # Merge into the existing row so we don't reset
                # auth_status / auth_broken_since / last_*_at columns
                # the fetch service maintains separately.
                existing = fitness_repo.get_auth_state(
                    user_id=user_id, source="strava",
                )
                fitness_repo.upsert_auth_state(
                    FitnessAuthState(
                        user_id=user_id,
                        source="strava",
                        access_token=tokens["access_token"],
                        refresh_token=tokens["refresh_token"],
                        token_expires_at=tokens["token_expires_at"],
                        extra_state=dict(existing.extra_state) if existing else {},
                        last_successful_login_at=(
                            existing.last_successful_login_at if existing else None
                        ),
                        last_refresh_at=existing.last_refresh_at if existing else None,
                        auth_status=existing.auth_status if existing else "unknown",
                        auth_broken_since=(
                            existing.auth_broken_since if existing else None
                        ),
                        created_at=existing.created_at if existing else "",
                    ),
                )

            return StravalibStravaProvider(
                client_id=config.strava_client_id,
                client_secret=config.strava_client_secret,
                access_token=auth.access_token or "",
                refresh_token=auth.refresh_token or "",
                token_expires_at=auth.token_expires_at or "1970-01-01T00:00:00Z",
                persist_tokens=_persist,
            )

        strava_fetch = StravaFetchService(
            repo=fitness_repo,
            notifier=notification_service,
            config=config,
            provider_factory=_strava_provider_factory,
        )
        out["fetch_strava_callable"] = strava_fetch.run_sync
        out["normalize_strava_callable"] = (
            lambda *, user_id, **kw: normalize_strava(
                fitness_repo, user_id=user_id, notifier=notification_service,
                **kw,
            )
        )
        # W5 backfill — wraps the existing CLI orchestrator with the
        # same fetch service so resume / abort semantics are identical
        # to ``journal fitness-backfill``. ``end=None`` means "today
        # (UTC)" inside the orchestrator.
        def _backfill_strava(
            *, user_id: int, start: str, end: str | None = None,
        ) -> Any:
            return backfill_strava(
                user_id=user_id,
                repo=fitness_repo,
                fetch_service=strava_fetch,
                notifier=notification_service,
                start=start,
                end=end,
            )

        out["backfill_strava_callable"] = _backfill_strava

    # Garmin is wired unconditionally — per-user credentials in
    # `fitness_auth_state` are the source of truth from W6 onwards.
    def _garmin_provider_factory(
        auth: FitnessAuthState,
    ) -> GarminConnectGarminProvider:
        user_id = auth.user_id
        tokens_blob = auth.extra_state.get("tokens_blob") if auth.extra_state else None

        def _persist(tokens_blob: str) -> None:
            # garminconnect emits a JSON blob covering both OAuth1
            # and OAuth2 tokens; we stash it on extra_state so the
            # next sync boots from the DB, not a filesystem cache.
            existing = fitness_repo.get_auth_state(
                user_id=user_id, source="garmin",
            )
            extra = dict(existing.extra_state) if existing else {}
            extra["tokens_blob"] = tokens_blob
            fitness_repo.upsert_auth_state(
                FitnessAuthState(
                    user_id=user_id,
                    source="garmin",
                    access_token=existing.access_token if existing else None,
                    refresh_token=existing.refresh_token if existing else None,
                    token_expires_at=(
                        existing.token_expires_at if existing else None
                    ),
                    extra_state=extra,
                ),
            )

        return GarminConnectGarminProvider(
            username="",
            password="",
            tokens_blob=tokens_blob,
            persist_tokens=_persist,
        )

    garmin_fetch = GarminFetchService(
        repo=fitness_repo,
        notifier=notification_service,
        config=config,
        provider_factory=_garmin_provider_factory,
    )
    out["fetch_garmin_callable"] = garmin_fetch.run_sync
    out["normalize_garmin_callable"] = (
        lambda *, user_id, **kw: normalize_garmin(
            fitness_repo, user_id=user_id, notifier=notification_service,
            **kw,
        )
    )

    def _backfill_garmin(
        *, user_id: int, start: str, end: str | None = None,
    ) -> Any:
        return backfill_garmin(
            user_id=user_id,
            repo=fitness_repo,
            fetch_service=garmin_fetch,
            notifier=notification_service,
            start=start,
            end=end,
        )

    out["backfill_garmin_callable"] = _backfill_garmin

    return out


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

    # One process-wide ``ConnectionFactory`` — each thread that calls
    # into a migrated repo opens its own ``sqlite3.Connection``, so
    # the shared-state commit race documented in
    # ``docs/sqlite-threading.md`` cannot happen. Repos migrated so
    # far (W2, W3): ``SQLiteJobRepository``, ``SQLiteEntryRepository``.
    # The remaining repos (``FitnessRepository``, ``SQLiteEntityStore``,
    # ``SQLiteUserRepository``, ``RuntimeSettings``) still share
    # ``conn`` until the rest of W3 lands.
    db_factory = ConnectionFactory(config.db_path)

    repo = SQLiteEntryRepository(db_factory)
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
    transcription = build_transcription_provider(config)
    embeddings = OpenAIEmbeddingsProvider(
        api_key=config.openai_api_key,
        model=config.embedding_model,
        dimensions=config.embedding_dimensions,
    )
    reranker = build_reranker(
        config.hybrid_reranker,
        anthropic_api_key=config.anthropic_api_key,
        model=config.reranker_model,
    )
    log.info(
        "  Providers: OCR=%s%s (%s), transcription=%s, embeddings=%s, "
        "reranker=%s (%s)",
        config.ocr_provider,
        " [dual-pass]" if config.ocr_dual_pass else "",
        config.ocr_model or "default",
        config.transcription_model,
        config.embedding_model,
        config.hybrid_reranker,
        config.reranker_model
        if config.hybrid_reranker != "none"
        else "n/a",
    )
    if config.preprocess_images:
        log.info("  Image preprocessing: enabled")

    chunker = build_chunker(config, embeddings)

    from journal.services.entity_naming import load_entity_casing_exceptions

    entity_casing_exceptions = load_entity_casing_exceptions(
        config.entity_casing_exceptions_path
    )
    log.info(
        "  Entity casing exceptions loaded: %d entries from %s",
        len(entity_casing_exceptions),
        config.entity_casing_exceptions_path,
    )
    entity_store = SQLiteEntityStore(
        db_factory, casing_exceptions=entity_casing_exceptions
    )
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
    mood_dimensions_meta: Any = None
    if config.enable_mood_scoring:
        from journal.providers.mood_scorer import AnthropicMoodScorer
        from journal.services.mood_dimensions import (
            load_mood_dimensions,
            load_mood_meta,
        )
        from journal.services.mood_scoring import MoodScoringService

        mood_dimensions = load_mood_dimensions(config.mood_dimensions_path)
        mood_dimensions_meta = load_mood_meta(config.mood_dimensions_path)
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

    user_repo = SQLiteUserRepository(db_factory)

    entity_extraction_service = EntityExtractionService(
        repository=repo,
        entity_store=entity_store,
        extraction_provider=extraction_provider,
        embeddings_provider=embeddings,
        author_name=config.journal_author_name,
        dedup_similarity_threshold=config.entity_dedup_similarity_threshold,
        user_repo=user_repo,
        llm_candidate_top_k=config.entity_llm_candidate_top_k,
        llm_candidate_threshold=config.entity_llm_candidate_threshold,
        llm_match_min_cosine=config.entity_llm_match_min_cosine,
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

    def _build_heading_detector(cfg, rs):  # type: ignore[no-untyped-def]
        """Build a date-heading detector if the runtime setting is enabled."""
        if not rs.get("date_heading_detection"):
            return None
        if not cfg.anthropic_api_key:
            log.warning(
                "date_heading_detection is enabled but ANTHROPIC_API_KEY is not "
                "set — heading detection will be skipped"
            )
            return None
        from journal.services.heading_detector import AnthropicHeadingDetector
        return AnthropicHeadingDetector(
            api_key=cfg.anthropic_api_key,
            model=cfg.date_heading_model,
        )

    def _on_runtime_setting_change(key: str, value: Any) -> None:
        """Side-effect callback: rebuild OCR / mood / formatter / heading
        detector when the matching runtime setting changes.

        OCR and mood-scoring rebuilds delegate to the same helpers that
        back the admin reload endpoints (`services/reload.py`). Keeping
        both paths on a single implementation means a future fix to the
        swap logic lands everywhere it needs to.
        """
        if key in ("ocr_dual_pass", "ocr_provider"):
            from dataclasses import replace

            from journal.services.reload import reload_ocr_provider

            # Build a Config with both OCR runtime values overlaid so
            # the helper sees the freshly toggled setting.
            other_key = "ocr_dual_pass" if key == "ocr_provider" else "ocr_provider"
            patched = replace(
                config,
                **{key: value, other_key: runtime_settings.get(other_key)},
            )
            reload_ocr_provider(
                {"ingestion": ingestion_service}, patched,
            )
            log.info(
                "OCR provider rebuilt due to runtime setting change: %s=%r",
                key,
                value,
            )
        elif key == "preprocess_images":
            ingestion_service.set_preprocess_images(value)
            log.info("Preprocessing %s via runtime settings", "enabled" if value else "disabled")
        elif key == "enable_mood_scoring":
            if value:
                from dataclasses import replace

                from journal.services.reload import reload_mood_dimensions

                # The runtime flip is the source of truth here — the
                # startup config may still say `enable_mood_scoring=False`.
                # Patch it on so the helper doesn't refuse to build.
                reload_mood_dimensions(
                    {
                        "ingestion": ingestion_service,
                        "job_runner": job_runner,
                    },
                    replace(config, enable_mood_scoring=True),
                )
                log.info("Mood scoring enabled via runtime settings")
            else:
                ingestion_service.replace_mood_scoring(None)
                job_runner.replace_mood_scoring(None)
                log.info("Mood scoring disabled via runtime settings")
        elif key == "transcript_formatting":
            if value:
                from journal.providers.formatter import AnthropicFormatter

                ingestion_service.replace_formatter(
                    AnthropicFormatter(
                        api_key=config.anthropic_api_key,
                        model=config.transcript_formatter_model,
                    ),
                )
                log.info("Transcript formatting enabled via runtime settings")
            else:
                ingestion_service.replace_formatter(None)
                log.info("Transcript formatting disabled via runtime settings")
        elif key == "date_heading_detection":
            if value and config.anthropic_api_key:
                from journal.services.heading_detector import (
                    AnthropicHeadingDetector,
                )

                ingestion_service.replace_heading_detector(
                    AnthropicHeadingDetector(
                        api_key=config.anthropic_api_key,
                        model=config.date_heading_model,
                    ),
                )
                log.info("Date-heading detection enabled via runtime settings")
            else:
                ingestion_service.replace_heading_detector(None)
                log.info("Date-heading detection disabled via runtime settings")

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
        heading_detector=_build_heading_detector(config, runtime_settings),
    )

    # Pushover notification service — optional, only when credentials
    # are configured via environment or per-user preferences.
    from journal.services.notifications import PushoverNotificationService

    notification_service: PushoverNotificationService | None = None
    notification_service = PushoverNotificationService(
        user_repo=user_repo,
        default_user_key=config.pushover_user_key,
        default_app_token=config.pushover_app_token,
    )
    if config.pushover_app_token:
        log.info("  Notification service initialized (Pushover, server default token set)")
    else:
        log.info("  Notification service initialized (Pushover, per-user credentials only)")

    # Jobs infrastructure: repository + single-worker runner.
    #
    # The jobs repo uses the process-wide ``ConnectionFactory`` (see
    # ``db_factory`` above) so each thread that touches it (ASGI
    # request handler, JobRunner worker, lifespan hooks) opens its
    # own ``sqlite3.Connection``. That eliminates the shared-state
    # commit race that bit prod on 2026-04-XX and 2026-05-11 — see
    # ``docs/sqlite-per-thread-connections-plan.md``.
    job_repository = SQLiteJobRepository(db_factory)
    reconciled = job_repository.reconcile_stuck_jobs()
    log.info(
        "  Jobs: reconciled %d stuck job(s) from previous process",
        reconciled,
    )
    from journal.db.fitness_repository import FitnessRepository

    fitness_repo = FitnessRepository(db_factory)
    fitness_callables = _build_fitness_callables(
        fitness_repo=fitness_repo,
        config=config,
        notification_service=notification_service,
    )
    job_runner = JobRunner(
        job_repository=job_repository,
        entity_extraction_service=entity_extraction_service,
        mood_backfill_callable=backfill_mood_scores,
        mood_scoring_service=mood_scoring_service,
        entry_repository=repo,
        ingestion_service=ingestion_service,
        notification_service=notification_service,
        **fitness_callables,
    )
    # Garmin is wired unconditionally (per-user creds, W6). Strava is
    # wired only when STRAVA_CLIENT_ID + STRAVA_CLIENT_SECRET are set.
    strava_wired = fitness_callables.get("fetch_strava_callable") is not None
    if strava_wired:
        log.info("  Fitness sync wired (Strava + Garmin providers)")
    else:
        log.info(
            "  Fitness sync wired (Garmin only — no STRAVA_CLIENT_ID; "
            "submit_fitness_sync_strava will refuse)",
        )
    log.info("  Jobs: JobRunner started (single-worker executor)")

    # Health poller — daemon thread that monitors internal components
    # and notifies admins via Pushover on status degradation.
    from journal.services.health_poll import HealthPoller

    health_poller = HealthPoller(
        conn=conn,
        vector_store=vector_store,
        db_path=config.db_path,
        notification_service=notification_service,
    )
    health_poller.start()
    log.info("  Health poller started (interval=300s)")

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
        health_poller.stop()
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
            reranker=reranker,
            hybrid_config=HybridConfig(
                bm25_candidates=config.hybrid_bm25_candidates,
                dense_candidates=config.hybrid_dense_candidates,
                fusion_top_m=config.hybrid_fusion_top_m,
                rrf_k=config.hybrid_rrf_k,
            ),
        ),
        "entity_store": entity_store,
        "entity_casing_exceptions": entity_casing_exceptions,
        "entity_extraction": entity_extraction_service,
        "job_repository": job_repository,
        "job_runner": job_runner,
        "config": config,
        "runtime_settings": runtime_settings,
        "stats": stats_collector,
        "mood_dimensions": mood_dimensions,
        "mood_dimensions_meta": mood_dimensions_meta,
        "mood_scoring": mood_scoring_service,
        # Auth services — used by auth_api.py routes.
        "auth_service": auth_service,
        "email_service": email_service,
        "user_repo": user_repo,
        # Notifications
        "notification_service": notification_service,
        # Fitness — repo for read APIs (W9) and the integrity check.
        "fitness_repo": fitness_repo,
        # Raw DB connection — used by lightweight config readers (e.g. pricing).
        "db_conn": conn,
    }

    entry_count = repo.count_entries()
    log.info("Services initialized (entries in DB: %d)", entry_count)
    return _services


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Yield shared services for MCP sessions."""
    yield _init_services()
