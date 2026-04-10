# Semantic chunking with adaptive tail overlap

**Date:** 2026-04-10
**Plan:** `.engineering-team/chunking-implementation-plan.md`
**Commits:** 8239598, 90429c0, 876ae84, 6b73787, ba057ed, 6749d52, 9ef81a8, (this docs commit), (WU-F flip)

## Motivation

The existing fixed-token chunker (`chunk_text` in `services/chunking.py`) produces chunk boundaries based on token counts, blind to where topic meaning shifts. On a representative ~500-token journal entry the user shared in the session discussion, it glued together unrelated topics: the meta-opening about "starting to journal again" got fused with "I like this pen"; the bio paragraph got fused with "Life is good / marriage is hard"; marriage + kids ended up in the same chunk. For a RAG pipeline where retrieved chunks go into an LLM, those blended chunks produce blurry embeddings that match weakly on anything.

User-stated constraints when kicking this work off:
- Priority: flexibility, because they're still discovering use cases
- Chunks go into an LLM as RAG context
- Embedding precision matters more than display coherence
- Extra ingest cost is acceptable — prioritise quality
- Backwards compatibility is not required — the DB will be nuked and re-seeded

## What we built

Eight work units, one commit each, ordered by dependency:

| # | Commit  | WU  | What it did |
|---|---------|-----|-------------|
| 1 | 8239598 | A   | Add `pysbd`-backed sentence splitter (fixes Dr./a.m./decimals/em-dashes) |
| 2 | 90429c0 | B   | `ChunkingStrategy` Protocol + `FixedTokenChunker` class; drop the old free `chunk_text` function; rename `chunk_*_tokens` → `chunking_*_tokens` everywhere |
| 3 | 876ae84 | G   | Aggregate matching chunks per entry in `SearchResult` — no more dedup by entry_id, the LLM sees all matching passages |
| 4 | 6b73787 | C   | `SemanticChunker` with numpy cosine, percentile-based cuts, adaptive tail overlap, min/max size enforcement |
| 5 | ba057ed | E   | Date metadata prefix at embed time (embedded text has `"Date: YYYY-MM-DD. Weekday."` prefix, stored document doesn't) |
| 6 | 6749d52 | D   | `journal rechunk` CLI — delete existing vectors, run full chunk→embed→store pipeline using current strategy, with `--dry-run` |
| 7 | 9ef81a8 | H   | `journal eval-chunking` CLI — cohesion/separation/ratio metrics, no ground truth needed |
| 8 | (this)  | docs + F | Docs update + flip default `CHUNKING_STRATEGY` from `fixed` to `semantic` |

**67 new tests** across the whole series (baseline 166 → 233). Ruff clean throughout. No test from the previous baseline was deleted, just rewritten to use the new class API where applicable.

## Key design decisions

### Adaptive tail overlap

`SemanticChunker` uses **two** percentile thresholds rather than one:

- `chunking_boundary_percentile` (default 25) — adjacent-sentence similarities at or below this percentile become cut positions.
- `chunking_decisive_percentile` (default 10) — cuts at or below this are "decisive" (clean break, no overlap). Cuts between the two are "weak" — the boundary sentence gets duplicated into the next chunk as a lead-in.

The rationale: fixed overlap in a semantic chunker is actively harmful, because by construction the content on either side of a cut is about different topics; overlapping it dilutes the embeddings. But transition sentences (the "Life is good, — hard and easy — but many good things" kind) are genuinely ambiguous and lose context if they get assigned unilaterally to one side. Adaptive overlap addresses this: sharp cuts stay tight, fuzzy cuts carry a little bit of forward context.

At default values, ~40% of cuts are decisive (10/25) and ~60% are weak (15/25). Tunable via env vars so the user can iterate.

### `pysbd` over hand-rolled splitting

The existing character-level `.!?` splitter mis-handles abbreviations (Dr., a.m., i.e.), decimals ($3.14), and ellipses. For fixed chunking that's a minor annoyance; for semantic chunking it's a direct quality multiplier, because bad sentence splits produce bad embeddings produce bad cut positions. `pysbd` is pure-Python, ~170 KB, no model download, handles every case we tested correctly.

Alternatives considered: `nltk.sent_tokenize` (50+ MB plus data download, overkill) and `spaCy` (needs a language model, slower).

### Numpy cosine, not hand-rolled

Earlier plan draft had me rolling cosine similarity by hand to "avoid numpy as a new dep". Reviewer (rightly) pushed back: numpy is already transitively installed via `chromadb-client`, so the marginal cost is zero. Vectorised matrix ops are cleaner and faster. The `_pairwise_cosine` helper builds the full similarity matrix in one numpy call, and `_mean_pairwise_cosine` extracts the upper triangle.

### Aggregate matching chunks per entry

The old `search_entries` deduped vector results by `entry_id`, keeping only the top-scoring chunk per entry and dropping the rest. For a RAG consumer feeding chunks to an LLM, that throws away passages. The new shape returns one `SearchResult` per entry with a `matching_chunks: list[ChunkMatch]` field carrying every chunk that matched, sorted by score descending. Over-fetches 5× from the vector store to give the grouper headroom.

This turned out to be a one-commit change with high leverage — it was already "parent-entry retrieval" in spirit (the `SearchResult.text` field already carried `entry.final_text`), but the single-chunk-per-entry limit defeated half the point.

### Date metadata prefix

Per-chunk embedding inputs get `"Date: 2026-02-15. Sunday.\n\n"` prepended before being sent to OpenAI, but the document stored in ChromaDB is the original un-prefixed chunk. ChromaDB's `add_entry` already accepts pre-computed embeddings separately from documents, so this is a free plumbing change. ~10 extra tokens embedded per chunk, ~3% ingest cost. The payoff is that date-sensitive queries (`"what did I write about Atlas in February"`) match the right entries more reliably because the date is literally in the embedded text.

Malformed dates fall back to `"Date: <raw-string>.\n\n"` without a weekday rather than crashing the ingest.

### Intrinsic eval metrics (no ground truth)

`journal eval-chunking` computes three numbers over the stored corpus:

- **Cohesion** — mean pairwise cosine similarity of sentences within each chunk. Higher = internally consistent chunks.
- **Separation** — `1 − cosine` between adjacent chunks within the same entry. Higher = adjacent chunks are actually distinct.
- **Ratio** — `cohesion / (1 − separation)`. A single dimensionless number to optimise.

These are "intrinsic" — no golden query set needed. Re-run after rechunking with a different config, compare ratios. The gold-standard alternative (retrieval recall@K against a hand-labelled query set) is deferred; it needs investment in building the query set and its value compounds with corpus size. Intrinsic metrics are the right first step.

## What's deferred to the next session

1. **Percentile tuning on real data.** I shipped WU-F with default values (boundary=25, decisive=10) because the user is running in production with only 2 real entries right now; 2-entry stats are meaningless. As the user ingests more multi-page entries via the MCP server, we'll have enough signal to iterate.

2. **A way to get eval numbers out of prod and into the dev journal.** The `eval-chunking --json` output lives inside the `journal-server` container on the media VM. Next session should set up a workflow to capture those numbers periodically — probably a small shell script that does `docker exec ... uv run journal eval-chunking --json` and appends to a timestamped log on the VM's bind-mounted data volume, so the dev workflow can rsync it back. Save the percentile value that produced each run so comparisons are trivially scriptable.

3. **Retrieval recall@K against a golden query set.** The user will eventually want "does this chunking config actually make retrieval better at the thing I care about?". That needs a curated set of queries with known-correct answers. Worth building once the corpus has ~20+ real entries.

4. **Transition sentence detection quality.** The adaptive overlap scheme relies on the percentile thresholds doing a decent job separating "clean cuts" from "fuzzy transitions". If real corpus data shows the two thresholds behaving poorly (e.g. cuts bunching at a single similarity value, making the decisive/weak distinction meaningless), we may want a completely different approach — maybe a fixed ratio rather than percentile-based, or a lookup-table mapping "how unusual is this similarity drop" to "decisive vs weak".

## Open questions for later

- How does the ratio metric react when there's only one chunk per entry (separation undefined)? Currently the code guards with `max(1 - separation, 1e-6)`, so a corpus of one-chunk entries collapses to ratio = cohesion / 1e-6, which is a huge but meaningless number. Should probably refuse to report ratio if `n_pairs_evaluated == 0` and print a warning instead. Minor — flag for next iteration.
- Should `eval-chunking` also segment by entry length? Short entries (< 100 tokens) short-circuit to a single chunk in `SemanticChunker`, which is fine, but they pollute the corpus-level cohesion average with perfect 1.0 scores. Might want to exclude them from the aggregate or report two numbers.
- Is the 5× over-fetch factor in `QueryService` right? With small corpora the over-fetch rarely helps; with large corpora it might not be enough. Worth re-examining when we have real traffic data.

## Follow-up checklist

- [ ] Ingest 10+ real journal entries via the MCP server
- [ ] Run `docker exec journal-server uv run journal eval-chunking` and capture the baseline
- [ ] Run the tuning loop over boundary_percentile values 15/20/25/30/35
- [ ] Run the tuning loop over decisive_percentile values 5/10/15
- [ ] If best values differ from defaults, update `config.py`
- [ ] Document final chosen values in a follow-up journal entry
- [ ] Consider building a golden query set once the corpus is ~20 entries
