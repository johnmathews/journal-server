"""Tests for query service."""

from unittest.mock import MagicMock

import pytest

from journal.db.repository import SQLiteEntryRepository
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
    e1 = repo.create_entry("2026-03-22", "ocr", "Walked through Vienna with Atlas", 5)
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


def test_search_entries_empty(query_service):
    results = query_service.search_entries("anything")
    assert results == []


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
