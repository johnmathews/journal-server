"""Query service — combines SQLite and vector search for answering queries."""

import logging
import time
from collections.abc import Callable
from typing import TypeVar

from journal.db.repository import EntryRepository
from journal.models import (
    ChunkMatch,
    Entry,
    EntryPage,
    MoodTrend,
    SearchResult,
    Statistics,
    TopicFrequency,
)
from journal.providers.embeddings import EmbeddingsProvider
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
    ) -> None:
        self._repo = repository
        self._vector_store = vector_store
        self._embeddings = embeddings_provider
        # Optional stats collector. When `None`, the timed wrapper
        # below is a straight passthrough — zero extra clock reads
        # and no locks. The `/health` endpoint passes in an
        # `InMemoryStatsCollector`; everything else keeps working
        # unchanged.
        self._stats = stats

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

    # When the caller asks for `limit` entries, we over-fetch chunks from
    # the vector store so that after grouping by entry_id we still have a
    # good chance of finding `limit` distinct entries. A factor of 5× is
    # arbitrary but reasonable — tune if the real corpus shows low entry
    # diversity in top results.
    _VECTOR_OVERFETCH_FACTOR: int = 5

    def search_entries(
        self,
        query: str,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 10,
        offset: int = 0,
        user_id: int | None = None,
    ) -> list[SearchResult]:
        """Semantic search across journal entries.

        Returns one `SearchResult` per unique entry, each carrying a list
        of `ChunkMatch` objects for every chunk in that entry that matched
        the query. The `SearchResult.score` is the max chunk score for
        that entry, and the outer list is sorted by score descending.
        """
        return self._timed(
            "semantic_search",
            lambda: self._search_entries_impl(
                query, start_date, end_date, limit, offset, user_id
            ),
        )

    def _search_entries_impl(
        self,
        query: str,
        start_date: str | None,
        end_date: str | None,
        limit: int,
        offset: int,
        user_id: int | None = None,
    ) -> list[SearchResult]:
        log.info("Semantic search: '%s' (limit=%d, offset=%d)", query, limit, offset)

        query_embedding = self._embeddings.embed_query(query)

        conditions: list[dict] = []
        if user_id is not None:
            conditions.append({"user_id": user_id})
        if start_date:
            conditions.append({"entry_date": {"$gte": start_date}})
        if end_date:
            conditions.append({"entry_date": {"$lte": end_date}})

        where: dict = {}
        if len(conditions) == 1:
            where = conditions[0]
        elif len(conditions) > 1:
            where = {"$and": conditions}

        # Over-fetch chunks so we can aggregate multiple matches per entry.
        vector_limit = (limit + offset) * self._VECTOR_OVERFETCH_FACTOR
        vector_results = self._vector_store.search(
            query_embedding=query_embedding,
            limit=vector_limit,
            where=where or None,
        )

        # Group chunk matches by entry_id, preserving the order from the
        # vector store (which is already sorted by ascending distance, i.e.
        # descending similarity). Track the chunk_index from Chroma
        # metadata so we can JOIN back to `entry_chunks` for char offsets
        # after the grouping pass.
        chunks_by_entry: dict[int, list[ChunkMatch]] = {}
        for vr in vector_results:
            chunk_index = vr.metadata.get("chunk_index")
            chunks_by_entry.setdefault(vr.entry_id, []).append(
                ChunkMatch(
                    text=vr.chunk_text,
                    score=1.0 - vr.distance,
                    chunk_index=chunk_index,
                )
            )

        # Build one SearchResult per entry, enriching with the full parent
        # text and char offsets pulled from `entry_chunks` by chunk_index.
        # Entries whose row has been deleted from SQLite (stale chromadb
        # data) are skipped. Entries that were ingested before migration
        # 0003 have no persisted chunks — we still return them but leave
        # char_start/char_end as None on each ChunkMatch.
        results: list[SearchResult] = []
        for entry_id, chunks in chunks_by_entry.items():
            entry = self._repo.get_entry(entry_id, user_id=user_id)
            if entry is None:
                continue

            persisted_chunks = self._repo.get_chunks(entry_id)
            if persisted_chunks:
                for cm in chunks:
                    if cm.chunk_index is None:
                        continue
                    if 0 <= cm.chunk_index < len(persisted_chunks):
                        span = persisted_chunks[cm.chunk_index]
                        cm.char_start = span.char_start
                        cm.char_end = span.char_end

            # Sort chunks within the entry by score descending.
            chunks.sort(key=lambda c: c.score, reverse=True)
            results.append(
                SearchResult(
                    entry_id=entry.id,
                    entry_date=entry.entry_date,
                    text=entry.final_text or entry.raw_text,
                    score=chunks[0].score,
                    matching_chunks=chunks,
                )
            )

        # Sort entries by their top chunk score descending, then apply
        # pagination.
        results.sort(key=lambda r: r.score, reverse=True)
        return results[offset : offset + limit]

    def keyword_search(
        self,
        query: str,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 10,
        offset: int = 0,
        user_id: int | None = None,
    ) -> list[SearchResult]:
        """FTS5 keyword search across journal entries.

        Thin wrapper on `EntryRepository.search_text_with_snippets`.
        Returns one `SearchResult` per matching entry, with `snippet`
        populated (FTS5 `snippet()` output wrapping matched terms in
        `\\x02`/`\\x03`) and `matching_chunks` intentionally empty —
        FTS5 does not produce per-chunk scores.

        The per-entry `score` is derived from the result's rank in the
        FTS5 ORDER BY (1.0 for the best hit, decaying linearly across
        the page). This is not a similarity score and is not comparable
        to semantic mode scores — it only keeps the list ordering
        stable when the frontend re-sorts.
        """
        return self._timed(
            "keyword_search",
            lambda: self._keyword_search_impl(
                query, start_date, end_date, limit, offset, user_id
            ),
        )

    def _keyword_search_impl(
        self,
        query: str,
        start_date: str | None,
        end_date: str | None,
        limit: int,
        offset: int,
        user_id: int | None = None,
    ) -> list[SearchResult]:
        log.info(
            "Keyword search: '%s' (limit=%d, offset=%d)", query, limit, offset
        )

        rows = self._repo.search_text_with_snippets(
            query=query,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
            user_id=user_id,
        )

        results: list[SearchResult] = []
        for rank, (entry, snippet) in enumerate(rows):
            # Linear decay from 1.0 so clients that sort by `score`
            # preserve the FTS5 rank ordering. The exact decay is not
            # meaningful — only the ordering is.
            score = 1.0 - (rank / max(len(rows), 1))
            results.append(
                SearchResult(
                    entry_id=entry.id,
                    entry_date=entry.entry_date,
                    text=entry.final_text or entry.raw_text,
                    score=score,
                    matching_chunks=[],
                    snippet=snippet,
                )
            )
        return results

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
