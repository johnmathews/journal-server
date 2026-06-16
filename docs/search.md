# Search

Journal search uses a single, fixed pipeline: there is no user-visible
mode toggle and no per-request switch. Every call to `GET /api/search`
and to the `journal_search_entries` MCP tool runs the full hybrid
pipeline below.

## Architecture

```
                         query: str
                             │
        ┌────────────────────┴────────────────────┐
        ▼                                         ▼
 BM25 retriever                            Dense retriever
 SQLite FTS5 (entries_fts)                 ChromaDB (per-chunk
 entry-level, BM25 default                  embeddings,
 ranking, returns one row +                 cosine distance,
 FTS5 snippet per entry).                   returns chunks).
 top-N = HYBRID_BM25_CANDIDATES             top-N = HYBRID_DENSE_CANDIDATES
        │                                         │
        └────────────────┬────────────────────────┘
                         ▼
              project to entry-level
              (BM25 already is; dense → keep
              best chunk per entry as ranking
              signal, retain all chunks for
              display).
                         ▼
            RRF fusion  (k = HYBRID_RRF_K, default 60)
            → top-M entries (HYBRID_FUSION_TOP_M, default 30)
                         ▼
               L2 reranker (Reranker Protocol)
               input: query + entry text/snippet
               output: top-K = request `limit`
                         ▼
            response envelope (see below)
```

### Granularity: why entry-level

BM25 is at the entry level because `entries_fts` indexes whole-entry
`raw_text`. Dense retrieval is at the chunk level (Chroma stores one
embedding per chunk), but we project chunk hits to entries by
keeping the best-scoring chunk per entry as the ranking signal. We
do NOT add a chunk-level FTS5 index because:

- Chunks are ~150 tokens (`CHUNKING_MAX_TOKENS`) — too short for
  BM25's IDF statistics to be meaningful at this corpus size.
- The UI contract is already entry-with-matching-chunks, so
  fusing at entry level matches what the webapp renders.
- It would double FTS5 storage and add sync triggers on
  `entry_chunks`.

If eval data later shows that chunk-level retrieval would meaningfully
improve quality, adding a `chunks_fts` virtual table is a non-breaking
follow-up.

### Why no mode toggle

Earlier versions of this endpoint exposed `mode=keyword|semantic`.
That pushed the cognitive cost of "which retriever should I use"
onto the user. Hybrid search consistently beats either retriever
alone on the journal corpus's two failure modes:

- **Proper nouns** (people, places, gadgets) — BM25 catches what
  dense misses on novel vocabulary.
- **Paraphrased themes** ("stressed" finding "anxious") — dense
  catches what BM25 misses on lexical mismatch.

The `mode` parameter is now a hard `400 mode_removed` so any client
still passing it surfaces immediately.

## Configuration

All knobs are env-tunable; defaults match published guidance for
hybrid retrieval at this corpus scale (Cormack et al. for k=60;
OpenSearch / Azure AI Search for candidate counts).

| Env var | Default | What it controls |
|---|---|---|
| `HYBRID_BM25_CANDIDATES` | `50` | Max entries fetched from FTS5 in L1. |
| `HYBRID_DENSE_CANDIDATES` | `50` | Max chunks fetched from Chroma in L1. |
| `HYBRID_FUSION_TOP_M` | `30` | Entries kept after fusion, before rerank. |
| `HYBRID_RRF_K` | `60` | RRF damping constant. Lower = sharper top-rank preference. |
| `HYBRID_RERANKER` | `anthropic` | `anthropic` runs the L2 stage; `none` skips it. |
| `RERANKER_MODEL` | `claude-haiku-4-5` | Model used by `AnthropicReranker`. |

The current values are visible at `/api/settings` under the
`search` block.

## Reranker (L2)

The reranker scores fusion candidates against the query and returns
a trimmed top-K. It sits behind a Protocol so the implementation can
be swapped without touching the service:

```python
class Reranker(Protocol):
    def rerank(
        self, query: str, candidates: list[RerankCandidate], top_k: int,
    ) -> list[RerankResult]: ...
```

### Built-in adapters

- **`AnthropicReranker`** (default). Sends a single listwise prompt
  to a Claude model — by default `claude-haiku-4-5`. The model
  ranks the candidates and returns scored indices with one-line
  reasons. Latency: 200–500 ms typical. Cost: roughly $0.015 per
  search at 30 candidates × ~500 tokens each (~15K input tokens at
  Haiku 4.5's $1/MTok input rate, plus a small output term). The system prompt is
  marked `cache_control` for forward compatibility (current Haiku
  cache minimum is above the prompt size, so caching is currently
  a no-op).
- **`NoopReranker`**. Passes the fused candidates through unchanged,
  ordered by RRF score. Set `HYBRID_RERANKER=none` to skip the L2
  stage entirely. Useful for: benchmarking RRF in isolation; cutting
  latency at the cost of some precision; tests that don't care about
  rerank order.

Both adapters fall back transparently when something goes wrong:
a network/API error, malformed model output, or unparsable JSON
all degrade to RRF-only ordering rather than 500-ing the request.
The fallback is logged at WARN level so operators can see when it
happens.

### Adding a new reranker

1. Implement the `Reranker` Protocol in
   `src/journal/providers/reranker.py` (or a sibling module).
2. Register it in `build_reranker(name, ...)` in the same module.
3. Update the `HYBRID_RERANKER` allowed values in `config.py`'s
   docstring and in this document.
4. Add unit tests modelled on `tests/test_providers/test_reranker.py`.

A `VoyageReranker` (or `CohereReranker`, `JinaReranker`) using a
hosted cross-encoder API is the obvious next adapter when latency
becomes a concern — it's typically ~3× faster than an LLM listwise
rerank.

## Query parameters

| Parameter    | Required | Default      | Description                                                                                                                                                          |
| ------------ | -------- | ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `q`          | yes      |              | Search query. Must be non-empty after stripping whitespace. Returns `400 missing_query` otherwise. Treated as free-form natural language — see "Query sanitisation" below; punctuation never errors.                                                                   |
| `start_date` | no       |              | ISO date (`YYYY-MM-DD`) lower bound (inclusive) on `entry_date`.                                                                                                     |
| `end_date`   | no       |              | ISO date upper bound (inclusive).                                                                                                                                    |
| `limit`      | no       | `10`         | Page size. Range `1..50`. Out-of-range returns `400 invalid_query`.                                                                                                  |
| `offset`     | no       | `0`          | Page offset.                                                                                                                                                         |
| `sort`       | no       | `relevance`  | One of `relevance`, `date_desc`, `date_asc`. `relevance` returns the post-rerank order; `date_*` re-orders the same candidate list by entry date. Anything else returns `400 invalid_sort`. |

The `mode` parameter was retired when hybrid shipped; passing it returns `400 mode_removed`.

### Query sanitisation

`q` is free-form natural language ("when did my back start hurting?"),
but SQLite FTS5's `MATCH` grammar treats `?`, double quotes, `-`, `:`,
`*`, `(` / `)` and the bare booleans `AND` / `OR` / `NOT` as operators.
Passing the raw query to `MATCH` raised `sqlite3.OperationalError` on
anything that wasn't plain barewords — most visibly any question ending
in `?`.

The BM25 retriever now sanitises the query before it reaches FTS5
(`db/repository/search.py::_to_fts_match_query`): the query is tokenised
on whitespace, tokens with no word characters (a lone `?`, `--`) are
dropped, and each remaining token is wrapped as a quoted literal phrase
(embedded quotes escaped by doubling). Tokens are space-joined, which
FTS5 reads as implicit AND, so ordinary keyword queries behave as before.

Consequences:

- Punctuation is inert: no query can produce an FTS5 syntax error.
- A query with no word characters at all (e.g. `???`) yields no BM25
  hits; the dense retriever still runs, so the request still returns 200.
- Raw FTS5 operators are no longer honoured on the BM25 side — `vienna OR
  atlas` searches for the literal words `vienna`, `OR`, `atlas` rather
  than running a boolean OR, and `atl*` no longer prefix-matches. The
  web search box never advertised this syntax; the dense retriever and
  reranker carry semantic intent regardless.

### Result cache

A small in-memory LRU cache (64 entries, 5-minute TTL) is keyed by `(query, start_date, end_date, user_id)` and stores
the full reranked candidate list. `sort` and pagination are applied **on every call** to the cached candidates, so
paging through results does not re-run the BM25/dense/RRF/rerank pipeline. See `services/hybrid.py:_ResultCache`.

## Response envelope

```json
{
  "query": "vienna",
  "limit": 10,
  "offset": 0,
  "sort": "relevance",
  "reranker": "AnthropicReranker",
  "items": [
    {
      "entry_id": 42,
      "entry_date": "2026-03-22",
      "text": "Walked through Vienna with Atlas today …",
      "score": 0.91,
      "snippet": "Walked through Vienna with Atlas today.",
      "matching_chunks": [
        {
          "text": "Walked through Vienna with Atlas",
          "score": 0.83,
          "chunk_index": 0,
          "char_start": 0,
          "char_end": 33
        }
      ]
    }
  ]
}
```

- `score` is the post-rerank score on `[0.0, 1.0]`. It is not
  comparable across queries (the reranker doesn't promise calibrated
  scores) — only the within-result ordering is contract.
- `snippet` is populated when BM25 contributed to the match. The
  `\x02` (start) / `\x03` (end) ASCII control characters wrap
  matched terms (FTS5's `snippet()` output) and survive JSON
  serialisation. The webapp converts them to `<mark>` tags via
  `src/utils/searchSnippet.ts`.
- `matching_chunks` is populated when dense retrieval contributed,
  ordered by chunk similarity descending. `char_start` and
  `char_end` are absolute offsets into `text`, present only for
  entries that have rows in `entry_chunks` (everything ingested
  after migration 0003).
- `reranker` echoes the active L2 stage — useful for debugging and
  for cache busting on the webapp side. Will be the class name of
  the `Reranker` adapter (e.g. `AnthropicReranker`, `NoopReranker`).

## Errors

- `400 missing_query` — `q` parameter missing or whitespace-only.
- `400 mode_removed` — client passed `mode=`. The parameter was
  retired when hybrid shipped; drop it.
- `400 invalid_query` — defensive fallback if the BM25 retriever ever
  raises `sqlite3.OperationalError`. In practice the query is now
  sanitised before it reaches FTS5 (see "Query sanitisation"), so
  punctuation no longer triggers this — it remains only as a safety net
  that turns an unexpected FTS5 error into a 400 rather than a 500.
- `400 invalid_sort` — `sort` was something other than `relevance`,
  `date_desc`, or `date_asc`.
- `503 Server not initialized` — service registry is missing,
  typically during a startup race or test setup error.

## Answer synthesis (opt-in)

`POST /api/search/answer` synthesizes a short, grounded, cited answer to a
natural-language question. It is opt-in — the webapp only calls it when the
user clicks "Answer this", so the per-query LLM cost is never paid on a plain
search.

**Body:** `{q: str, start_date?: ISO, end_date?: ISO}` (same bearer auth as
`/api/search`).

**Flow:** reuse the hybrid search top-`ANSWER_CONTEXT_ENTRIES` (default 8) as
grounding → ask the answerer (`claude-sonnet-4-6`, adaptive thinking) for a
strictly-grounded JSON answer → resolve cited ids back to entries.

**Grounding contract:** the answerer may only use the supplied passages. If
they don't cover the question it returns `answered: false` with the fixed
message *"I couldn't find anything about that in your journal."* — it never
guesses.

**Response:**

```json
{
  "question": "when did my back start hurting?",
  "answer": "Your back pain first appears on 2026-02-14 …",
  "answered": true,
  "citations": [{"entry_id": 42, "entry_date": "2026-02-14", "snippet": "…"}],
  "model": "claude-sonnet-4-6"
}
```

**Config:** `ANSWER_PROVIDER` (`anthropic`|`none`, default `anthropic`),
`ANSWER_MODEL` (default `claude-sonnet-4-6`), `ANSWER_CONTEXT_ENTRIES`
(default 8).

**Errors:** `400 missing_query`; `502 answer_unavailable` (synthesis failed —
the client should fall back to the results list); `503` if synthesis is not
wired.
