"""End-to-end ChromaVectorStore tests against a real ChromaDB.

Auto-skipped when Chroma is unreachable (see
``tests/integration/conftest.py`` for the TCP probe). To run locally::

    docker compose -f docker-compose.dev.yml up -d   # Chroma on :8401
    uv run pytest tests/integration

CI sets ``CHROMA_PORT=8000`` explicitly to hit its service container.
Local default is 8401 (the dev compose port).

The fixtures use a unique collection name per test so concurrent runs
don't collide and a failed test can't poison sibling collections.
Cleanup is best-effort in the teardown.

This suite covers all five public methods of ``ChromaVectorStore``:
``add_entry``, ``search``, ``delete_entry``, ``count``,
``get_chunks_for_entry``.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import TYPE_CHECKING

import pytest

from journal.vectorstore.store import ChromaVectorStore
from tests.integration.conftest import chroma_endpoint

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration


@pytest.fixture
def chroma_store() -> Iterator[ChromaVectorStore]:
    host, port = chroma_endpoint()
    collection = f"itest-{uuid.uuid4().hex[:12]}"
    store = ChromaVectorStore(host=host, port=port, collection_name=collection)
    yield store
    # Best-effort cleanup. We can't easily delete the collection via the
    # ChromaVectorStore surface, but we can clear all entries we added.
    # Each test that adds data uses entry_ids in a known range; the
    # delete_entry call inside each test handles its own cleanup, so the
    # teardown is a safety net rather than the primary path.
    # Reach the underlying client to drop the collection so we don't
    # accumulate hundreds of empty itest-* collections in CI. Best-effort
    # — a teardown failure must not mask a test failure.
    with contextlib.suppress(Exception):
        store._client.delete_collection(collection)  # noqa: SLF001


def _vec(dim: int = 4, *, fill: float = 0.0, mark: int | None = None) -> list[float]:
    """Build a small fixed-dimension test vector. ``mark`` sets a single
    component to 1.0 so different vectors are linearly distinguishable
    without bringing numpy into the test.
    """
    v = [fill] * dim
    if mark is not None:
        v[mark % dim] = 1.0
    return v


def test_count_starts_empty(chroma_store: ChromaVectorStore) -> None:
    assert chroma_store.count() == 0


def test_add_entry_then_count(chroma_store: ChromaVectorStore) -> None:
    chroma_store.add_entry(
        entry_id=1,
        chunks=["chunk a", "chunk b"],
        embeddings=[_vec(mark=0), _vec(mark=1)],
        metadata={"entry_date": "2026-05-07"},
    )
    assert chroma_store.count() == 2


def test_search_returns_nearest_chunk(chroma_store: ChromaVectorStore) -> None:
    chroma_store.add_entry(
        entry_id=1,
        chunks=["walked through Vienna", "stayed home and read"],
        embeddings=[_vec(mark=0), _vec(mark=1)],
        metadata={"entry_date": "2026-05-07"},
    )
    # Query vector aligned with chunk-0 → chunk-0 should be top.
    results = chroma_store.search(query_embedding=_vec(mark=0), limit=2)
    assert len(results) == 2
    assert results[0].chunk_text == "walked through Vienna"
    assert results[0].entry_id == 1


def test_search_respects_where_filter(chroma_store: ChromaVectorStore) -> None:
    chroma_store.add_entry(
        entry_id=1,
        chunks=["entry one"],
        embeddings=[_vec(mark=0)],
        metadata={"entry_date": "2026-05-07"},
    )
    chroma_store.add_entry(
        entry_id=2,
        chunks=["entry two"],
        embeddings=[_vec(mark=0)],
        metadata={"entry_date": "2026-05-08"},
    )
    results = chroma_store.search(
        query_embedding=_vec(mark=0), limit=10, where={"entry_id": 1},
    )
    assert {r.entry_id for r in results} == {1}


def test_search_empty_collection_returns_empty(
    chroma_store: ChromaVectorStore,
) -> None:
    results = chroma_store.search(query_embedding=_vec(mark=0), limit=5)
    assert results == []


def test_delete_entry_removes_chunks(chroma_store: ChromaVectorStore) -> None:
    chroma_store.add_entry(
        entry_id=1,
        chunks=["a", "b"],
        embeddings=[_vec(mark=0), _vec(mark=1)],
        metadata={"entry_date": "2026-05-07"},
    )
    chroma_store.add_entry(
        entry_id=2,
        chunks=["c"],
        embeddings=[_vec(mark=2)],
        metadata={"entry_date": "2026-05-08"},
    )
    assert chroma_store.count() == 3

    chroma_store.delete_entry(1)
    assert chroma_store.count() == 1

    survivors = chroma_store.search(query_embedding=_vec(mark=2), limit=5)
    assert {r.entry_id for r in survivors} == {2}


def test_get_chunks_for_entry_round_trip(
    chroma_store: ChromaVectorStore,
) -> None:
    chunks = ["first", "second", "third"]
    embeds = [_vec(mark=i) for i in range(len(chunks))]
    chroma_store.add_entry(
        entry_id=42,
        chunks=chunks,
        embeddings=embeds,
        metadata={"entry_date": "2026-05-07"},
    )
    records = chroma_store.get_chunks_for_entry(42)
    assert [r.text for r in records] == chunks
    assert [r.chunk_index for r in records] == [0, 1, 2]
    # Embeddings round-trip exactly (within the limits of float32 storage).
    for record, expected in zip(records, embeds, strict=True):
        assert len(record.embedding) == len(expected)
        for got, exp in zip(record.embedding, expected, strict=True):
            assert abs(got - exp) < 1e-5


def test_get_chunks_for_missing_entry_is_empty(
    chroma_store: ChromaVectorStore,
) -> None:
    assert chroma_store.get_chunks_for_entry(9999) == []
