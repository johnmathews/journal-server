"""Query service — combines SQLite and vector search for answering queries."""

import logging

from journal.db.repository import EntryRepository
from journal.models import Entry, EntryPage, MoodTrend, SearchResult, Statistics, TopicFrequency
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

    def search_entries(
        self,
        query: str,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[SearchResult]:
        """Semantic search across journal entries."""
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

        vector_results = self._vector_store.search(
            query_embedding=query_embedding,
            limit=limit + offset,
            where=where or None,
        )

        # Deduplicate by entry_id and enrich with full entry data
        seen_entries: set[int] = set()
        results: list[SearchResult] = []
        for vr in vector_results:
            if vr.entry_id in seen_entries:
                continue
            seen_entries.add(vr.entry_id)

            entry = self._repo.get_entry(vr.entry_id)
            if entry is None:
                continue

            results.append(
                SearchResult(
                    entry_id=entry.id,
                    entry_date=entry.entry_date,
                    text=entry.final_text or entry.raw_text,
                    score=1.0 - vr.distance,  # Convert distance to similarity
                    chunk_text=vr.chunk_text,
                )
            )

        # Apply offset
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
