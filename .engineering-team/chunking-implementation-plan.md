# 	Chunking improvements — implementation plan

**Date:** 2026-04-10
**Author:** engineering-team (Claude Opus 4.6)
**Status:** approved (round 2) — ready to implement
**Parent discussion:** session transcript on chunk size / semantic chunking / parent-entry retrieval

## Executive summary

Move the journal-server chunking pipeline from fixed-token chunks to content-adaptive semantic chunking, add a rechunk CLI so we can iterate on strategies against the real DB without re-fetching source images, and add an eval CLI so chunking quality is quantitatively measurable. Backwards compatibility is explicitly not a goal — the user plans to nuke the DB and re-ingest, so we can rename config fields and drop deprecated shims freely.

Eight work units, one commit each, ordered by dependency:

1. **WU-A** — `pysbd`-backed sentence splitter (fixes Dr./a.m./decimals/em-dashes)
2. **WU-B** — `ChunkingStrategy` Protocol + `FixedTokenChunker` class (pure refactor; drops the `chunk_text` free function and the old `chunk_max_tokens` / `chunk_overlap_tokens` config names)
3. **WU-G** — Aggregate matching chunks per entry in `SearchResult` (lands before semantic chunking so we can *see* multi-chunk matches while tuning)
4. **WU-C** — `SemanticChunker` with adaptive tail overlap, numpy cosine, percentile-based cuts, min/max enforcement; config flag still defaults to `fixed`
5. **WU-E** — Date metadata prefix at embedding time (opt-in, default on)
6. **WU-D** — `journal rechunk` CLI command (re-chunks *and* re-embeds every entry)
7. **WU-H** — `journal eval-chunking` CLI (cohesion / separation / ratio, no ground truth needed)
8. *(manual validation — run rechunk + eval across several percentile values on real entries)*
9. **WU-F** — flip `CHUNKING_STRATEGY` default from `fixed` to `semantic`

That's 8 commits on `journal-server` main, with a single manual validation step before the last one.

---

## Why `pysbd`

`pysbd` = **Python Sentence Boundary Disambiguation**. Pure-Python, rule-based sentence segmenter. ~170 KB install, ~10 ms per entry, no ML model, no data download.

**What it handles correctly that the current `.!?` loop does not:**

| Input | Current behaviour | `pysbd` |
|-------|-------------------|---------|
| `"Dr. Smith went home."` | 2 sentences | 1 sentence |
| `"It was $3.14."` | 2 sentences | 1 sentence |
| `"We left at 7 a.m."` | 2 sentences | 1 sentence |
| `"So... I began."` | ~3 fragments | 1 sentence |
| `"i.e. the thing"` | 2 sentences | 1 sentence |
| `"J.F.K. was president."` | 4 sentences | 1 sentence |

**Why this matters for semantic chunking:** `SemanticChunker` embeds individual sentences, computes adjacent-sentence cosine similarities, and cuts where similarity dips. If the splitter produces junk sentence fragments, the embeddings are also junk, and the cut positions become arbitrary. Sentence quality is a direct multiplier on chunking quality.

**Why not alternatives:**
- `nltk.sent_tokenize` — works, but NLTK is a 50+ MB dependency with a separate `punkt` data download. Overkill.
- `spaCy` — even larger, needs a language model, slower.
- Clever regex — fragile in exactly the ways we're trying to fix.

`pysbd` is the least-work correct answer.

## Context recap

### Current state

- `src/journal/services/chunking.py::chunk_text(text, max_tokens=150, overlap_tokens=40)` — standalone function, paragraph-first splitting, naive character-level sentence fallback.
- `src/journal/services/ingestion.py::IngestionService._process_text` (line 156) calls `chunk_text` directly with params stored on the service.
- `src/journal/cli.py::_build_services` wires `config.chunk_max_tokens` / `config.chunk_overlap_tokens` into the service.
- `src/journal/config.py` hardcodes `chunk_max_tokens: int = 150` and `chunk_overlap_tokens: int = 40`, not read from environment.
- `src/journal/services/query.py::search_entries` dedupes by `entry_id` (line 61), returning one best chunk per unique entry and discarding other chunks in the same entry.
- `SearchResult` at `src/journal/models.py:57–62` has `text` (full entry) and `chunk_text` (single matching chunk).
- `src/journal/services/backfill.py::backfill_chunk_counts` recomputes counts but never regenerates embeddings.

### Observed problem

The user's real entries are ~600–800 tokens of stream-of-consciousness prose. On a representative sample the current chunker produces ~4 chunks that straddle topic boundaries (childhood-journaling + bio glued together; marriage + kids glued together), because fixed-token splitting is blind to topic shifts. The user's retrieval use case feeds chunks into an LLM, so embedding precision matters more than display coherence, and they're OK paying extra ingest cost for quality.

### Design targets

- Chunks sit between ~30 tokens (min) and ~300 tokens (max).
- Chunk boundaries land on topic shifts where possible.
- Pipeline is swappable via config flag — no ingestion code change to A/B.
- A `rechunk` CLI re-runs chunking against the existing DB without re-fetching OCR.
- Chunking quality is quantitatively measurable via an `eval-chunking` CLI.

---

## WU-A — `pysbd` sentence splitter

**Priority:** Medium. Prerequisite for WU-C.

### Files

| File | Change |
|------|--------|
| `pyproject.toml` | Add `"pysbd>=0.3,<1"` to `[project].dependencies`. |
| `uv.lock` | Regenerated by `uv sync`. |
| `src/journal/services/chunking.py` | Add module-level `split_sentences(text: str) -> list[str]` using `pysbd.Segmenter(language="en", clean=False)`. Replace the body of the sentence loop inside `_split_long_paragraph` to use it. |
| `tests/test_services/test_chunking.py` | Add `TestSplitSentences` class with cases for: abbreviations (`Dr.`, `Mr.`, `a.m.`, `i.e.`), decimals, time notation, em-dashes, ellipses, basic multi-sentence prose, empty string, single sentence, whitespace-only. |

### Acceptance criteria

1. `split_sentences("Dr. Smith went home.")` → `["Dr. Smith went home."]`
2. `split_sentences("It was 3.14 pi day.")` → `["It was 3.14 pi day."]`
3. `split_sentences("Hello. World.")` → `["Hello.", "World."]`
4. `split_sentences("")` → `[]`
5. Existing `TestChunkText` tests still pass unchanged.
6. Full suite green, ruff clean.

---

## WU-B — `ChunkingStrategy` Protocol + `FixedTokenChunker` (with cleanup)

**Priority:** High. Prerequisite for WU-C. Pure refactor plus opportunistic cleanup.

Since backwards compatibility is not a goal, this commit also:
- **Drops** the module-level `chunk_text` free function entirely
- **Renames** `chunk_max_tokens` → `chunking_max_tokens` and `chunk_overlap_tokens` → `chunking_overlap_tokens` in `config.py`, `IngestionService`, `cli.py`, `backfill.py`, and every test

### Files

| File | Change |
|------|--------|
| `src/journal/services/chunking.py` | Add `ChunkingStrategy` Protocol: `def chunk(self, text: str) -> list[str]: ...`. Add `FixedTokenChunker` class with `__init__(max_tokens: int = 150, overlap_tokens: int = 40)` and `chunk(text: str) -> list[str]`. Delete the free `chunk_text` function — move its body into the class method, keep `_split_long_paragraph` and `count_tokens` as module-level helpers. |
| `src/journal/config.py` | Rename `chunk_max_tokens` → `chunking_max_tokens`, `chunk_overlap_tokens` → `chunking_overlap_tokens`. |
| `src/journal/services/ingestion.py` | `IngestionService.__init__` signature changes: replace `chunk_max_tokens` + `chunk_overlap_tokens` with `chunker: ChunkingStrategy`. Drop the `_chunk_*` instance attributes. `_process_text` calls `self._chunker.chunk(text)`. |
| `src/journal/cli.py::_build_services` | Construct `FixedTokenChunker(config.chunking_max_tokens, config.chunking_overlap_tokens)` and pass to `IngestionService`. |
| `src/journal/services/backfill.py` | Update `backfill_chunk_counts` to accept a `ChunkingStrategy` instead of `max_tokens`/`overlap_tokens` kwargs. Update the CLI caller in `cli.py::cmd_backfill_chunks` accordingly. |
| `tests/test_services/test_ingestion.py` | Update the `ingestion_service` fixture to inject a `FixedTokenChunker`. Remove any tests referencing `chunk_max_tokens` as an IngestionService constructor arg. |
| `tests/test_services/test_chunking.py` | Rename `TestChunkText` → `TestFixedTokenChunker`. Tests assert the same outputs but via the class API. |
| `tests/test_services/test_backfill.py` | Update test signatures if they call `backfill_chunk_counts` with old kwargs. |

### Acceptance criteria

1. `FixedTokenChunker(150, 40).chunk(text)` produces the same output the old `chunk_text(text, 150, 40)` produced for every case in the existing test corpus.
2. No references to `chunk_text` (the function) remain in `src/` or `tests/`.
3. No references to `chunk_max_tokens` or `chunk_overlap_tokens` remain — all callers use the new `chunking_*` names.
4. Full suite green, ruff clean.

---

## WU-G — Aggregate matching chunks per entry in `SearchResult`

**Priority:** High. Independent of the chunking refactor; landing it before WU-C means we can *see* multi-chunk matches in the MCP tool output while tuning semantic chunking.

### Why

Today `QueryService.search_entries` dedupes vector results by `entry_id`, returning one best chunk per entry and discarding the rest. For a RAG consumer feeding chunks into an LLM, this throws away useful content: if a query matches three different passages in the same entry, only one survives. Option 3 from the discussion: group by entry, keep all matching chunks, expose them as a list.

### Data model change

```python
@dataclass
class ChunkMatch:
    text: str
    score: float

@dataclass
class SearchResult:
    entry_id: int
    entry_date: str
    text: str                                    # full parent entry (unchanged)
    score: float                                 # highest chunk score for this entry
    matching_chunks: list[ChunkMatch]            # NEW — all chunks that matched, ordered by score desc
    # chunk_text: str = ""  ← REMOVED (backwards compat not required)
```

### Files

| File | Change |
|------|--------|
| `src/journal/models.py` | Add `ChunkMatch` dataclass. Update `SearchResult`: add `matching_chunks: list[ChunkMatch]`, remove `chunk_text`. |
| `src/journal/services/query.py` | Rewrite the dedup loop at lines 57–77: group vector results by `entry_id`, build a `ChunkMatch` per matching chunk, sort each entry's chunks by score desc, compute entry-level `score` as the max chunk score, sort entries by top score desc. |
| `src/journal/mcp_server.py::journal_search_entries` | Update the display format (lines 148–156): show *all* matching chunks per entry, truncated to ~200 chars each, with individual scores. Drop the `r.chunk_text and r.chunk_text != r.text` guard since the field no longer exists. |
| `tests/test_services/test_query.py` | Update `test_search_entries` to assert the new shape. Add tests for: multiple chunks from one entry all appearing in `matching_chunks`; entries sorted by top score; chunks within an entry sorted by score. |

### Acceptance criteria

1. A query that matches 3 chunks in entry 1 and 2 chunks in entry 2 returns 2 `SearchResult`s (one per entry), with `matching_chunks` lists of length 3 and 2 respectively.
2. `SearchResult.score` equals `max(cm.score for cm in matching_chunks)`.
3. Entries in the result list are sorted by `score` desc.
4. `matching_chunks` within each entry are sorted by `score` desc.
5. MCP tool output shows every matching chunk, not just the top one.
6. Full suite green, ruff clean.

### Dependencies

None.

---

## WU-C — `SemanticChunker` with adaptive tail overlap

**Priority:** High. The main feature.

### Algorithm

```
SemanticChunker.chunk(text):
    sentences = split_sentences(text)            # from WU-A
    if len(sentences) <= 2:
        return [text.strip()] if text.strip() else []

    # 1. Embed every sentence in one batched call.
    sent_vectors = self._embeddings.embed_texts(sentences)

    # 2. Adjacent cosine similarities (numpy vectorised).
    vecs = np.array(sent_vectors, dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    normed = vecs / np.maximum(norms, 1e-12)
    sims = (normed[:-1] * normed[1:]).sum(axis=1)   # shape: (n-1,)

    # 3. Percentile-based thresholds.
    boundary_threshold = np.percentile(sims, self._boundary_percentile)
    decisive_threshold = np.percentile(sims, self._decisive_percentile)
    cut_positions = [i for i, s in enumerate(sims) if s <= boundary_threshold]

    # 4. For each cut, decide whether it's decisive or weak.
    #    Decisive cuts (sim <= decisive_threshold) are clean: no overlap.
    #    Weak cuts (decisive_threshold < sim <= boundary_threshold) are
    #    transition points: duplicate the boundary sentence into the next
    #    chunk as a lead-in.
    segments = _segment_with_adaptive_overlap(
        sentences, cut_positions, sims, decisive_threshold
    )

    # 5. Enforce min size — merge undersized segments into nearest neighbour.
    segments = _merge_undersized(segments, self._min_tokens)

    # 6. Enforce max size — any segment over max_tokens falls back to
    #    sentence-level token packing (reuses FixedTokenChunker's packing loop).
    segments = _split_oversized(segments, self._max_tokens)

    return [join_sentences(seg) for seg in segments]
```

### Adaptive tail overlap — detail

A cut is placed *after* sentence `i` (between `i` and `i+1`). Let `sim = sims[i]` be the adjacent similarity at that cut.

- **Decisive cut** (`sim <= decisive_threshold`): clean break. Chunk N ends at sentence `i`. Chunk N+1 starts at sentence `i+1`. No overlap.
- **Weak cut** (`decisive_threshold < sim <= boundary_threshold`): this is a transition point — the semantic shift is real enough to cut on, but not so sharp that the boundary sentence clearly belongs on one side. **Duplicate sentence `i`** as the first sentence of chunk N+1, so it appears in both chunks. Chunk N's embedding keeps its transition tail; chunk N+1 gets a lead-in from the transition.

Two new parameters on `SemanticChunker`:
- `boundary_percentile` (default 25) — primary cut threshold; `sims <= p25` become cut positions
- `decisive_percentile` (default 10) — below this is a clean cut; between 10 and 25 is a weak cut (gets the overlap)

At default values, 10/25 = **40% of cuts are decisive** (no overlap) and **60% are weak** (single-sentence tail overlap). Both values are config-driven so the user can tune the aggressiveness.

### Edge cases

- `len(sentences) <= 2` → return the whole text as one chunk.
- Empty / whitespace-only text → `[]`.
- `embed_texts` raises → propagate; ingestion's caller decides retry.
- A single sentence exceeds `max_tokens` → falls through to the oversized-segment pack, which calls the sentence-level packer. Worst case, the sentence becomes its own chunk slightly over max. Acceptable.
- All sims nearly equal (uniform similarity) → percentile cuts still fire, but they may not be meaningful. Rare in practice for journal prose. Not special-casing.

### Files

| File | Change |
|------|--------|
| `src/journal/config.py` | Add `chunking_strategy: str` (env `CHUNKING_STRATEGY`, default `"fixed"`), `chunking_min_tokens: int = 30` (env `CHUNKING_MIN_TOKENS`), `chunking_max_tokens_semantic: int = 300` (env — or reuse existing `chunking_max_tokens` with context-dependent default; decision: reuse `chunking_max_tokens` but bump default to 300 since semantic is becoming the main path; fixed users can still override), `chunking_boundary_percentile: int = 25` (env `CHUNKING_BOUNDARY_PERCENTILE`), `chunking_decisive_percentile: int = 10` (env `CHUNKING_DECISIVE_PERCENTILE`). |
| `src/journal/services/chunking.py` | Add `SemanticChunker` class implementing `ChunkingStrategy`. Constructor: `__init__(embeddings: EmbeddingsProvider, max_tokens: int = 300, min_tokens: int = 30, boundary_percentile: int = 25, decisive_percentile: int = 10)`. Add module-level helpers `_pairwise_cosine(vectors)`, `_segment_with_adaptive_overlap(...)`, `_merge_undersized(...)`, `_split_oversized(...)`. Add factory `build_chunker(config, embeddings) -> ChunkingStrategy`. Import numpy at the top of the file. |
| `src/journal/cli.py::_build_services` | Replace the direct `FixedTokenChunker(...)` construction with `build_chunker(config, embeddings)`. |
| `tests/test_services/test_chunking.py` | Add `TestSemanticChunker` class. Use a deterministic stub `EmbeddingsProvider` that returns hand-crafted vectors so we can assert exact cut positions. Tests:<br>— three sentences, s1&s2 similar, s3 unrelated → `["s1. s2.", "s3."]` (one cut, decisive)<br>— six sentences with one weak cut and one decisive cut → verify tail overlap on the weak cut only<br>— min-size merging<br>— max-size splitting with sentence-pack fallback<br>— 1- and 2-sentence entries short-circuit<br>— empty string returns `[]`<br>— `embed_texts` raising propagates |

### Acceptance criteria

1. With `chunking_strategy="fixed"` (default), ingestion output is bit-identical to WU-B's output.
2. With `chunking_strategy="semantic"` and the stub embedder, `SemanticChunker` produces cuts where the test vectors specify low similarity.
3. A weak cut (sim between the two thresholds) duplicates the boundary sentence into both adjacent chunks.
4. A decisive cut does not duplicate.
5. Min-size (30 tokens) merges; max-size (300 tokens) splits via the sentence-pack fallback.
6. Single-sentence and empty entries short-circuit correctly.
7. No `numpy` import added to any file *except* `chunking.py` (to contain the scope).
8. Full suite green, ruff clean.

### Dependencies

WU-A, WU-B.

---

## WU-E — Date metadata prefix at embed time

**Priority:** Medium. Cheap quality boost. Default on.

### Design

Prepend a short metadata header to each chunk *before embedding*, but store the **un-prefixed** chunk in ChromaDB's `documents` field so retrieval returns clean text.

```python
# Per chunk, during _process_text:
if self._embed_metadata_prefix:
    weekday = datetime.date.fromisoformat(date).strftime("%A")
    embed_inputs = [f"Date: {date}. {weekday}.\n\n{c}" for c in chunks]
else:
    embed_inputs = chunks
embeddings = self._embeddings.embed_texts(embed_inputs)
self._vector_store.add_entry(
    entry_id=entry_id,
    chunks=chunks,              # un-prefixed — what the user/LLM sees back
    embeddings=embeddings,      # computed from prefixed text
    metadata={"entry_date": date},
)
```

The prefix is ~10 tokens, adds ~3% to ingest embedding cost, and helps date-sensitive queries (`"what did I write about Robyn in February?"`) because the date is literally in the embedded text.

### Files

| File | Change |
|------|--------|
| `src/journal/config.py` | Add `chunking_embed_metadata_prefix: bool = True` (env `CHUNKING_EMBED_METADATA_PREFIX`, default `true`). |
| `src/journal/services/ingestion.py::_process_text` | Branch on the flag; build `embed_inputs` list; call `embed_texts` on the prefixed list; call `add_entry` with the un-prefixed chunks. Add an instance attribute `self._embed_metadata_prefix` set from constructor. |
| `src/journal/services/ingestion.py::IngestionService.__init__` | New constructor kwarg `embed_metadata_prefix: bool = True`. |
| `src/journal/cli.py::_build_services` | Pass `config.chunking_embed_metadata_prefix` through. |
| `tests/test_services/test_ingestion.py` | New `TestMetadataPrefix` class:<br>— with flag on, `embed_texts` receives prefixed strings, `add_entry` receives original chunks<br>— with flag off, both calls receive original chunks<br>— weekday name is correct (`2026-02-15` → `"Sunday"`) |

### Acceptance criteria

1. With flag on: `embed_texts` input strings all start with `"Date: YYYY-MM-DD. <Weekday>.\n\n"`.
2. With flag on: `add_entry` `chunks` parameter contains the un-prefixed chunk texts.
3. With flag off: both calls receive identical un-prefixed text.
4. Full suite green, ruff clean.

### Dependencies

WU-B (stable `_process_text` shape).

---

## WU-D — `journal rechunk` CLI command

**Priority:** High. Unlocks fast iteration.

### Files

| File | Change |
|------|--------|
| `src/journal/services/backfill.py` | Add `@dataclass RechunkResult { updated, skipped, errors, old_total_chunks, new_total_chunks }` and `rechunk_entries(ingestion_service, repository, *, dry_run=False) -> RechunkResult`. Loops entries, calls a new `IngestionService.rechunk_entry(entry_id)` method per entry, accumulates results. On `dry_run`, compute new chunks in memory but don't write to ChromaDB or SQLite. |
| `src/journal/services/ingestion.py` | Add `rechunk_entry(self, entry_id: int, *, dry_run: bool = False) -> int` method. Steps: `get_entry`, delete from vector store (unless dry run), run full `_process_text` pipeline, update `chunk_count` (unless dry run), return new chunk count. |
| `src/journal/cli.py` | Add `cmd_rechunk(args, config)` handler and register a `rechunk` subparser with `--dry-run` flag. Wire into the `commands` dict. |
| `tests/test_services/test_backfill.py` | Add `TestRechunkEntries` class. Mock ingestion service; assert per-entry calls; dry-run skips writes; errors on one entry don't abort the batch; `old_total_chunks` and `new_total_chunks` accurate. |
| `tests/test_services/test_ingestion.py` | Add `TestRechunkEntry` — use `InMemoryVectorStore`, ingest an entry, call `rechunk_entry`, assert the vector store has fresh chunks for that `entry_id` and the stored chunk count matches. |
| `tests/test_cli.py` | Add `test_cli_rechunk_help` and extend `test_cli_all_commands_registered` to include `rechunk`. |

### Acceptance criteria

1. `uv run journal rechunk` iterates every entry, re-runs the full pipeline with the current strategy, and reports a `RechunkResult`.
2. `uv run journal rechunk --dry-run` reports counts without writing to ChromaDB or SQLite.
3. Per-entry errors are captured in `RechunkResult.errors`; remaining entries still process.
4. After running `CHUNKING_STRATEGY=semantic uv run journal rechunk`, ChromaDB contains chunks produced by the semantic chunker for every entry.
5. Full suite green, ruff clean.

### Dependencies

WU-B, WU-C, WU-E (so the metadata prefix is automatically applied when rechunking).

---

## WU-H — `journal eval-chunking` CLI

**Priority:** Medium. Makes the manual validation step before WU-F objective instead of vibes-based.

### Metrics

Given the current corpus in ChromaDB:

1. **Cohesion** = mean over all chunks of: mean pairwise cosine similarity of the sentences inside that chunk. Higher = chunks are internally consistent.
2. **Separation** = mean over all adjacent chunk pairs (within the same entry) of: 1 − cosine(chunk_N_centroid, chunk_{N+1}_centroid). Higher = adjacent chunks are actually distinct.
3. **Ratio** = cohesion / (1 − separation). Higher = both coherent *and* distinct. Dimensionless, comparable across runs.

No ground truth needed. All computable from the already-stored embeddings plus one extra embed call per sentence (to get per-sentence vectors — or we can recompute by re-embedding chunks at sentence granularity, which is cheap).

### Algorithm

```
eval_chunking():
    total_cohesion, total_separation, n_chunks, n_pairs = 0, 0, 0, 0
    for entry in repo.list_entries(limit=big):
        chunks = vector_store.get_chunks_for_entry(entry.id)   # NEW helper
        if len(chunks) == 0: continue
        for chunk in chunks:
            sentences = split_sentences(chunk.text)
            if len(sentences) < 2:
                continue
            sent_vecs = embeddings.embed_texts(sentences)
            # cohesion = mean of upper-triangle cosine similarities
            c = _mean_pairwise_cosine(sent_vecs)
            total_cohesion += c
            n_chunks += 1
        # separation = 1 - cos(chunk_N_centroid, chunk_{N+1}_centroid)
        # chunk centroid = mean of the chunk's embedding (already in ChromaDB)
        for prev, curr in pairwise(chunks):
            s = 1 - cosine(prev.embedding, curr.embedding)
            total_separation += s
            n_pairs += 1
    cohesion = total_cohesion / max(n_chunks, 1)
    separation = total_separation / max(n_pairs, 1)
    ratio = cohesion / max(1 - separation, 1e-6)
    print(f"Cohesion:   {cohesion:.3f}")
    print(f"Separation: {separation:.3f}")
    print(f"Ratio:      {ratio:.3f}")
```

### Files

| File | Change |
|------|--------|
| `src/journal/services/chunking_eval.py` | New module. Implements `evaluate_chunking(repo, vector_store, embeddings) -> ChunkingEvalResult` dataclass with `cohesion`, `separation`, `ratio`, `n_chunks`, `n_entries_evaluated`. |
| `src/journal/vectorstore/store.py` | Add `get_chunks_for_entry(entry_id: int) -> list[ChunkRecord]` method to the Protocol and both implementations. Returns `ChunkRecord(text, embedding, score)` list. |
| `src/journal/cli.py` | Add `cmd_eval_chunking(args, config)` handler, register `eval-chunking` subparser (optional `--json` flag to emit machine-readable output). |
| `tests/test_services/test_chunking_eval.py` | New file. Test with a mock vector store returning controlled chunks/embeddings; assert cohesion/separation/ratio math is correct on a 2-chunk 4-sentence toy example. |
| `tests/test_cli.py` | `test_cli_eval_chunking_help` + add `eval-chunking` to `test_cli_all_commands_registered`. |
| `tests/test_vectorstore/test_store.py` | (if the file exists; otherwise add) test for `get_chunks_for_entry`. |

### Acceptance criteria

1. `uv run journal eval-chunking` prints cohesion, separation, ratio.
2. `uv run journal eval-chunking --json` writes structured output to stdout.
3. Running eval twice without changing data produces the same numbers.
4. Running rechunk with a different `CHUNKING_BOUNDARY_PERCENTILE` and re-running eval produces different numbers (confirms the metric is sensitive to chunking choices).
5. Full suite green, ruff clean.

### Dependencies

WU-A (split_sentences), WU-C (so there's something non-trivial to measure), WU-D (so we can rechunk between eval runs during tuning).

### Usage pattern during validation

```bash
for pct in 15 20 25 30 35; do
  CHUNKING_STRATEGY=semantic CHUNKING_BOUNDARY_PERCENTILE=$pct \
    uv run journal rechunk
  CHUNKING_STRATEGY=semantic CHUNKING_BOUNDARY_PERCENTILE=$pct \
    uv run journal eval-chunking
done
```

Pick the value with the highest ratio, set as the default.

---

## WU-F — Flip default strategy to `semantic`

**Priority:** One-line config change after manual validation.

### What

Change `config.py::chunking_strategy` default from `"fixed"` to `"semantic"`. Potentially also tune `chunking_boundary_percentile` / `chunking_decisive_percentile` to whatever the eval run showed was best. Capture the eval numbers in the dev journal entry.

### Acceptance criteria

1. Fresh ingestions use `SemanticChunker` by default with no env vars set.
2. Dev journal entry records: baseline eval numbers for fixed, best eval numbers for semantic, which percentile values were picked.
3. User has confirmed sample chunks on real data look better.

### Dependencies

WU-C, WU-D, WU-H, plus manual validation.

---

## Cross-cutting test plan

Beyond per-WU tests, before each commit:

1. **Full suite green:** `uv run pytest` exits 0. Current baseline: 166 tests passing. Post-plan target: ~210+.
2. **Coverage not regressed** (if `pyproject.toml` enforces a threshold).
3. **Ruff clean:** `uv run ruff check src/ tests/`.
4. **After WU-C:** manual smoke test — seed fresh data with `DB_PATH=.local-journal.db uv run journal seed`, run `DB_PATH=.local-journal.db CHUNKING_STRATEGY=semantic uv run journal rechunk`, hit `GET /api/entries` and verify chunk counts. Small (<100 token) seed entries should still be 1 chunk; longer entries should split.

## Cross-cutting documentation updates

| Doc | What to add |
|-----|-------------|
| `docs/architecture.md` | New "Chunking strategies" section explaining the Protocol, fixed vs semantic with adaptive overlap, and the metadata-prefix design. Cite file paths. Add "Search result aggregation" subsection noting the `matching_chunks` model. |
| `docs/development.md` | New command section for `rechunk` and `eval-chunking`. Document the `CHUNKING_STRATEGY` env var and the tuning loop. |
| `docs/configuration.md` | Document all new env vars: `CHUNKING_STRATEGY`, `CHUNKING_MIN_TOKENS`, `CHUNKING_MAX_TOKENS`, `CHUNKING_BOUNDARY_PERCENTILE`, `CHUNKING_DECISIVE_PERCENTILE`, `CHUNKING_EMBED_METADATA_PREFIX`. Note the rename of `CHUNK_MAX_TOKENS` → `CHUNKING_MAX_TOKENS` if it was ever env-exposed (it wasn't — the old config fields were hardcoded). |
| `docs/api.md` | Document the `matching_chunks` field in the search response shape (if / when a REST search route is added; currently there isn't one, but the MCP tool response format changes). |
| `journal/260410-semantic-chunking.md` | Dev journal entry: motivation, algorithm, adaptive overlap rationale, eval numbers, percentile choice, what was left out, follow-up ideas. |

## Commit sequence

1. **WU-A** — `Add pysbd sentence splitter`
2. **WU-B** — `Introduce ChunkingStrategy protocol; drop chunk_text function`
3. **WU-G** — `Aggregate matching chunks per entry in SearchResult`
4. **WU-C** — `Add SemanticChunker with adaptive tail overlap (behind config flag)`
5. **WU-E** — `Embed chunks with date metadata prefix`
6. **WU-D** — `Add rechunk CLI command`
7. **WU-H** — `Add eval-chunking CLI command`
8. *(manual validation)*
9. **WU-F** — `Flip default chunking strategy to semantic`

---

## Resolved design decisions (for the record)

- **Search dedup → option 3** (aggregate matching chunks per entry). Implemented in WU-G.
- **WU-E (metadata prefix) → in scope**, default on, ship now.
- **Overlap in semantic chunking → adaptive** (option 3 from the discussion). Decisive cuts are clean; weak cuts get single-sentence tail overlap to preserve transition context. Two percentile parameters so the user can tune aggressiveness.
- **Cosine → numpy** (already in the venv via chromadb transitive deps; vectorised matrix op).
- **Backwards compatibility → not required.** Rename config fields, drop old function, drop `SearchResult.chunk_text`.
- **Measurement → cohesion / separation / ratio** via WU-H. Cheap, automatic, no ground truth needed. Upgrade path to recall@K against a golden query set deferred.
- **`pysbd` → approved as a new dependency.**
- **Percentile defaults → 25 / 10**, both config-driven. Re-evaluate after manual validation.
- **Eight commits, one per WU.**
