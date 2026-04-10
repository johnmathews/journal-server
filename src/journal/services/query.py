"""Query service — combines SQLite and vector search for answering queries."""

import logging

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
from journal.vectorstore.store import VectorStore

log = logging.getLogger(__name__)


class QueryService:
    def __init__(
        self,
        repository: EntryRepository,
        vector_store: VectorStore,
        embeddings_provider: EmbeddingsProvider,
    ) -> None:
        self._repo = repository
        self._vector_store = vector_store
        self._embeddings = embeddings_provider

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
    ) -> list[SearchResult]:
        """Semantic search across journal entries.

        Returns one `SearchResult` per unique entry, each carrying a list
        of `ChunkMatch` objects for every chunk in that entry that matched
        the query. The `SearchResult.score` is the max chunk score for
        that entry, and the outer list is sorted by score descending.
        """
        log.info("Semantic search: '%s' (limit=%d, offset=%d)", query, limit, offset)

        query_embedding = self._embeddings.embed_query(query)

        where = {}
        if start_date:
            where["entry_date"] = {"$gte": start_date}
        if end_date:
            if "entry_date" in where:
                where = {
                    "$and": [
                        {"entry_date": {"$gte": start_date}},
                        {"entry_date": {"$lte": end_date}},
                    ]
                }
            else:
                where["entry_date"] = {"$lte": end_date}

        # Over-fetch chunks so we can aggregate multiple matches per entry.
        vector_limit = (limit + offset) * self._VECTOR_OVERFETCH_FACTOR
        vector_results = self._vector_store.search(
            query_embedding=query_embedding,
            limit=vector_limit,
            where=where or None,
        )

        # Group chunk matches by entry_id, preserving the order from the
        # vector store (which is already sorted by ascending distance, i.e.
        # descending similarity).
        chunks_by_entry: dict[int, list[ChunkMatch]] = {}
        for vr in vector_results:
            chunks_by_entry.setdefault(vr.entry_id, []).append(
                ChunkMatch(text=vr.chunk_text, score=1.0 - vr.distance)
            )

        # Build one SearchResult per entry, enriching with the full parent
        # text. Entries whose row has been deleted from SQLite (stale
        # chromadb data) are skipped.
        results: list[SearchResult] = []
        for entry_id, chunks in chunks_by_entry.items():
            entry = self._repo.get_entry(entry_id)
            if entry is None:
                continue
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

    def get_entries_by_date(self, date: str) -> list[Entry]:
        return self._repo.get_entries_by_date(date)

    def list_entries(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Entry]:
        return self._repo.list_entries(start_date, end_date, limit, offset)

    def get_statistics(
        self, start_date: str | None = None, end_date: str | None = None
    ) -> Statistics:
        return self._repo.get_statistics(start_date, end_date)

    def get_mood_trends(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        granularity: str = "week",
    ) -> list[MoodTrend]:
        return self._repo.get_mood_trends(start_date, end_date, granularity)

    def get_topic_frequency(
        self, topic: str, start_date: str | None = None, end_date: str | None = None
    ) -> TopicFrequency:
        return self._repo.get_topic_frequency(topic, start_date, end_date)

    def get_entry_pages(self, entry_id: int) -> list[EntryPage]:
        return self._repo.get_entry_pages(entry_id)
