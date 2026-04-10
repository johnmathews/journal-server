"""Tests for the chunking evaluator (WU-H)."""

import math

import pytest

from journal.db.repository import SQLiteEntryRepository
from journal.services.chunking_eval import (
    _cosine,
    _mean_pairwise_cosine,
    evaluate_chunking,
)
from journal.vectorstore.store import InMemoryVectorStore


@pytest.fixture
def repo(db_conn):
    return SQLiteEntryRepository(db_conn)


@pytest.fixture
def vector_store():
    return InMemoryVectorStore()


def _unit_vector(angle: float, dim: int = 4) -> list[float]:
    v = [0.0] * dim
    v[0] = math.cos(angle)
    v[1] = math.sin(angle)
    return v


class StubEmbeddings:
    def __init__(self):
        self.vectors: dict[str, list[float]] = {}

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self.vectors.get(t, [0.0, 0.0, 0.0, 0.0]) for t in texts]

    def embed_query(self, query: str) -> list[float]:
        return self.vectors.get(query, [0.0, 0.0, 0.0, 0.0])


class TestMeanPairwiseCosine:
    def test_single_vector_is_vacuously_coherent(self):
        assert _mean_pairwise_cosine([[1.0, 0.0]]) == 1.0

    def test_empty_list_is_vacuously_coherent(self):
        assert _mean_pairwise_cosine([]) == 1.0

    def test_two_identical_vectors_are_perfectly_coherent(self):
        assert _mean_pairwise_cosine([[1.0, 0.0], [1.0, 0.0]]) == pytest.approx(1.0)

    def test_two_orthogonal_vectors(self):
        assert _mean_pairwise_cosine([[1.0, 0.0], [0.0, 1.0]]) == pytest.approx(0.0)

    def test_three_vectors_mean_of_three_pairs(self):
        # Three vectors at 0, 60, 120 degrees.
        vs = [
            _unit_vector(0.0),
            _unit_vector(math.pi / 3),     # cos = 0.5 with v0
            _unit_vector(2 * math.pi / 3), # cos = -0.5 with v0
        ]
        # Pairwise sims: (v0,v1)=0.5, (v0,v2)=-0.5, (v1,v2)=0.5
        # Mean = (0.5 - 0.5 + 0.5) / 3 = 0.1667
        assert _mean_pairwise_cosine(vs) == pytest.approx(1 / 6, abs=1e-5)


class TestCosine:
    def test_identical_vectors(self):
        assert _cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        assert _cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_zero_vector_returns_zero(self):
        assert _cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


class TestEvaluateChunking:
    def test_empty_corpus_returns_defaults(self, repo, vector_store):
        result = evaluate_chunking(repo, vector_store, StubEmbeddings())
        assert result.n_entries_evaluated == 0
        assert result.n_chunks_evaluated == 0
        assert result.n_pairs_evaluated == 0

    def test_single_entry_single_chunk_with_coherent_sentences(
        self, repo, vector_store
    ):
        # One entry, one chunk with 2 sentences about the same topic.
        entry = repo.create_entry("2026-03-01", "ocr", "raw", 3)
        vector_store.add_entry(
            entry_id=entry.id,
            chunks=["Vienna was beautiful. The spring flowers bloomed."],
            embeddings=[_unit_vector(0.0)],
            metadata={"entry_date": "2026-03-01"},
        )

        stub = StubEmbeddings()
        # Both sentences point the same direction → cohesion = 1.0
        stub.vectors["Vienna was beautiful."] = _unit_vector(0.0)
        stub.vectors["The spring flowers bloomed."] = _unit_vector(0.0)

        result = evaluate_chunking(repo, vector_store, stub)
        assert result.n_entries_evaluated == 1
        assert result.n_chunks_evaluated == 1
        assert result.cohesion == pytest.approx(1.0)
        # Only one chunk → no adjacent pairs → separation defaults to 0.
        assert result.n_pairs_evaluated == 0

    def test_two_chunks_distinct_topics_high_separation(self, repo, vector_store):
        entry = repo.create_entry("2026-03-02", "ocr", "raw", 4)
        vector_store.add_entry(
            entry_id=entry.id,
            chunks=[
                "Coffee with Atlas today.",
                "Taxes are due next week.",
            ],
            # Orthogonal chunk centroids → high separation
            embeddings=[_unit_vector(0.0), _unit_vector(math.pi / 2)],
            metadata={"entry_date": "2026-03-02"},
        )

        stub = StubEmbeddings()
        stub.vectors["Coffee with Atlas today."] = _unit_vector(0.0)
        stub.vectors["Taxes are due next week."] = _unit_vector(math.pi / 2)

        result = evaluate_chunking(repo, vector_store, stub)
        assert result.n_pairs_evaluated == 1
        # Orthogonal vectors → cosine = 0 → separation = 1 - 0 = 1.0
        assert result.separation == pytest.approx(1.0, abs=1e-5)

    def test_cohesion_separation_ratio_math(self, repo, vector_store):
        # Craft a two-chunk entry where cohesion = 0.8, separation = 0.5
        # so ratio = 0.8 / 0.5 = 1.6.
        entry = repo.create_entry("2026-03-03", "ocr", "raw", 6)
        # Chunk A: 2 sentences with cosine sim = 0.8.
        # Chunk B: 2 sentences with cosine sim = 0.8 (same cohesion).
        # Chunk centroids at 60 degrees apart → sim = 0.5 → separation = 0.5.
        vector_store.add_entry(
            entry_id=entry.id,
            chunks=[
                "Sentence A1. Sentence A2.",
                "Sentence B1. Sentence B2.",
            ],
            embeddings=[_unit_vector(0.0), _unit_vector(math.pi / 3)],
            metadata={"entry_date": "2026-03-03"},
        )

        stub = StubEmbeddings()
        theta = math.acos(0.8)  # angle that gives cosine = 0.8
        stub.vectors["Sentence A1."] = _unit_vector(0.0)
        stub.vectors["Sentence A2."] = _unit_vector(theta)
        stub.vectors["Sentence B1."] = _unit_vector(0.0)
        stub.vectors["Sentence B2."] = _unit_vector(theta)

        result = evaluate_chunking(repo, vector_store, stub)
        assert result.cohesion == pytest.approx(0.8, abs=1e-5)
        assert result.separation == pytest.approx(0.5, abs=1e-5)
        assert result.ratio == pytest.approx(0.8 / 0.5, abs=1e-5)

    def test_single_sentence_chunks_get_perfect_cohesion(self, repo, vector_store):
        """Single-sentence chunks are trivially cohesive (can't be pairwise
        incoherent with zero other sentences). They should count with
        cohesion=1.0 rather than being ignored."""
        entry = repo.create_entry("2026-03-04", "ocr", "raw", 2)
        vector_store.add_entry(
            entry_id=entry.id,
            chunks=["One sentence.", "Another sentence."],
            embeddings=[_unit_vector(0.0), _unit_vector(math.pi / 2)],
            metadata={"entry_date": "2026-03-04"},
        )
        stub = StubEmbeddings()
        stub.vectors["One sentence."] = _unit_vector(0.0)
        stub.vectors["Another sentence."] = _unit_vector(math.pi / 2)

        result = evaluate_chunking(repo, vector_store, stub)
        # Two single-sentence chunks, both trivially cohesive.
        assert result.n_chunks_evaluated == 2
        assert result.cohesion == pytest.approx(1.0)

    def test_entry_with_no_chunks_is_skipped(self, repo, vector_store):
        # Entry exists but has no vectors in the store.
        repo.create_entry("2026-03-05", "ocr", "raw", 1)
        result = evaluate_chunking(repo, vector_store, StubEmbeddings())
        assert result.n_entries_evaluated == 0

    def test_ratio_is_comparable_across_runs(self, repo, vector_store):
        """Same corpus evaluated twice should produce the same numbers."""
        entry = repo.create_entry("2026-03-06", "ocr", "raw", 4)
        vector_store.add_entry(
            entry_id=entry.id,
            chunks=["First. Second.", "Third. Fourth."],
            embeddings=[_unit_vector(0.0), _unit_vector(math.pi / 2)],
            metadata={"entry_date": "2026-03-06"},
        )
        stub = StubEmbeddings()
        stub.vectors["First."] = _unit_vector(0.0)
        stub.vectors["Second."] = _unit_vector(0.0)
        stub.vectors["Third."] = _unit_vector(math.pi / 2)
        stub.vectors["Fourth."] = _unit_vector(math.pi / 2)

        a = evaluate_chunking(repo, vector_store, stub)
        b = evaluate_chunking(repo, vector_store, stub)
        assert a.cohesion == b.cohesion
        assert a.separation == b.separation
        assert a.ratio == b.ratio

    def test_as_dict_returns_all_fields(self, repo, vector_store):
        result = evaluate_chunking(repo, vector_store, StubEmbeddings())
        d = result.as_dict()
        assert set(d.keys()) == {
            "cohesion",
            "separation",
            "ratio",
            "n_chunks_evaluated",
            "n_entries_evaluated",
            "n_pairs_evaluated",
        }
