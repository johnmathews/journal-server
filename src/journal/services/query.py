"""Query service — orchestrates hybrid search and stat/list lookups.

The keyword/semantic mode toggle was retired when hybrid search shipped.
`search_entries` now runs the full L1 (BM25 + dense) → RRF → L2 rerank
pipeline; the `keyword_search` and semantic-only paths are gone.
Callers (REST API, MCP tool, CLI) get a single search method that does
the right thing without forcing a mode choice on the user.

Other read methods (statistics, mood trends, topic frequency, list /
get-by-date) are unchanged — they delegate to the repository and
optionally record latency through `StatsCollector`.
"""

import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypeVar

from journal.db.repository import EntryRepository
from journal.models import (
    CalendarDay,
    ChunkSpan,
    EntityDistributionBin,
    EntityTrendBin,
    Entry,
    EntryPage,
    IngestionStats,
    MoodDrilldownEntry,
    MoodEntityCorrelation,
    MoodTrend,
    SearchResult,
    Statistics,
    TopicFrequency,
    WordCountBucket,
    WordCountStats,
    WritingFrequencyBin,
)
from journal.providers.embeddings import EmbeddingsProvider
from journal.providers.reranker import NoopReranker, Reranker
from journal.services.hybrid import HybridConfig, HybridSearchService
from journal.services.stats import StatsCollector
from journal.vectorstore.store import VectorStore

log = logging.getLogger(__name__)

T = TypeVar("T")


class QueryService:
    def __init__(
        self,
        repository: EntryRepository,
        vector_store: VectorStore,
        embeddings_provider: EmbeddingsProvider,
        stats: StatsCollector | None = None,
        reranker: Reranker | None = None,
        hybrid_config: HybridConfig | None = None,
    ) -> None:
        self._repo = repository
        # Kept as an attribute (not strictly needed for query routing)
        # so the /health endpoint and other diagnostics that reach in
        # for `query_svc._vector_store` continue to work.
        self._vector_store = vector_store
        self._stats = stats
        self._hybrid = HybridSearchService(
            repository=repository,
            vector_store=vector_store,
            embeddings_provider=embeddings_provider,
            reranker=reranker or NoopReranker(),
            config=hybrid_config,
            stats=stats,
        )

    @property
    def hybrid(self) -> HybridSearchService:
        """Expose the underlying hybrid service for diagnostics and admin."""
        return self._hybrid

    @property
    def vector_store(self) -> VectorStore:
        """Vector store handle, exposed for liveness checks and diagnostics."""
        return self._vector_store

    @property
    def connection(self) -> sqlite3.Connection:
        """SQLite connection backing the repository, exposed for liveness checks.

        Delegates to ``EntryRepository.connection``. Only call when you
        need the raw connection for a check that takes a sqlite3 handle
        (e.g. ``journal.services.liveness.check_sqlite``); for anything
        else, prefer the named query methods on this service.
        """
        return self._repo.connection  # type: ignore[attr-defined]

    def _timed(self, query_type: str, fn: Callable[[], T]) -> T:
        """Run `fn()` and record its latency under `query_type`.

        If no stats collector is configured, this is a direct call
        with no clock reads.
        """
        if self._stats is None:
            return fn()
        start = time.monotonic()
        try:
            return fn()
        finally:
            latency_ms = (time.monotonic() - start) * 1000.0
            self._stats.record_query(query_type, latency_ms)

    def search_entries(
        self,
        query: str,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 10,
        offset: int = 0,
        user_id: int | None = None,
        sort: str = "relevance",
    ) -> list[SearchResult]:
        """Hybrid search across journal entries.

        Runs BM25 (FTS5) and dense embedding retrieval in parallel,
        fuses the rankings with Reciprocal Rank Fusion, and reranks
        the top-M candidates with the configured reranker.

        Returns one `SearchResult` per matching entry. Each carries:
        - `snippet` if BM25 contributed (FTS5-marked excerpt).
        - `matching_chunks` if dense retrieval contributed.
        Either or both may be present. The list is ordered by post-
        rerank score descending, then sliced by `offset` / `limit`.

        `sort` overrides the final ordering: "relevance" (default)
        preserves the rerank order; "date_desc" / "date_asc" sort by
        `entry_date` before the slice.
        """
        return self._hybrid.search(
            query=query,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
            user_id=user_id,
            sort=sort,
        )

    def get_entries_by_date(
        self, date: str, user_id: int | None = None
    ) -> list[Entry]:
        return self._repo.get_entries_by_date(date, user_id=user_id)

    def list_entries(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 20,
        offset: int = 0,
        user_id: int | None = None,
    ) -> list[Entry]:
        return self._repo.list_entries(
            start_date, end_date, limit, offset, user_id=user_id
        )

    def get_statistics(
        self, start_date: str | None = None, end_date: str | None = None,
        user_id: int | None = None,
    ) -> Statistics:
        return self._timed(
            "statistics",
            lambda: self._repo.get_statistics(
                start_date, end_date, user_id=user_id
            ),
        )

    def get_mood_trends(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        granularity: str = "week",
        user_id: int | None = None,
    ) -> list[MoodTrend]:
        return self._timed(
            "mood_trends",
            lambda: self._repo.get_mood_trends(
                start_date, end_date, granularity, user_id=user_id
            ),
        )

    def get_topic_frequency(
        self, topic: str, start_date: str | None = None, end_date: str | None = None,
        user_id: int | None = None,
    ) -> TopicFrequency:
        return self._timed(
            "topic_frequency",
            lambda: self._repo.get_topic_frequency(
                topic, start_date, end_date, user_id=user_id
            ),
        )

    def get_entry_pages(self, entry_id: int) -> list[EntryPage]:
        return self._repo.get_entry_pages(entry_id)

    # ──────────────────────────────────────────────────────────────────
    # Public entry reads / writes / metadata.
    #
    # These thin pass-throughs replace the `query_svc._repo.<method>`
    # reach-ins that the api/ layer used before Unit 1b. They exist so
    # callers (REST routes, MCP tools, CLI) can grep for the operation
    # by name without learning the repository structure. Do NOT extend
    # this section by adding speculative methods — only add when there
    # is a concrete caller that would otherwise reach into `_repo`.
    # ──────────────────────────────────────────────────────────────────

    def get_entry(self, entry_id: int, *, user_id: int | None = None) -> Entry | None:
        return self._repo.get_entry(entry_id, user_id=user_id)

    def count_entries(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        user_id: int | None = None,
    ) -> int:
        return self._repo.count_entries(start_date, end_date, user_id=user_id)

    def get_page_count(self, entry_id: int) -> int:
        return self._repo.get_page_count(entry_id)

    def get_uncertain_span_count(self, entry_id: int) -> int:
        return self._repo.get_uncertain_span_count(entry_id)

    def get_uncertain_spans(self, entry_id: int) -> list[tuple[int, int]]:
        return self._repo.get_uncertain_spans(entry_id)

    def get_entity_mention_count(self, entry_id: int) -> int:
        return self._repo.get_entity_mention_count(entry_id)

    def get_chunks(self, entry_id: int) -> list[ChunkSpan]:
        return self._repo.get_chunks(entry_id)

    def get_ingestion_stats(
        self, *, now: datetime | None = None, user_id: int | None = None,
    ) -> IngestionStats:
        return self._repo.get_ingestion_stats(now or datetime.now(UTC), user_id=user_id)

    # ──────────────────────────────────────────────────────────────────
    # Public dashboard aggregations.
    #
    # Each method maps 1:1 to a `/api/dashboard/*` endpoint's underlying
    # query. Same rule as above: extend only when there is a concrete
    # caller.
    # ──────────────────────────────────────────────────────────────────

    def get_writing_frequency(
        self,
        start_date: str | None,
        end_date: str | None,
        granularity: str,
        *,
        user_id: int | None = None,
    ) -> list[WritingFrequencyBin]:
        return self._repo.get_writing_frequency(
            start_date=start_date,
            end_date=end_date,
            granularity=granularity,
            user_id=user_id,
        )

    def get_mood_drilldown(
        self,
        dimension: str,
        period_start: str,
        period_end: str,
        *,
        user_id: int | None = None,
    ) -> list[MoodDrilldownEntry]:
        return self._repo.get_mood_drilldown(
            dimension=dimension,
            period_start=period_start,
            period_end=period_end,
            user_id=user_id,
        )

    def get_entity_distribution(
        self,
        entity_type: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 50,
        *,
        user_id: int | None = None,
    ) -> list[EntityDistributionBin]:
        return self._repo.get_entity_distribution(
            entity_type=entity_type,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            user_id=user_id,
        )

    def get_calendar_heatmap(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        user_id: int | None = None,
    ) -> list[CalendarDay]:
        return self._repo.get_calendar_heatmap(
            start_date=start_date, end_date=end_date, user_id=user_id,
        )

    def get_entity_trends(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        granularity: str = "month",
        entity_type: str | None = None,
        limit: int = 8,
        *,
        user_id: int | None = None,
    ) -> tuple[list[str], list[EntityTrendBin]]:
        return self._repo.get_entity_trends(
            start_date=start_date,
            end_date=end_date,
            granularity=granularity,
            entity_type=entity_type,
            limit=limit,
            user_id=user_id,
        )

    def get_mood_entity_correlation(
        self,
        dimension: str,
        start_date: str | None = None,
        end_date: str | None = None,
        entity_type: str | None = None,
        limit: int = 10,
        *,
        user_id: int | None = None,
    ) -> tuple[float, list[MoodEntityCorrelation]]:
        return self._repo.get_mood_entity_correlation(
            dimension=dimension,
            start_date=start_date,
            end_date=end_date,
            entity_type=entity_type,
            limit=limit,
            user_id=user_id,
        )

    def get_word_count_distribution(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        bucket_size: int = 100,
        *,
        user_id: int | None = None,
    ) -> tuple[list[WordCountBucket], WordCountStats]:
        return self._repo.get_word_count_distribution(
            start_date=start_date,
            end_date=end_date,
            bucket_size=bucket_size,
            user_id=user_id,
        )
