"""Tests for vector store using InMemoryVectorStore."""

import pytest

from journal.vectorstore.store import InMemoryVectorStore, VectorStore


@pytest.fixture
def store():
    return InMemoryVectorStore()


def test_implements_protocol():
    assert isinstance(InMemoryVectorStore(), VectorStore)


def test_add_and_count(store):
    store.add_entry(
        entry_id=1,
        chunks=["chunk one", "chunk two"],
        embeddings=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        metadata={"entry_date": "2026-03-22"},
    )
    assert store.count() == 2


def test_search_returns_nearest(store):
    store.add_entry(
        entry_id=1,
        chunks=["happy day in the park"],
        embeddings=[[1.0, 0.0, 0.0]],
        metadata={"entry_date": "2026-03-22"},
    )
    store.add_entry(
        entry_id=2,
        chunks=["sad rainy morning"],
        embeddings=[[0.0, 1.0, 0.0]],
        metadata={"entry_date": "2026-03-23"},
    )

    # Query close to entry 1
    results = store.search(query_embedding=[0.9, 0.1, 0.0], limit=1)
    assert len(results) == 1
    assert results[0].entry_id == 1
    assert results[0].chunk_text == "happy day in the park"


def test_search_with_filter(store):
    store.add_entry(
        entry_id=1,
        chunks=["old entry"],
        embeddings=[[1.0, 0.0, 0.0]],
        metadata={"entry_date": "2026-01-01"},
    )
    store.add_entry(
        entry_id=2,
        chunks=["new entry"],
        embeddings=[[0.9, 0.1, 0.0]],
        metadata={"entry_date": "2026-03-22"},
    )

    results = store.search(
        query_embedding=[1.0, 0.0, 0.0],
        where={"entry_date": "2026-03-22"},
    )
    assert len(results) == 1
    assert results[0].entry_id == 2


def test_delete_entry(store):
    store.add_entry(
        entry_id=1,
        chunks=["chunk a", "chunk b"],
        embeddings=[[1.0, 0.0], [0.0, 1.0]],
        metadata={"entry_date": "2026-03-22"},
    )
    assert store.count() == 2

    store.delete_entry(1)
    assert store.count() == 0


def test_search_empty_store(store):
    results = store.search(query_embedding=[1.0, 0.0, 0.0])
    assert results == []


def test_search_limit(store):
    for i in range(10):
        store.add_entry(
            entry_id=i,
            chunks=[f"entry {i}"],
            embeddings=[[float(i) / 10, 1.0 - float(i) / 10, 0.0]],
            metadata={"entry_date": f"2026-03-{i + 1:02d}"},
        )

    results = store.search(query_embedding=[1.0, 0.0, 0.0], limit=3)
    assert len(results) == 3
