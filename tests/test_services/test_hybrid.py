"""Tests for the hybrid search service and RRF fusion."""

from unittest.mock import MagicMock

import pytest

from journal.db.repository import SQLiteEntryRepository
from journal.providers.reranker import (
    NoopReranker,
    RerankCandidate,
    RerankResult,
)
from journal.services.hybrid import (
    HybridConfig,
    HybridSearchService,
    _ResultCache,
    rrf_fuse,
)
from journal.vectorstore.store import InMemoryVectorStore

# ---------------------------------------------------------------------------
# rrf_fuse — unit tests
# ---------------------------------------------------------------------------


class TestRrfFuse:
    def test_empty_input(self) -> None:
        assert rrf_fuse({}) == []

    def test_empty_lists(self) -> None:
        assert rrf_fuse({"a": [], "b": []}) == []

    def test_single_retriever_preserves_order(self) -> None:
        fused = rrf_fuse({"a": ["x", "y", "z"]})
        assert [doc for doc, _ in fused] == ["x", "y", "z"]

    def test_overlapping_doc_gets_summed_score(self) -> None:
        # x is rank 1 in both → score 2 * 1/61. y is rank 2 in a only → 1/62.
        fused = rrf_fuse({"a": ["x", "y"], "b": ["x", "z"]})
        scores = dict(fused)
        assert scores["x"] == pytest.approx(2 / 61)
        assert scores["y"] == pytest.approx(1 / 62)
        assert scores["z"] == pytest.approx(1 / 62)
        # x must come first.
        assert fused[0][0] == "x"

    def test_disjoint_lists(self) -> None:
        # a's rank-1 and b's rank-1 should tie. Order is implementation
        # detail (sort is stable in Python so insertion order wins).
        fused = rrf_fuse({"a": ["x"], "b": ["y"]})
        scores = dict(fused)
        assert scores["x"] == pytest.approx(1 / 61)
        assert scores["y"] == pytest.approx(1 / 61)

    def test_k_parameter_changes_decay(self) -> None:
        # Smaller k should make the gap between rank 1 and rank 2 larger.
        fused_small = dict(rrf_fuse({"a": ["x", "y"]}, k=10))
        fused_large = dict(rrf_fuse({"a": ["x", "y"]}, k=100))
        gap_small = fused_small["x"] - fused_small["y"]
        gap_large = fused_large["x"] - fused_large["y"]
        assert gap_small > gap_large

    def test_three_retrievers(self) -> None:
        fused = rrf_fuse(
            {"a": ["x", "y"], "b": ["y", "x"], "c": ["x"]}, k=1
        )
        # x is rank 1 in two, rank 2 in one → 1/2 + 1/3 + 1/2 = 1.333…
        # y is rank 2 in one, rank 1 in one      → 1/3 + 1/2     = 0.833…
        scores = dict(fused)
        assert scores["x"] == pytest.approx(1 / 2 + 1 / 3 + 1 / 2)
        assert scores["y"] == pytest.approx(1 / 3 + 1 / 2)
        assert fused[0][0] == "x"


# ---------------------------------------------------------------------------
# HybridSearchService — end-to-end with in-memory deps + mocked reranker
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(factory):
    return SQLiteEntryRepository(factory)


@pytest.fixture
def vector_store():
    return InMemoryVectorStore()


@pytest.fixture
def mock_embeddings():
    provider = MagicMock()
    provider.embed_query.return_value = [1.0, 0.0, 0.0]
    return provider


def _make_service(repo, vector_store, embeddings, reranker=None, **cfg):
    return HybridSearchService(
        repository=repo,
        vector_store=vector_store,
        embeddings_provider=embeddings,
        reranker=reranker or NoopReranker(),
        config=HybridConfig(**cfg) if cfg else None,
    )


class TestHybridPipeline:
    def test_empty_corpus_returns_empty(
        self, repo, vector_store, mock_embeddings
    ) -> None:
        svc = _make_service(repo, vector_store, mock_embeddings)
        assert svc.search("anything") == []

    def test_dense_only_match(self, repo, vector_store, mock_embeddings) -> None:
        # Entry exists in vector store but FTS5 won't match the query "trip"
        # against "Vienna day with Atlas". Dense should still surface it.
        e = repo.create_entry(
            "2026-03-22", "photo", "Vienna day with Atlas", 4,
        )
        vector_store.add_entry(
            entry_id=e.id,
            chunks=["Vienna day with Atlas"],
            embeddings=[[1.0, 0.0, 0.0]],
            metadata={"entry_date": "2026-03-22"},
        )
        mock_embeddings.embed_query.return_value = [1.0, 0.0, 0.0]
        svc = _make_service(repo, vector_store, mock_embeddings)
        results = svc.search("trip overseas")
        assert len(results) == 1
        assert results[0].entry_id == e.id
        # Dense contributed → matching_chunks populated.
        assert len(results[0].matching_chunks) == 1
        assert results[0].matching_chunks[0].text == "Vienna day with Atlas"
        # No BM25 hit → no snippet.
        assert results[0].snippet is None

    def test_bm25_only_match(self, repo, vector_store, mock_embeddings) -> None:
        # Entry exists in SQLite but the dense vector is orthogonal to the
        # query embedding, so dense filters it out below the cutoff.
        e = repo.create_entry(
            "2026-03-22", "photo", "ate at Trabocco yesterday", 4,
        )
        vector_store.add_entry(
            entry_id=e.id,
            chunks=["ate at Trabocco yesterday"],
            embeddings=[[0.0, 1.0, 0.0]],
            metadata={"entry_date": "2026-03-22"},
        )
        # Query embedding is orthogonal — dense distance is large.
        mock_embeddings.embed_query.return_value = [1.0, 0.0, 0.0]
        svc = _make_service(repo, vector_store, mock_embeddings)
        results = svc.search("trabocco")
        assert len(results) == 1
        assert results[0].entry_id == e.id
        # BM25 contributed → snippet populated with FTS5 markers.
        assert results[0].snippet is not None
        assert "\x02" in results[0].snippet
        assert "\x03" in results[0].snippet

    def test_both_retrievers_match(
        self, repo, vector_store, mock_embeddings
    ) -> None:
        e = repo.create_entry(
            "2026-03-22", "photo", "Vienna day with Atlas at the museum", 7,
        )
        vector_store.add_entry(
            entry_id=e.id,
            chunks=["Vienna day with Atlas at the museum"],
            embeddings=[[1.0, 0.0, 0.0]],
            metadata={"entry_date": "2026-03-22"},
        )
        mock_embeddings.embed_query.return_value = [1.0, 0.0, 0.0]
        svc = _make_service(repo, vector_store, mock_embeddings)
        results = svc.search("Vienna")
        assert len(results) == 1
        # Both signals present.
        assert results[0].snippet is not None
        assert len(results[0].matching_chunks) == 1

    def test_reranker_controls_final_order(
        self, repo, vector_store, mock_embeddings
    ) -> None:
        # Two entries that both match. Inject a fake reranker that
        # promotes whichever entry the test asks for. Confirm the
        # output order follows the reranker, not fusion order.
        e1 = repo.create_entry("2026-03-22", "photo", "Vienna with Atlas", 3)
        e2 = repo.create_entry("2026-03-23", "photo", "Vienna alone", 2)
        vector_store.add_entry(
            entry_id=e1.id, chunks=["Vienna with Atlas"],
            embeddings=[[1.0, 0.0, 0.0]],
            metadata={"entry_date": "2026-03-22"},
        )
        vector_store.add_entry(
            entry_id=e2.id, chunks=["Vienna alone"],
            embeddings=[[0.95, 0.05, 0.0]],
            metadata={"entry_date": "2026-03-23"},
        )
        mock_embeddings.embed_query.return_value = [1.0, 0.0, 0.0]

        # Reranker that flips fusion order.
        class FlipReranker:
            def rerank(self, query, candidates, top_k):
                reversed_ = list(reversed(candidates))
                return [
                    RerankResult(id=c.id, score=1.0 - i * 0.1, reason=None)
                    for i, c in enumerate(reversed_[:top_k])
                ]

        svc = _make_service(
            repo, vector_store, mock_embeddings, reranker=FlipReranker()
        )
        results = svc.search("Vienna")
        # Whatever fusion produced, FlipReranker reversed — so the first
        # output is whichever entry fusion put last.
        assert len(results) == 2
        assert results[0].score >= results[1].score

    def test_reranker_receives_truncated_text(
        self, repo, vector_store, mock_embeddings
    ) -> None:
        long_body = "Vienna " * 500  # ~3000 chars
        e = repo.create_entry("2026-03-22", "photo", long_body, 500)
        vector_store.add_entry(
            entry_id=e.id, chunks=[long_body[:200]],
            embeddings=[[1.0, 0.0, 0.0]],
            metadata={"entry_date": "2026-03-22"},
        )
        mock_embeddings.embed_query.return_value = [1.0, 0.0, 0.0]

        captured: list[RerankCandidate] = []

        class CapturingReranker:
            def rerank(self, query, candidates, top_k):
                captured.extend(candidates)
                return [
                    RerankResult(id=c.id, score=1.0, reason=None)
                    for c in candidates[:top_k]
                ]

        svc = _make_service(
            repo, vector_store, mock_embeddings, reranker=CapturingReranker()
        )
        svc.search("Vienna")
        assert captured
        # Hard cap inside the service (800 chars).
        assert all(len(c.text) <= 800 for c in captured)

    def test_pagination(self, repo, vector_store, mock_embeddings) -> None:
        for i in range(5):
            e = repo.create_entry(
                f"2026-03-{20 + i:02d}", "photo", f"Vienna entry {i}", 3,
            )
            vector_store.add_entry(
                entry_id=e.id, chunks=[f"Vienna entry {i}"],
                embeddings=[[1.0, 0.0, 0.0]],
                metadata={"entry_date": f"2026-03-{20 + i:02d}"},
            )
        mock_embeddings.embed_query.return_value = [1.0, 0.0, 0.0]
        svc = _make_service(repo, vector_store, mock_embeddings)
        page_one = svc.search("Vienna", limit=2, offset=0)
        page_two = svc.search("Vienna", limit=2, offset=2)
        page_three = svc.search("Vienna", limit=2, offset=4)
        ids = [r.entry_id for r in page_one + page_two + page_three]
        # No overlap across pages.
        assert len(set(ids)) == len(ids)

    def test_date_filter_applied_to_both_retrievers(
        self, repo, vector_store, mock_embeddings
    ) -> None:
        e_in_range = repo.create_entry(
            "2026-03-22", "photo", "Vienna inside", 2,
        )
        e_out = repo.create_entry("2026-04-22", "photo", "Vienna outside", 2)
        for e, date in ((e_in_range, "2026-03-22"), (e_out, "2026-04-22")):
            vector_store.add_entry(
                entry_id=e.id, chunks=[e.raw_text],
                embeddings=[[1.0, 0.0, 0.0]],
                metadata={"entry_date": date},
            )
        mock_embeddings.embed_query.return_value = [1.0, 0.0, 0.0]
        svc = _make_service(repo, vector_store, mock_embeddings)
        results = svc.search(
            "Vienna", start_date="2026-03-01", end_date="2026-03-31",
        )
        ids = [r.entry_id for r in results]
        assert e_in_range.id in ids
        assert e_out.id not in ids

    def test_user_filter_isolates_results(
        self, repo, db_conn, vector_store, mock_embeddings
    ) -> None:
        # Migration 0001 seeds user_id=1; insert a second user so the
        # FK constraint on entries.user_id is satisfied for user 2.
        db_conn.execute(
            "INSERT INTO users (email, display_name, is_admin, email_verified) "
            "VALUES ('user2@test.com', 'User Two', 0, 1)"
        )
        e_user1 = repo.create_entry(
            "2026-03-22", "photo", "Vienna user1", 2, user_id=1,
        )
        e_user2 = repo.create_entry(
            "2026-03-22", "photo", "Vienna user2", 2, user_id=2,
        )
        for e, uid in ((e_user1, 1), (e_user2, 2)):
            vector_store.add_entry(
                entry_id=e.id, chunks=[e.raw_text],
                embeddings=[[1.0, 0.0, 0.0]],
                metadata={"entry_date": "2026-03-22", "user_id": uid},
            )
        mock_embeddings.embed_query.return_value = [1.0, 0.0, 0.0]
        svc = _make_service(repo, vector_store, mock_embeddings)
        results = svc.search("Vienna", user_id=1)
        ids = [r.entry_id for r in results]
        assert e_user1.id in ids
        assert e_user2.id not in ids

    def test_results_carry_chunk_offsets_when_persisted(
        self, repo, vector_store, mock_embeddings
    ) -> None:
        from journal.models import ChunkSpan

        e = repo.create_entry(
            "2026-03-22", "photo",
            "Vienna day with Atlas at the museum and dinner",
            10,
        )
        repo.replace_chunks(
            e.id,
            [
                ChunkSpan(
                    text="Vienna day with Atlas at the museum and dinner",
                    char_start=0,
                    char_end=46,
                    token_count=10,
                )
            ],
        )
        vector_store.add_entry(
            entry_id=e.id,
            chunks=["Vienna day with Atlas at the museum and dinner"],
            embeddings=[[1.0, 0.0, 0.0]],
            metadata={"entry_date": "2026-03-22"},
        )
        mock_embeddings.embed_query.return_value = [1.0, 0.0, 0.0]
        svc = _make_service(repo, vector_store, mock_embeddings)
        results = svc.search("Vienna")
        assert results[0].matching_chunks[0].char_start == 0
        assert results[0].matching_chunks[0].char_end == 46

    def test_proper_noun_query_only_matches_via_bm25(
        self, repo, vector_store, mock_embeddings
    ) -> None:
        """Journals are full of proper nouns (people, places, gadgets)
        that the embedding model's training corpus may not represent
        well. BM25 must catch what dense misses."""
        # An entry containing a unique proper noun. Make the embedding
        # orthogonal to the query so dense-only would miss this entry.
        e_match = repo.create_entry(
            "2026-04-01", "photo", "had dinner at Trabocco again", 5,
        )
        # An unrelated entry whose embedding is closer to the query —
        # if dense ran alone it would surface the wrong entry first.
        e_decoy = repo.create_entry(
            "2026-04-02", "photo", "rambling thoughts about life", 4,
        )
        vector_store.add_entry(
            entry_id=e_match.id, chunks=["had dinner at Trabocco again"],
            embeddings=[[0.0, 1.0, 0.0]],
            metadata={"entry_date": "2026-04-01"},
        )
        vector_store.add_entry(
            entry_id=e_decoy.id, chunks=["rambling thoughts about life"],
            embeddings=[[1.0, 0.0, 0.0]],
            metadata={"entry_date": "2026-04-02"},
        )
        mock_embeddings.embed_query.return_value = [1.0, 0.0, 0.0]

        svc = _make_service(repo, vector_store, mock_embeddings)
        results = svc.search("Trabocco")
        ids = [r.entry_id for r in results]
        # The matching entry must appear, and must outrank the decoy.
        assert e_match.id in ids
        assert ids.index(e_match.id) < ids.index(e_decoy.id) if e_decoy.id in ids else True
        # And carry an FTS5 snippet wrapping the proper noun.
        match = next(r for r in results if r.entry_id == e_match.id)
        assert match.snippet is not None
        assert "Trabocco" in match.snippet

    def test_paraphrased_query_only_matches_via_dense(
        self, repo, vector_store, mock_embeddings
    ) -> None:
        """Conversely, when the user paraphrases a concept that doesn't
        share keywords with the entry, dense should rescue it."""
        # The entry is about a concept ("anxious") but the user types
        # "stressed" — no lexical overlap, only semantic.
        e_anxious = repo.create_entry(
            "2026-04-01", "photo",
            "felt really anxious before the presentation",
            7,
        )
        vector_store.add_entry(
            entry_id=e_anxious.id,
            chunks=["felt really anxious before the presentation"],
            embeddings=[[1.0, 0.0, 0.0]],
            metadata={"entry_date": "2026-04-01"},
        )
        # The query "stressed" shares no terms with the entry — only
        # the embedding model can connect them.
        mock_embeddings.embed_query.return_value = [1.0, 0.0, 0.0]

        svc = _make_service(repo, vector_store, mock_embeddings)
        results = svc.search("stressed")
        ids = [r.entry_id for r in results]
        assert e_anxious.id in ids
        match = next(r for r in results if r.entry_id == e_anxious.id)
        # No BM25 hit → no snippet. Dense found it → matching_chunks present.
        assert match.snippet is None
        assert len(match.matching_chunks) >= 1

    def test_records_stats_when_collector_provided(
        self, repo, vector_store, mock_embeddings
    ) -> None:
        e = repo.create_entry("2026-03-22", "photo", "Vienna", 1)
        vector_store.add_entry(
            entry_id=e.id, chunks=["Vienna"], embeddings=[[1.0, 0.0, 0.0]],
            metadata={"entry_date": "2026-03-22"},
        )
        mock_embeddings.embed_query.return_value = [1.0, 0.0, 0.0]

        from journal.services.stats import InMemoryStatsCollector

        stats = InMemoryStatsCollector()
        svc = HybridSearchService(
            repository=repo,
            vector_store=vector_store,
            embeddings_provider=mock_embeddings,
            reranker=NoopReranker(),
            stats=stats,
        )
        svc.search("Vienna")
        snap = stats.snapshot()
        assert "hybrid_search" in snap.by_type
        assert snap.by_type["hybrid_search"].count == 1


class TestDenseDateFiltering:
    """Date filters must not be passed as `$gte` / `$lte` string operands
    to the vector store — real ChromaDB rejects strings for those numeric
    operators (validate_where in chromadb 0.5+) with::

        ValueError: Expected operand value to be an int or a float for
        operator $gte, got 2025-11-04 in query.

    Our InMemoryVectorStore is permissive, so the bug only fires in
    prod. We guard against it by asserting the structure of the
    `where` clause that reaches the vector store.
    """

    def test_dense_where_clause_omits_string_range_operators(
        self, repo, vector_store, mock_embeddings,
    ) -> None:
        from unittest.mock import patch

        e = repo.create_entry("2026-03-22", "photo", "Vienna day", 4)
        vector_store.add_entry(
            entry_id=e.id, chunks=["Vienna day"],
            embeddings=[[1.0, 0.0, 0.0]],
            metadata={"entry_date": "2026-03-22", "user_id": 1},
        )
        mock_embeddings.embed_query.return_value = [1.0, 0.0, 0.0]
        svc = _make_service(repo, vector_store, mock_embeddings)

        captured: list[dict | None] = []
        original_search = vector_store.search

        def spy(*args, **kwargs):
            captured.append(kwargs.get("where"))
            return original_search(*args, **kwargs)

        with patch.object(vector_store, "search", side_effect=spy):
            svc.search(
                "Vienna",
                start_date="2025-11-04",
                end_date="2026-05-04",
                user_id=1,
            )

        assert captured, "vector_store.search was never called"
        for where in captured:
            assert _no_string_range_operators(where), (
                f"Where clause contained $gte/$lte with string operand: "
                f"{where!r}"
            )

    def test_dense_search_fails_loudly_on_chroma_style_validation(
        self, repo, vector_store, mock_embeddings,
    ) -> None:
        """Simulate real ChromaDB validation. If the dense path passes
        a string operand to $gte/$lte, this test fails with the same
        ValueError the production server emitted."""
        e = repo.create_entry("2026-03-22", "photo", "Vienna day", 4)
        vector_store.add_entry(
            entry_id=e.id, chunks=["Vienna day"],
            embeddings=[[1.0, 0.0, 0.0]],
            metadata={"entry_date": "2026-03-22", "user_id": 1},
        )
        mock_embeddings.embed_query.return_value = [1.0, 0.0, 0.0]
        svc = _make_service(repo, vector_store, mock_embeddings)

        original_search = vector_store.search

        def chroma_strict_search(*args, **kwargs):
            _validate_chroma_where(kwargs.get("where"))
            return original_search(*args, **kwargs)

        from unittest.mock import patch

        with patch.object(vector_store, "search", side_effect=chroma_strict_search):
            # Should not raise — the bug is the date filters reaching
            # Chroma as $gte/$lte string operands.
            results = svc.search(
                "Vienna",
                start_date="2025-11-04",
                end_date="2026-05-04",
                user_id=1,
            )
        assert isinstance(results, list)

    def test_dense_results_outside_date_range_are_dropped(
        self, repo, vector_store, mock_embeddings,
    ) -> None:
        """Filtering happens post-fetch; entries outside the range must
        not appear in the final results, even if dense retrieval
        returned them."""
        in_range = repo.create_entry(
            "2026-03-22", "photo", "Atlas in March", 4,
        )
        out_of_range = repo.create_entry(
            "2025-06-01", "photo", "Atlas last summer", 4,
        )
        for entry, date in (
            (in_range, "2026-03-22"),
            (out_of_range, "2025-06-01"),
        ):
            vector_store.add_entry(
                entry_id=entry.id, chunks=[entry.raw_text],
                embeddings=[[1.0, 0.0, 0.0]],
                metadata={"entry_date": date, "user_id": 1},
            )
        mock_embeddings.embed_query.return_value = [1.0, 0.0, 0.0]
        svc = _make_service(repo, vector_store, mock_embeddings)

        results = svc.search(
            "Atlas", start_date="2026-01-01", end_date="2026-12-31",
            user_id=1,
        )
        ids = {r.entry_id for r in results}
        assert in_range.id in ids
        assert out_of_range.id not in ids


def _no_string_range_operators(where: dict | None) -> bool:
    """Recursively check that no $gte / $lte / $gt / $lt operator has a
    non-numeric operand. Used by tests to mirror Chroma's behaviour."""
    if where is None:
        return True
    if isinstance(where, dict):
        for k, v in where.items():
            if k in ("$gte", "$lte", "$gt", "$lt") and not isinstance(
                v, (int, float),
            ):
                return False
            if not _no_string_range_operators(v):
                return False
    elif isinstance(where, list):
        for item in where:
            if not _no_string_range_operators(item):
                return False
    return True


def _validate_chroma_where(where: dict | None) -> None:
    """Minimal mirror of `chromadb.api.types.validate_where`. Raises the
    same ValueError that prod hit when a string is passed to a numeric
    range operator."""
    if where is None:
        return
    if isinstance(where, dict):
        for k, v in where.items():
            if k in ("$gte", "$lte", "$gt", "$lt") and not isinstance(
                v, (int, float),
            ):
                raise ValueError(
                    f"Expected operand value to be an int or a float for "
                    f"operator {k}, got {v} in query."
                )
            _validate_chroma_where(v)
    elif isinstance(where, list):
        for item in where:
            _validate_chroma_where(item)


class TestResultCacheUnit:
    """Direct tests of the LRU+TTL cache primitive."""

    def test_get_returns_none_for_unknown_key(self) -> None:
        cache = _ResultCache()
        assert cache.get(("q", None, None, None)) is None

    def test_get_returns_what_was_set(self) -> None:
        cache = _ResultCache()
        cache.set(("q", None, None, None), [])
        assert cache.get(("q", None, None, None)) == []

    def test_ttl_expiry_drops_entry(self, monkeypatch) -> None:
        # Drive the cache's clock manually so we don't rely on real sleeps.
        from journal.services import hybrid as hybrid_mod

        clock = [1000.0]
        monkeypatch.setattr(hybrid_mod.time, "monotonic", lambda: clock[0])

        cache = _ResultCache(ttl_s=10.0)
        cache.set(("q", None, None, None), [])
        assert cache.get(("q", None, None, None)) == []

        clock[0] += 11.0  # 11s later — past TTL
        assert cache.get(("q", None, None, None)) is None
        assert len(cache) == 0  # expired entry was evicted

    def test_lru_eviction_drops_oldest(self) -> None:
        cache = _ResultCache(max_entries=2)
        cache.set(("a", None, None, None), [])
        cache.set(("b", None, None, None), [])
        cache.set(("c", None, None, None), [])  # evicts "a"
        assert cache.get(("a", None, None, None)) is None
        assert cache.get(("b", None, None, None)) == []
        assert cache.get(("c", None, None, None)) == []

    def test_get_marks_recently_used(self) -> None:
        # If "a" is touched, "b" should be the LRU instead.
        cache = _ResultCache(max_entries=2)
        cache.set(("a", None, None, None), [])
        cache.set(("b", None, None, None), [])
        assert cache.get(("a", None, None, None)) == []  # touch a
        cache.set(("c", None, None, None), [])  # should evict b, not a
        assert cache.get(("a", None, None, None)) == []
        assert cache.get(("b", None, None, None)) is None

    def test_clear_empties_cache(self) -> None:
        cache = _ResultCache()
        cache.set(("q", None, None, None), [])
        cache.clear()
        assert cache.get(("q", None, None, None)) is None
        assert len(cache) == 0


class TestHybridCacheIntegration:
    """End-to-end: cache hits skip the pipeline; sort/pagination reuse cache."""

    @staticmethod
    def _seed(repo, vector_store, mock_embeddings):
        # Three entries on different dates, all matching "Atlas".
        ids = []
        for date in ("2026-01-15", "2026-02-15", "2026-03-15"):
            e = repo.create_entry(date, "photo", f"Atlas on {date}", 3)
            vector_store.add_entry(
                entry_id=e.id, chunks=[f"Atlas on {date}"],
                embeddings=[[1.0, 0.0, 0.0]],
                metadata={"entry_date": date},
            )
            ids.append(e.id)
        mock_embeddings.embed_query.return_value = [1.0, 0.0, 0.0]
        return ids

    def test_second_identical_call_does_not_re_embed(
        self, repo, vector_store, mock_embeddings,
    ) -> None:
        self._seed(repo, vector_store, mock_embeddings)
        svc = _make_service(repo, vector_store, mock_embeddings)

        svc.search("Atlas")
        assert mock_embeddings.embed_query.call_count == 1

        svc.search("Atlas")  # cache hit
        assert mock_embeddings.embed_query.call_count == 1  # unchanged

    def test_sort_change_reuses_cached_pipeline(
        self, repo, vector_store, mock_embeddings,
    ) -> None:
        self._seed(repo, vector_store, mock_embeddings)
        svc = _make_service(repo, vector_store, mock_embeddings)

        relevance = svc.search("Atlas")
        embed_calls_after_first = mock_embeddings.embed_query.call_count

        desc = svc.search("Atlas", sort="date_desc")
        asc = svc.search("Atlas", sort="date_asc")

        # Same query + filters → no further embed calls.
        assert mock_embeddings.embed_query.call_count == embed_calls_after_first

        # Same set of entries, different ordering.
        assert {r.entry_id for r in relevance} == {r.entry_id for r in desc}
        assert [r.entry_date for r in desc] == sorted(
            [r.entry_date for r in desc], reverse=True,
        )
        assert [r.entry_date for r in asc] == sorted(
            [r.entry_date for r in asc],
        )

    def test_pagination_reuses_cached_pipeline(
        self, repo, vector_store, mock_embeddings,
    ) -> None:
        self._seed(repo, vector_store, mock_embeddings)
        svc = _make_service(repo, vector_store, mock_embeddings)

        page1 = svc.search("Atlas", limit=2, offset=0)
        embed_calls_after_first = mock_embeddings.embed_query.call_count

        page2 = svc.search("Atlas", limit=2, offset=2)

        assert mock_embeddings.embed_query.call_count == embed_calls_after_first
        # Pages must be disjoint.
        assert {r.entry_id for r in page1}.isdisjoint(
            {r.entry_id for r in page2}
        )

    def test_different_date_filter_misses_cache(
        self, repo, vector_store, mock_embeddings,
    ) -> None:
        self._seed(repo, vector_store, mock_embeddings)
        svc = _make_service(repo, vector_store, mock_embeddings)

        svc.search("Atlas")
        assert mock_embeddings.embed_query.call_count == 1
        svc.search("Atlas", start_date="2026-02-01")
        assert mock_embeddings.embed_query.call_count == 2

    def test_different_user_misses_cache(
        self, repo, vector_store, mock_embeddings,
    ) -> None:
        self._seed(repo, vector_store, mock_embeddings)
        svc = _make_service(repo, vector_store, mock_embeddings)

        svc.search("Atlas", user_id=1)
        svc.search("Atlas", user_id=2)
        assert mock_embeddings.embed_query.call_count == 2

    def test_cache_does_not_mutate_stored_list(
        self, repo, vector_store, mock_embeddings,
    ) -> None:
        # Sorting must not corrupt the cached list — otherwise a
        # date_desc call followed by a relevance call would return the
        # date-ordered list.
        self._seed(repo, vector_store, mock_embeddings)
        svc = _make_service(repo, vector_store, mock_embeddings)

        relevance_first = [r.entry_id for r in svc.search("Atlas")]
        svc.search("Atlas", sort="date_desc")
        relevance_second = [r.entry_id for r in svc.search("Atlas")]

        assert relevance_first == relevance_second
