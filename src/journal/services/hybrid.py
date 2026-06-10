"""Hybrid search — BM25 + dense retrieval, RRF fusion, listwise rerank.

The pipeline is fixed: there is no user-visible mode toggle. Every
search request runs both retrievers in parallel, fuses the rankings
with Reciprocal Rank Fusion, and reranks the merged top-M candidates
with the configured `Reranker`.

Granularity is at the **entry** level. BM25 is already entry-level
(SQLite FTS5 indexes whole entry `raw_text`). Dense retrieval returns
chunks; we project them to entries by keeping the best-scoring chunk
per entry as the ranking signal, while preserving every matching
chunk for the caller to display.

Why entry-level fusion: chunks are ~150 tokens (CHUNKING_MAX_TOKENS),
which is too short for BM25's IDF statistics to be meaningful, and
the UI contract is already entry-with-matching-chunks. Adding a
chunk-level FTS5 index is a non-breaking follow-up if eval shows it
matters.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

from journal.models import ChunkMatch, SearchResult
from journal.providers.reranker import RerankCandidate, RerankResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from journal.db.repository import EntryRepository
    from journal.models import Entry
    from journal.providers.embeddings import EmbeddingsProvider
    from journal.providers.reranker import Reranker
    from journal.services.stats import StatsCollector
    from journal.vectorstore.store import VectorStore

log = logging.getLogger(__name__)

T = TypeVar("T")


# Per-candidate text length sent to the reranker. Picked to fit ~30
# candidates × ~600 chars in well under Haiku's 200K context, while
# still giving the model enough text to judge relevance. The reranker
# also caps internally — this is the soft cap, that is the hard cap.
_RERANK_TEXT_CHARS = 800


@dataclass(frozen=True)
class HybridConfig:
    """Tunable parameters for the hybrid search pipeline.

    All fields have sensible defaults aligned with published guidance
    (Cormack et al. for k=60; OpenSearch / Azure AI Search candidate
    counts). The `Config` dataclass populates these from env vars; the
    service accepts the dataclass directly so tests can override
    cleanly.
    """

    bm25_candidates: int = 50
    dense_candidates: int = 50
    fusion_top_m: int = 30
    rrf_k: int = 60


def rrf_fuse(
    rankings: dict[str, list[str]], k: int = 60
) -> list[tuple[str, float]]:
    """Fuse multiple ranked lists into a single ranking via RRF.

    `rankings` maps a retriever name → its top-N list of document IDs
    in rank order (best first). The function returns a list of
    `(doc_id, fused_score)` tuples, sorted by fused score descending.

    Score for each doc is the sum across retrievers of `1 / (k + rank)`
    where `rank` is the doc's 1-based position in that retriever's
    list. Documents missing from a retriever simply contribute zero
    from that retriever — no penalty.

    `k = 60` is the canonical value from Cormack et al. (2009) and
    remains the production default in OpenSearch, Azure AI Search,
    Weaviate, and ParadeDB. Lower k sharpens preference for top
    ranks; higher k flattens.
    """
    scores: dict[str, float] = {}
    for ranked in rankings.values():
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def _apply_sort_and_slice(
    results: list[SearchResult], sort: str, offset: int, limit: int,
) -> list[SearchResult]:
    """Apply final ordering and pagination to a reranked candidate list.

    Pure / cheap — runs on every search call (cache hit or miss) so
    sort and pagination don't trigger pipeline re-execution. Uses
    `sorted()` (not in-place) so the cached list is never mutated.
    """
    if sort == "date_desc":
        ordered = sorted(results, key=lambda r: r.entry_date, reverse=True)
    elif sort == "date_asc":
        ordered = sorted(results, key=lambda r: r.entry_date)
    else:
        ordered = results
    return ordered[offset : offset + limit]


@dataclass
class _DenseChunk:
    """One chunk hit from the dense retriever, pre-aggregation."""

    entry_id: int
    chunk_index: int | None
    text: str
    similarity: float  # 1.0 - cosine distance


# Cache key fields. We deliberately leave `sort`, `limit`, and `offset`
# out — the cached value is the full reranked candidate list, and sort
# and slicing are applied per-call. That's the whole point: changing
# the sort or paging through results doesn't re-run the pipeline.
_CacheKey = tuple[str, str | None, str | None, int | None]


class _ResultCache:
    """In-memory LRU + TTL cache for hybrid search results.

    Holds the full reranked candidate list for a given (query, dates,
    user) tuple. Sized for personal-scale traffic — a handful of
    distinct queries kept warm for ~5 minutes is plenty.

    Thread-safe via a single lock. The protected critical sections are
    short (dict ops only) so contention is negligible.
    """

    def __init__(self, max_entries: int = 64, ttl_s: float = 300.0) -> None:
        self._max = max_entries
        self._ttl = ttl_s
        self._data: OrderedDict[_CacheKey, tuple[float, list[SearchResult]]] = (
            OrderedDict()
        )
        self._lock = threading.Lock()

    def get(self, key: _CacheKey) -> list[SearchResult] | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            ts, results = entry
            if time.monotonic() - ts > self._ttl:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return results

    def set(self, key: _CacheKey, results: list[SearchResult]) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), results)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


class HybridSearchService:
    """Orchestrates BM25 + dense + RRF + rerank.

    Single public method: `search(query, ...)` returning a list of
    `SearchResult`. Each result carries:
      - The full entry text and entry-level score (post-rerank).
      - `snippet` populated when BM25 contributed to the match
        (FTS5 `\\x02`/`\\x03`-marked excerpt).
      - `matching_chunks` populated when dense retrieval contributed,
        sorted by similarity descending.

    Either or both may be present. Items where the rerank stage
    found the entry irrelevant are dropped before the slice.
    """

    def __init__(
        self,
        repository: EntryRepository,
        vector_store: VectorStore,
        embeddings_provider: EmbeddingsProvider,
        reranker: Reranker,
        config: HybridConfig | None = None,
        stats: StatsCollector | None = None,
        cache_max_entries: int = 64,
        cache_ttl_s: float = 300.0,
    ) -> None:
        self._repo = repository
        self._vector_store = vector_store
        self._embeddings = embeddings_provider
        self._reranker = reranker
        self._config = config or HybridConfig()
        self._stats = stats
        self._cache = _ResultCache(
            max_entries=cache_max_entries, ttl_s=cache_ttl_s,
        )

    @property
    def config(self) -> HybridConfig:
        return self._config

    @property
    def reranker(self) -> Reranker:
        return self._reranker

    def _timed(self, query_type: str, fn: Callable[[], T]) -> T:
        if self._stats is None:
            return fn()
        start = time.monotonic()
        try:
            return fn()
        finally:
            self._stats.record_query(
                query_type, (time.monotonic() - start) * 1000.0
            )

    def search(
        self,
        query: str,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 10,
        offset: int = 0,
        user_id: int | None = None,
        sort: str = "relevance",
    ) -> list[SearchResult]:
        """Run the hybrid pipeline and return paginated results."""
        return self._timed(
            "hybrid_search",
            lambda: self._search_impl(
                query, start_date, end_date, limit, offset, user_id, sort
            ),
        )

    @property
    def cache(self) -> _ResultCache:
        """Expose the result cache for diagnostics and admin."""
        return self._cache

    def _search_impl(
        self,
        query: str,
        start_date: str | None,
        end_date: str | None,
        limit: int,
        offset: int,
        user_id: int | None,
        sort: str,
    ) -> list[SearchResult]:
        cache_key: _CacheKey = (query, start_date, end_date, user_id)
        cached = self._cache.get(cache_key)
        if cached is not None:
            log.info(
                "Hybrid search cache hit: %r (sort=%s, offset=%d, limit=%d)",
                query, sort, offset, limit,
            )
            return _apply_sort_and_slice(cached, sort, offset, limit)

        log.info(
            "Hybrid search cache miss: %r (limit=%d, offset=%d)",
            query, limit, offset,
        )
        full = self._compute_full_results(query, start_date, end_date, user_id)
        self._cache.set(cache_key, full)
        return _apply_sort_and_slice(full, sort, offset, limit)

    def _compute_full_results(
        self,
        query: str,
        start_date: str | None,
        end_date: str | None,
        user_id: int | None,
    ) -> list[SearchResult]:
        """Run the full L1 + RRF + L2 pipeline. Expensive — cached by caller."""
        # ---- L1a: BM25 retrieval (entry-level) ----
        bm25_hits = self._repo.search_text_with_snippets(
            query=query,
            start_date=start_date,
            end_date=end_date,
            limit=self._config.bm25_candidates,
            offset=0,
            user_id=user_id,
        )
        bm25_ids: list[str] = [str(entry.id) for entry, _snip in bm25_hits]
        snippet_by_id: dict[str, str] = {
            str(entry.id): snip for entry, snip in bm25_hits
        }
        entries_by_id: dict[str, Entry] = {
            str(entry.id): entry for entry, _snip in bm25_hits
        }

        # ---- L1b: dense retrieval (chunk-level → projected to entry) ----
        dense_chunks = self._dense_search(query, start_date, end_date, user_id)
        chunks_by_entry: dict[str, list[_DenseChunk]] = {}
        for c in dense_chunks:
            chunks_by_entry.setdefault(str(c.entry_id), []).append(c)
        # Order each entry's chunks by similarity descending; dense entry
        # rank uses the top chunk's order from the vector store, which is
        # already by descending similarity, so the first occurrence per
        # entry IS the best chunk.
        dense_ids: list[str] = []
        seen: set[str] = set()
        for c in dense_chunks:
            eid = str(c.entry_id)
            if eid in seen:
                continue
            seen.add(eid)
            dense_ids.append(eid)
        for eid in chunks_by_entry:
            chunks_by_entry[eid].sort(key=lambda x: x.similarity, reverse=True)

        # ---- Fusion ----
        fused = rrf_fuse(
            {"bm25": bm25_ids, "dense": dense_ids}, k=self._config.rrf_k
        )
        fused = fused[: self._config.fusion_top_m]
        if not fused:
            return []

        # Resolve every fused entry id we don't already have an Entry for.
        # Filters (date / user_id) need to be re-applied here because dense
        # candidates were filtered in Chroma but a paranoid second pass is
        # cheap and prevents drift if filter semantics ever diverge.
        for eid_str, _score in fused:
            if eid_str in entries_by_id:
                continue
            entry = self._repo.get_entry(int(eid_str), user_id=user_id)
            if entry is None:
                continue
            entries_by_id[eid_str] = entry

        # ---- L2: rerank ----
        rerank_input: list[RerankCandidate] = []
        for eid_str, _fscore in fused:
            entry = entries_by_id.get(eid_str)
            if entry is None:
                continue
            text = self._candidate_text(entry, snippet_by_id.get(eid_str))
            rerank_input.append(RerankCandidate(id=eid_str, text=text))

        if not rerank_input:
            return []

        # Rerank to a window large enough to support the requested
        # offset+limit slice. We do not page the reranker — that would
        # require a stable cross-page ordering the rerank stage cannot
        # promise. Instead we rerank the full fused top-M and slice in
        # Python.
        try:
            reranked = self._reranker.rerank(
                query, rerank_input, top_k=len(rerank_input)
            )
        except Exception as e:  # noqa: BLE001 — reranker outages must not 500 search
            log.warning(
                "Rerank failed — falling back to fused order: %s", e
            )
            fused_scores = dict(fused)
            reranked = [
                RerankResult(id=c.id, score=fused_scores.get(c.id, 0.0))
                for c in rerank_input
            ]
        if not reranked:
            return []

        # ---- Build SearchResult objects in reranked order ----
        results: list[SearchResult] = []
        for rr in reranked:
            entry = entries_by_id.get(rr.id)
            if entry is None:
                continue
            chunks = self._build_chunk_matches(
                rr.id, chunks_by_entry.get(rr.id, [])
            )
            snippet = snippet_by_id.get(rr.id)
            results.append(
                SearchResult(
                    entry_id=entry.id,
                    entry_date=entry.entry_date,
                    text=entry.final_text or entry.raw_text,
                    score=rr.score,
                    matching_chunks=chunks,
                    snippet=snippet,
                )
            )

        return results

    def _dense_search(
        self,
        query: str,
        start_date: str | None,
        end_date: str | None,
        user_id: int | None,
    ) -> list[_DenseChunk]:
        """Embed the query and run a Chroma search with user filter,
        then drop chunks outside the date range in Python.

        Date filtering used to live in the Chroma `where` clause as
        `$gte` / `$lte` on the `entry_date` string metadata. Recent
        ChromaDB validates `$gte` / `$lte` operands as numeric and
        rejects strings outright, so dense retrieval blew up in prod
        whenever a date filter was set. We over-fetch unfiltered (still
        bounded by `dense_candidates`) and filter post-hoc against the
        chunk's `entry_date` metadata, which the BM25 path already
        does in SQL anyway.

        Dense retrieval depends on two external services (the
        embeddings API and the vector store). Either failing must not
        take search down — BM25 still works — so any exception here
        degrades the pipeline to BM25-only with a warning.
        """
        try:
            query_embedding = self._embeddings.embed_query(query)
            where = {"user_id": user_id} if user_id is not None else None

            raw = self._vector_store.search(
                query_embedding=query_embedding,
                limit=self._config.dense_candidates,
                where=where,
            )
        except Exception as e:  # noqa: BLE001 — provider outages must not 500 search
            log.warning(
                "Dense retrieval failed — degrading to BM25-only: %s", e
            )
            return []

        def in_range(metadata: dict) -> bool:
            entry_date = metadata.get("entry_date")
            if not isinstance(entry_date, str):
                # No date metadata — keep it; the entry-resolution pass
                # in `_search_impl` will drop it if SQLite says it's
                # out of range or the user can't see it.
                return True
            if start_date and entry_date < start_date:
                return False
            return not (end_date and entry_date > end_date)

        return [
            _DenseChunk(
                entry_id=r.entry_id,
                chunk_index=r.metadata.get("chunk_index"),
                text=r.chunk_text,
                similarity=1.0 - r.distance,
            )
            for r in raw
            if in_range(r.metadata)
        ]

    def _candidate_text(self, entry: Entry, snippet: str | None) -> str:
        """Pick the text the reranker reads to judge an entry.

        Preference order:
          1. The FTS5 snippet (already a focused excerpt around matched
             terms; cheap and concise).
          2. The first `_RERANK_TEXT_CHARS` of the entry text.
        """
        if snippet:
            cleaned = snippet.replace("\x02", "").replace("\x03", "")
            return cleaned[:_RERANK_TEXT_CHARS]
        text = entry.final_text or entry.raw_text or ""
        return text[:_RERANK_TEXT_CHARS]

    def _build_chunk_matches(
        self, entry_id_str: str, dense_chunks: list[_DenseChunk]
    ) -> list[ChunkMatch]:
        """Convert per-entry dense chunks into ChunkMatch objects.

        Looks up persisted char offsets via `entry_chunks` so the
        webapp can render in-place highlights. Entries ingested before
        migration 0003 have no persisted chunks; their `char_start`
        and `char_end` stay None.
        """
        if not dense_chunks:
            return []
        try:
            persisted = self._repo.get_chunks(int(entry_id_str))
        except Exception:  # noqa: BLE001 — repo errors must not 500 search
            log.warning(
                "Failed to load persisted chunks for entry %s; "
                "returning matches without offsets",
                entry_id_str,
            )
            persisted = []
        out: list[ChunkMatch] = []
        for c in dense_chunks:
            cm = ChunkMatch(
                text=c.text,
                score=c.similarity,
                chunk_index=c.chunk_index,
            )
            if (
                persisted
                and c.chunk_index is not None
                and 0 <= c.chunk_index < len(persisted)
            ):
                span = persisted[c.chunk_index]
                cm.char_start = span.char_start
                cm.char_end = span.char_end
            out.append(cm)
        return out
