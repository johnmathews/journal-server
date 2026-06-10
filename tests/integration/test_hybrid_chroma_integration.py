"""Hybrid search pipeline against a real ChromaDB.

The unit suite (``tests/test_services/test_hybrid.py``) exercises the
pipeline over ``InMemoryVectorStore``, which can only *simulate* what
Chroma does with the service's ``where`` clause and payload shapes.
These tests run the same ``HybridSearchService`` over a real
``ChromaVectorStore`` so the dense leg's round-trip — add, query with
a metadata filter, payload unmarshalling — is the production code
path end to end.

Auto-skipped when Chroma is unreachable (see
``tests/integration/conftest.py`` for the TCP probe). To run locally::

    docker compose -f docker-compose.dev.yml up -d   # Chroma on :8401
    uv run pytest tests/integration

Determinism without OpenAI: embeddings come from a keyword-keyed fake
(`KeywordEmbeddings`) that maps a small vocabulary onto unit basis
axes. A text's vector has 1.0 on the axis of every vocab keyword it
contains, so cosine similarity between a query and a document is a
pure function of shared keywords — no network, no model drift.

Entries are ingested through the real ``IngestionService.ingest_text``
path (with a MagicMock OCR/transcription provider that is never
called), so SQLite rows, persisted chunk offsets, and Chroma chunk
metadata (``entry_id``/``chunk_index``/``entry_date``/``user_id``) are
all written by production code rather than hand-rolled in the test.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from journal.db.repository import SQLiteEntryRepository
from journal.providers.reranker import NoopReranker, RerankResult
from journal.services.chunking import FixedTokenChunker
from journal.services.hybrid import HybridSearchService
from journal.services.ingestion import IngestionService
from journal.vectorstore.store import ChromaVectorStore
from tests.integration.conftest import chroma_endpoint

if TYPE_CHECKING:
    from collections.abc import Iterator

    from journal.db.factory import ConnectionFactory
    from journal.providers.reranker import RerankCandidate

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Deterministic collaborators
# ---------------------------------------------------------------------------

# Keyword → embedding axis. Two keywords on the same axis are
# "synonyms" (cosine-identical); keywords on different axes are
# orthogonal. The query in these tests is always "vienna" (axis 0).
_VOCAB: dict[str, int] = {
    "vienna": 0,
    "palace": 0,  # semantic synonym of "vienna" — matches dense, not FTS5
    "dynasty": 1,
    "trabocco": 2,
    "espresso": 3,
}
_DIM = max(_VOCAB.values()) + 2  # +1 noise axis for texts with no keyword


class KeywordEmbeddings:
    """Keyword-keyed fake ``EmbeddingsProvider``.

    The vector for a text has 1.0 at the axis of every vocab keyword
    it contains (case-insensitive substring). Texts with no keyword
    get a dedicated noise axis so the vector is never all-zero (cosine
    distance is undefined on zero vectors).
    """

    def _embed(self, text: str) -> list[float]:
        lowered = text.lower()
        vec = [0.0] * _DIM
        for word, axis in _VOCAB.items():
            if word in lowered:
                vec[axis] = 1.0
        if not any(vec):
            vec[-1] = 1.0
        return vec

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, query: str) -> list[float]:
        return self._embed(query)


class FlipReranker:
    """Reverses fused candidate order (same shape as the unit-suite fake)."""

    def rerank(
        self, query: str, candidates: list[RerankCandidate], top_k: int
    ) -> list[RerankResult]:
        reversed_ = list(reversed(candidates))
        return [
            RerankResult(id=c.id, score=1.0 - i * 0.1, reason=None)
            for i, c in enumerate(reversed_[:top_k])
        ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def chroma_store() -> Iterator[ChromaVectorStore]:
    """Real ChromaVectorStore on a unique collection, dropped on teardown."""
    host, port = chroma_endpoint()
    collection = f"itest-hybrid-{uuid.uuid4().hex[:12]}"
    store = ChromaVectorStore(host=host, port=port, collection_name=collection)
    yield store
    # Best-effort cleanup so CI doesn't accumulate itest-* collections;
    # a teardown failure must not mask a test failure.
    with contextlib.suppress(Exception):
        store._client.delete_collection(collection)  # noqa: SLF001


@pytest.fixture
def repo(factory: ConnectionFactory) -> SQLiteEntryRepository:
    return SQLiteEntryRepository(factory)


@pytest.fixture
def embeddings() -> KeywordEmbeddings:
    return KeywordEmbeddings()


@pytest.fixture
def ingestion(
    repo: SQLiteEntryRepository,
    chroma_store: ChromaVectorStore,
    embeddings: KeywordEmbeddings,
) -> IngestionService:
    """Production ingestion wiring; OCR/transcription are never called
    by the ``ingest_text`` path these tests use.
    """
    return IngestionService(
        repository=repo,
        vector_store=chroma_store,
        ocr_provider=MagicMock(),
        transcription_provider=MagicMock(),
        embeddings_provider=embeddings,
        chunker=FixedTokenChunker(),
    )


def _make_service(
    repo: SQLiteEntryRepository,
    chroma_store: ChromaVectorStore,
    embeddings: KeywordEmbeddings,
    reranker: object | None = None,
) -> HybridSearchService:
    return HybridSearchService(
        repository=repo,
        vector_store=chroma_store,
        embeddings_provider=embeddings,
        reranker=reranker or NoopReranker(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fusion_end_to_end_both_matcher_ranks_first(
    repo: SQLiteEntryRepository,
    chroma_store: ChromaVectorStore,
    embeddings: KeywordEmbeddings,
    ingestion: IngestionService,
) -> None:
    """Dense + BM25 fusion over real Chroma payloads.

    Three entries against the query "vienna":
      - BOTH:     literal "Vienna" (FTS5 hit) and a pure axis-0 vector
                  (dense rank 1, similarity 1.0).
      - SEMANTIC: "palace … dynasty" — axis {0,1} vector (dense
                  similarity ≈ 0.707) but no literal "vienna", so FTS5
                  misses it.
      - LEXICAL:  literal "Vienna" but diluted across axes {0,2,3}
                  (dense similarity ≈ 0.577, dense rank 3).

    RRF must put BOTH first: it is the only entry with a top rank in
    *both* legs, and its fused score strictly dominates regardless of
    BM25's internal ordering of the two lexical hits.
    """
    both = ingestion.ingest_text(
        "Walked around Vienna all afternoon", "2026-03-22",
    )
    semantic = ingestion.ingest_text(
        "The palace gardens and stories of the old dynasty", "2026-03-23",
    )
    lexical = ingestion.ingest_text(
        "Vienna again: trabocco dinner then a late espresso", "2026-03-24",
    )

    svc = _make_service(repo, chroma_store, embeddings)
    results = svc.search("vienna")

    assert [r.entry_id for r in results][:1] == [both.id]
    assert {r.entry_id for r in results} == {both.id, semantic.id, lexical.id}

    by_id = {r.entry_id: r for r in results}

    # BOTH: snippet from FTS5 + matching chunks from the real Chroma
    # payload, carrying persisted char offsets written at ingest time.
    top = by_id[both.id]
    assert top.snippet is not None
    assert len(top.matching_chunks) == 1
    chunk = top.matching_chunks[0]
    assert chunk.text == "Walked around Vienna all afternoon"
    assert chunk.chunk_index == 0
    assert chunk.char_start is not None
    assert chunk.char_end is not None

    # SEMANTIC: dense-only — chunks came back from Chroma, no snippet.
    assert by_id[semantic.id].snippet is None
    assert len(by_id[semantic.id].matching_chunks) == 1

    # LEXICAL: FTS5 contributed a snippet.
    assert by_id[lexical.id].snippet is not None


def test_date_filtered_dense_leg_excludes_out_of_range(
    repo: SQLiteEntryRepository,
    chroma_store: ChromaVectorStore,
    embeddings: KeywordEmbeddings,
    ingestion: IngestionService,
) -> None:
    """Real Chroma accepts the where clause the service builds.

    None of the entries contains the literal query term, so every
    result must come from the dense leg. Passing ``user_id`` makes
    ``_dense_search`` send ``where={"user_id": 1}`` to the real
    Chroma — if Chroma rejected it, the service would silently degrade
    to BM25-only and return nothing, so a non-empty result is itself
    the acceptance proof. Date bounds are then applied to the dense
    candidates and must drop the out-of-range entries.
    """
    early = ingestion.ingest_text("The palace gardens at dawn", "2026-03-20")
    middle = ingestion.ingest_text(
        "Back to the palace for the concert", "2026-03-22",
    )
    late = ingestion.ingest_text("Palace courtyard in the rain", "2026-03-24")

    svc = _make_service(repo, chroma_store, embeddings)

    # Sanity: without date bounds the dense leg surfaces all three.
    unfiltered = svc.search("vienna", user_id=1)
    assert {r.entry_id for r in unfiltered} == {early.id, middle.id, late.id}

    results = svc.search(
        "vienna", start_date="2026-03-21", end_date="2026-03-23", user_id=1,
    )
    assert [r.entry_id for r in results] == [middle.id]
    # Dense leg executed for real (no degradation): chunks present.
    assert len(results[0].matching_chunks) == 1


def test_flip_reranker_reverses_fused_order(
    repo: SQLiteEntryRepository,
    chroma_store: ChromaVectorStore,
    embeddings: KeywordEmbeddings,
    ingestion: IngestionService,
) -> None:
    """Reranker controls final order over candidates from real Chroma.

    Deterministic fused order: A matches both legs (dense similarity
    1.0 + FTS5), B matches dense only at similarity ≈ 0.707 — so
    fusion yields [A, B]. FlipReranker reverses it; getting [B, A]
    back proves both candidates survived the real round-trip *and*
    that the rerank stage, not retrieval, decides the output order.
    """
    entry_a = ingestion.ingest_text(
        "Walked around Vienna all afternoon", "2026-03-22",
    )
    entry_b = ingestion.ingest_text(
        "The palace gardens and stories of the old dynasty", "2026-03-23",
    )

    svc = _make_service(repo, chroma_store, embeddings, reranker=FlipReranker())
    results = svc.search("vienna")

    assert [r.entry_id for r in results] == [entry_b.id, entry_a.id]
    assert results[0].score >= results[1].score
