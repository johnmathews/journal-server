"""Tests for query service."""

from unittest.mock import MagicMock

import pytest

from journal.db.repository import SQLiteEntryRepository
from journal.models import ChunkSpan
from journal.services.query import QueryService
from journal.vectorstore.store import InMemoryVectorStore


@pytest.fixture
def repo(db_conn):
    return SQLiteEntryRepository(db_conn)


@pytest.fixture
def vector_store():
    return InMemoryVectorStore()


@pytest.fixture
def mock_embeddings():
    provider = MagicMock()
    provider.embed_texts.return_value = [[1.0, 0.0, 0.0]]
    provider.embed_query.return_value = [1.0, 0.0, 0.0]
    return provider


@pytest.fixture
def query_service(repo, vector_store, mock_embeddings):
    return QueryService(
        repository=repo,
        vector_store=vector_store,
        embeddings_provider=mock_embeddings,
    )


@pytest.fixture
def seeded_service(repo, vector_store, mock_embeddings):
    """A query service with some test data."""
    e1 = repo.create_entry("2026-03-22", "photo", "Walked through Vienna with Atlas", 5)
    e2 = repo.create_entry("2026-03-23", "voice", "Stayed home and read a book", 6)

    vector_store.add_entry(
        entry_id=e1.id,
        chunks=["Walked through Vienna with Atlas"],
        embeddings=[[1.0, 0.0, 0.0]],
        metadata={"entry_date": "2026-03-22"},
    )
    vector_store.add_entry(
        entry_id=e2.id,
        chunks=["Stayed home and read a book"],
        embeddings=[[0.0, 1.0, 0.0]],
        metadata={"entry_date": "2026-03-23"},
    )

    return QueryService(
        repository=repo,
        vector_store=vector_store,
        embeddings_provider=mock_embeddings,
    )


def test_search_entries(seeded_service, mock_embeddings):
    mock_embeddings.embed_query.return_value = [0.9, 0.1, 0.0]
    results = seeded_service.search_entries("Vienna")
    assert len(results) >= 1
    assert results[0].entry_date == "2026-03-22"
    assert results[0].score > 0
    # WU-G: every result carries the list of matching chunks.
    assert len(results[0].matching_chunks) >= 1
    assert results[0].matching_chunks[0].text == "Walked through Vienna with Atlas"


def test_search_entries_empty(query_service):
    results = query_service.search_entries("anything")
    assert results == []


def test_search_aggregates_multiple_chunks_per_entry(repo, vector_store, mock_embeddings):
    """A query matching 3 chunks in the same entry should return ONE result
    with 3 ChunkMatch objects, not 3 separate results."""
    entry = repo.create_entry(
        "2026-03-24",
        "photo",
        "Long entry with many thoughts about Vienna and Atlas and Robyn.",
        11,
    )
    # Three chunks from the same entry, all pointing in similar directions.
    vector_store.add_entry(
        entry_id=entry.id,
        chunks=[
            "Vienna was beautiful in spring",
            "Atlas loved the playground in Vienna",
            "Dinner in Vienna was memorable",
        ],
        embeddings=[[0.9, 0.1, 0.0], [0.8, 0.2, 0.0], [0.85, 0.15, 0.0]],
        metadata={"entry_date": "2026-03-24"},
    )
    mock_embeddings.embed_query.return_value = [1.0, 0.0, 0.0]

    svc = QueryService(
        repository=repo,
        vector_store=vector_store,
        embeddings_provider=mock_embeddings,
    )
    results = svc.search_entries("Vienna")

    assert len(results) == 1
    assert len(results[0].matching_chunks) == 3
    # Chunks within the entry are sorted by score descending.
    scores = [cm.score for cm in results[0].matching_chunks]
    assert scores == sorted(scores, reverse=True)
    # Entry-level score is the top chunk score.
    assert results[0].score == results[0].matching_chunks[0].score


def test_search_sorts_entries_by_top_score(repo, vector_store, mock_embeddings):
    """Two entries, one with a strong match, one with a weak match —
    the strong-match entry should come first."""
    e_weak = repo.create_entry("2026-03-25", "photo", "weak match entry", 3)
    e_strong = repo.create_entry("2026-03-26", "photo", "strong match entry", 3)

    vector_store.add_entry(
        entry_id=e_weak.id,
        chunks=["weak match"],
        embeddings=[[0.1, 0.9, 0.0]],
        metadata={"entry_date": "2026-03-25"},
    )
    vector_store.add_entry(
        entry_id=e_strong.id,
        chunks=["strong match"],
        embeddings=[[0.95, 0.05, 0.0]],
        metadata={"entry_date": "2026-03-26"},
    )
    mock_embeddings.embed_query.return_value = [1.0, 0.0, 0.0]

    svc = QueryService(
        repository=repo,
        vector_store=vector_store,
        embeddings_provider=mock_embeddings,
    )
    results = svc.search_entries("match")

    assert len(results) == 2
    assert results[0].entry_id == e_strong.id
    assert results[1].entry_id == e_weak.id
    assert results[0].score > results[1].score


def test_search_result_has_full_parent_text(seeded_service, mock_embeddings):
    """The `text` field on a SearchResult should carry the full entry text,
    not just the matched chunk."""
    mock_embeddings.embed_query.return_value = [1.0, 0.0, 0.0]
    results = seeded_service.search_entries("Vienna")
    assert results[0].text == "Walked through Vienna with Atlas"


def test_get_entries_by_date(seeded_service):
    entries = seeded_service.get_entries_by_date("2026-03-22")
    assert len(entries) == 1
    assert "Vienna" in entries[0].raw_text


def test_list_entries(seeded_service):
    entries = seeded_service.list_entries()
    assert len(entries) == 2


def test_list_entries_filtered(seeded_service):
    entries = seeded_service.list_entries(start_date="2026-03-23")
    assert len(entries) == 1
    assert entries[0].entry_date == "2026-03-23"


def test_get_statistics(seeded_service):
    stats = seeded_service.get_statistics()
    assert stats.total_entries == 2
    assert stats.total_words == 11


def test_get_topic_frequency(seeded_service):
    freq = seeded_service.get_topic_frequency("Vienna")
    assert freq.topic == "Vienna"
    assert freq.count == 1


def test_get_mood_trends(seeded_service, repo):
    repo.add_mood_score(1, "overall", 0.7)
    repo.add_mood_score(2, "overall", -0.2)
    trends = seeded_service.get_mood_trends(granularity="day")
    assert len(trends) == 2


class TestSearchEntriesChunkOffsets:
    """T1.4.b — semantic search results should carry char offsets."""

    def test_semantic_results_carry_chunk_offsets(
        self, repo, vector_store, mock_embeddings
    ):
        """When the entry has persisted chunks, every ChunkMatch gets
        char_start/char_end/chunk_index filled in from entry_chunks."""
        entry_text = "Walked through Vienna with Atlas. Then we met Robyn."
        entry = repo.create_entry("2026-03-22", "photo", entry_text, 10)

        # Persist two chunks with realistic offsets.
        repo.replace_chunks(
            entry.id,
            [
                ChunkSpan(
                    text="Walked through Vienna with Atlas.",
                    char_start=0,
                    char_end=33,
                    token_count=7,
                ),
                ChunkSpan(
                    text="Then we met Robyn.",
                    char_start=34,
                    char_end=52,
                    token_count=5,
                ),
            ],
        )

        # Index both chunks in the vector store with matching
        # chunk_index metadata — InMemoryVectorStore.add_entry sets
        # chunk_index on metadata automatically.
        vector_store.add_entry(
            entry_id=entry.id,
            chunks=[
                "Walked through Vienna with Atlas.",
                "Then we met Robyn.",
            ],
            embeddings=[[1.0, 0.0, 0.0], [0.1, 0.9, 0.0]],
            metadata={"entry_date": "2026-03-22"},
        )
        mock_embeddings.embed_query.return_value = [1.0, 0.0, 0.0]

        svc = QueryService(
            repository=repo,
            vector_store=vector_store,
            embeddings_provider=mock_embeddings,
        )
        results = svc.search_entries("Vienna")

        assert len(results) == 1
        chunks = results[0].matching_chunks
        # Top chunk is the Vienna one at index 0.
        top = chunks[0]
        assert top.chunk_index == 0
        assert top.char_start == 0
        assert top.char_end == 33
        # Sliced text from offsets should equal the persisted chunk text.
        assert entry_text[top.char_start : top.char_end] == (
            "Walked through Vienna with Atlas."
        )

    def test_semantic_offsets_none_for_legacy_entries(
        self, repo, vector_store, mock_embeddings
    ):
        """Entries ingested before chunk persistence get None offsets,
        not a crash."""
        entry = repo.create_entry("2026-03-22", "photo", "Legacy text", 2)
        # Deliberately skip repo.replace_chunks() to simulate pre-0003.
        vector_store.add_entry(
            entry_id=entry.id,
            chunks=["Legacy text"],
            embeddings=[[1.0, 0.0, 0.0]],
            metadata={"entry_date": "2026-03-22"},
        )
        mock_embeddings.embed_query.return_value = [1.0, 0.0, 0.0]

        svc = QueryService(
            repository=repo,
            vector_store=vector_store,
            embeddings_provider=mock_embeddings,
        )
        results = svc.search_entries("Legacy")
        assert len(results) == 1
        chunks = results[0].matching_chunks
        # chunk_index is still set (from Chroma metadata) but char
        # offsets are None because there are no persisted chunks to
        # JOIN against.
        assert chunks[0].chunk_index == 0
        assert chunks[0].char_start is None
        assert chunks[0].char_end is None


class TestKeywordSearch:
    """T1.4.a — keyword_search delegates to FTS5 snippet method."""

    def test_keyword_search_returns_snippet(self, seeded_service):
        results = seeded_service.keyword_search("Vienna")
        assert len(results) == 1
        r = results[0]
        assert r.entry_date == "2026-03-22"
        assert r.matching_chunks == []
        assert r.snippet is not None
        assert "\x02" in r.snippet and "\x03" in r.snippet
        # Score is a positive float ordering hint.
        assert r.score > 0

    def test_keyword_search_date_filter(self, repo, vector_store, mock_embeddings):
        repo.create_entry("2026-01-15", "photo", "Vienna in January", 3)
        repo.create_entry("2026-03-15", "photo", "Vienna in March", 3)
        svc = QueryService(
            repository=repo,
            vector_store=vector_store,
            embeddings_provider=mock_embeddings,
        )
        results = svc.keyword_search("Vienna", start_date="2026-03-01")
        assert len(results) == 1
        assert results[0].entry_date == "2026-03-15"

    def test_keyword_search_pagination(self, repo, vector_store, mock_embeddings):
        for i in range(5):
            repo.create_entry(
                f"2026-03-{10 + i:02d}",
                "photo",
                f"Entry {i} mentions Atlas directly.",
                5,
            )
        svc = QueryService(
            repository=repo,
            vector_store=vector_store,
            embeddings_provider=mock_embeddings,
        )
        page_one = svc.keyword_search("Atlas", limit=2, offset=0)
        page_two = svc.keyword_search("Atlas", limit=2, offset=2)
        assert len(page_one) == 2
        assert len(page_two) == 2
        ids_one = {r.entry_id for r in page_one}
        ids_two = {r.entry_id for r in page_two}
        assert ids_one.isdisjoint(ids_two)

    def test_keyword_search_no_match(self, seeded_service):
        assert seeded_service.keyword_search("nonexistent") == []

    def test_keyword_search_score_ordering_stable(
        self, repo, vector_store, mock_embeddings
    ):
        """Scores should be decreasing across the returned list so
        clients that sort by score preserve FTS5 rank order."""
        for i in range(3):
            repo.create_entry(
                f"2026-03-{10 + i:02d}", "photo", f"Atlas entry {i}", 3
            )
        svc = QueryService(
            repository=repo,
            vector_store=vector_store,
            embeddings_provider=mock_embeddings,
        )
        results = svc.keyword_search("Atlas", limit=10)
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)


class TestStatsCollectorIntegration:
    """T1.2.a — QueryService forwards latency samples to the collector."""

    def test_stats_records_semantic_search(
        self, repo, vector_store, mock_embeddings
    ):
        from journal.services.stats import InMemoryStatsCollector

        stats = InMemoryStatsCollector()
        repo.create_entry("2026-03-22", "photo", "Vienna trip", 2)
        svc = QueryService(
            repository=repo,
            vector_store=vector_store,
            embeddings_provider=mock_embeddings,
            stats=stats,
        )
        svc.search_entries("vienna")
        snap = stats.snapshot()
        assert snap.total_queries == 1
        assert "semantic_search" in snap.by_type
        assert snap.by_type["semantic_search"].count == 1

    def test_stats_records_keyword_search(
        self, repo, vector_store, mock_embeddings
    ):
        from journal.services.stats import InMemoryStatsCollector

        stats = InMemoryStatsCollector()
        repo.create_entry("2026-03-22", "photo", "Vienna trip", 2)
        svc = QueryService(
            repository=repo,
            vector_store=vector_store,
            embeddings_provider=mock_embeddings,
            stats=stats,
        )
        svc.keyword_search("vienna")
        snap = stats.snapshot()
        assert snap.by_type["keyword_search"].count == 1

    def test_stats_records_statistics_mood_topic(
        self, repo, vector_store, mock_embeddings
    ):
        from journal.services.stats import InMemoryStatsCollector

        stats = InMemoryStatsCollector()
        repo.create_entry("2026-03-22", "photo", "Vienna trip", 2)
        svc = QueryService(
            repository=repo,
            vector_store=vector_store,
            embeddings_provider=mock_embeddings,
            stats=stats,
        )
        svc.get_statistics()
        svc.get_mood_trends()
        svc.get_topic_frequency("Vienna")
        snap = stats.snapshot()
        assert snap.by_type["statistics"].count == 1
        assert snap.by_type["mood_trends"].count == 1
        assert snap.by_type["topic_frequency"].count == 1
        assert snap.total_queries == 3

    def test_no_stats_is_passthrough(
        self, repo, vector_store, mock_embeddings
    ):
        """When stats is None, methods behave identically — no wrapper
        errors, no side effects."""
        svc = QueryService(
            repository=repo,
            vector_store=vector_store,
            embeddings_provider=mock_embeddings,
        )
        assert svc.search_entries("anything") == []
        assert svc.keyword_search("anything") == []
