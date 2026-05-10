# API Reference

## REST API Endpoints

The journal server exposes REST API endpoints alongside the MCP protocol, both on the same port. These endpoints are
registered via `mcp.custom_route()` and served by the same Starlette/ASGI application. The route handlers live under
`src/journal/api/` (entries, ingestion, search, dashboard, jobs, entities, entity_merge, settings, users, health,
notifications) and `src/journal/auth_api/` (core, account, profile, api_keys, admin).

CORS is configurable via the `API_CORS_ORIGINS` environment variable (see [configuration.md](configuration.md)).

## Authentication

The server applies a single auth middleware stack (`src/journal/auth.py`) to **every** REST and MCP route. Two
authentication schemes are accepted, checked in order:

1. **Session cookie** — `session_id`, issued by `POST /api/auth/login` (or `POST /api/auth/register`). The cookie is
   set with `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`, and a 7-day `Max-Age`. The raw token is held only in the
   cookie; the server stores a SHA-256 hash of the session id in the `user_sessions` table. This is the path the webapp
   uses.
2. **Bearer API key** — `Authorization: Bearer <key>` header. Used by MCP clients and direct REST consumers. Keys are
   created by `POST /api/auth/api-keys` and shown to the caller exactly once. Like session ids, only a hash of the key
   is stored server-side.

Successful auth attaches an `AuthenticatedUser` to `request.user` and exposes `user_id` to MCP tool handlers via a
`ContextVar`. After auth, the middleware enforces:

- `403 forbidden` (`"Account is disabled"`) when `is_active` is false.
- `403 forbidden` (`"Please verify your email"`) when `email_verified` is false and the path is not in
  `VERIFICATION_EXEMPT_PATHS` (`/api/auth/me`, `/api/auth/logout`, `/api/auth/resend-verification`).
- `401 unauthorized` (`{"error": "unauthorized", ...}`) for everything else without valid credentials.

### Public paths

The following paths bypass auth entirely (`PUBLIC_PATHS` in `auth.py`):

- `/health`
- `/api/auth/login`
- `/api/auth/register`
- `/api/auth/config`
- `/api/auth/forgot-password`
- `/api/auth/verify-reset-token`
- `/api/auth/reset-password`
- `/api/auth/verify-email`

`OPTIONS` requests always pass (CORS preflight). For the full auth model — registration toggle, password reset
lifecycle, email verification, session/API key data shapes — see [`auth.md`](auth.md).

## Conventions

### Error envelopes

Most modern endpoints (auth, search, dashboard, ingestion, settings, users) return JSON of the form:

```json
{ "error": "<machine_code>", "message": "<human readable text>" }
```

with HTTP status `400` (validation), `401` (auth required / invalid credentials), `403` (forbidden / verification
required), `404` (not found), `409` (conflict — entry has active jobs, alias collides, mood reload disabled), `413`
(payload too large), or `503` (server still booting / dependency unavailable). Older endpoints — `/api/stats`, the
entity CRUD/aliases routes, and a handful of admin reload routes — emit a single-key `{"error": "..."}` body with no
`message` field and no machine code; treat both shapes as possible.

### Date format

All `entry_date`, `start_date`, `end_date`, `from`, and `to` parameters use ISO 8601 calendar dates (`YYYY-MM-DD`).
Timestamps in responses (e.g. `created_at`, `updated_at`, `started_at`) are ISO 8601 datetimes, usually with explicit
UTC offset.

### Pagination

List endpoints accept `limit` and `offset` query parameters. Defaults vary by endpoint (typically 20 or 50, capped
between 50 and 200) and are documented per route. Responses use a consistent
`{"items": [...], "total": N, "limit": L, "offset": O}` envelope where applicable.

### GET /api/entries

List entries with pagination and optional date filtering.

**Query parameters:**

| Parameter    | Type   | Required | Default | Description                  |
| ------------ | ------ | -------- | ------- | ---------------------------- |
| `start_date` | string | no       |         | Filter from date (ISO 8601)  |
| `end_date`   | string | no       |         | Filter until date (ISO 8601) |
| `limit`      | int    | no       | 20      | Max results (1-100)          |
| `offset`     | int    | no       | 0       | Pagination offset            |

**Response (200):**

```json
{
 "items": [
  {
   "id": 1,
   "entry_date": "2026-04-09",
   "source_type": "photo",
   "page_count": 2,
   "word_count": 450,
   "chunk_count": 5,
   "uncertain_span_count": 1,
   "doubts_verified": false,
   "created_at": "2026-04-09T10:30:00",
   "language": "en",
   "updated_at": "2026-04-09T11:00:00",
   "entity_mention_count": 3
  }
 ],
 "total": 42,
 "limit": 20,
 "offset": 0
}
```

### GET /api/entries/{id}

Get a single entry with full text.

**Response (200):**

```json
{
 "id": 1,
 "entry_date": "2026-04-09",
 "source_type": "photo",
 "raw_text": "original OCR output...",
 "final_text": "corrected text...",
 "page_count": 2,
 "word_count": 450,
 "chunk_count": 5,
 "language": "en",
 "created_at": "2026-04-09T10:30:00",
 "updated_at": "2026-04-09T11:00:00",
 "doubts_verified": false,
 "uncertain_spans": [
  { "char_start": 6, "char_end": 12 },
  { "char_start": 18, "char_end": 24 }
 ]
}
```

`uncertain_spans` is a list of half-open `[char_start, char_end)` character ranges into `raw_text` — each pair covers one
word or short phrase the OCR model flagged as uncertain at ingestion time. Entries ingested before migration `0005`
return an empty array. The field is always present; callers never need to check for its existence. See
[`ocr-context.md`](ocr-context.md) for the sentinel protocol and the webapp's Review toggle for the consumer UI.

`uncertain_spans` is preserved across `PATCH /api/entries/{id}` — edits change `final_text` but leave `raw_text` and its
span list untouched.

`doubts_verified` indicates whether the user has confirmed all OCR doubts are correct via
`POST /api/entries/{id}/verify-doubts`. When `true`, the API returns `uncertain_spans: []` and `uncertain_span_count: 0`
even though the underlying span rows are preserved in the database for future analysis (e.g., glossary enrichment,
accuracy tracking).

**Response (404):**

```json
{ "error": "not_found", "message": "Entry 999 not found" }
```

### PATCH /api/entries/{id}

Update an entry's `final_text` and/or `entry_date`. At least one field must be provided. When `final_text` is updated,
triggers re-chunking, re-embedding, FTS5 rebuild, and an async entity re-extraction job.

**Request body (all fields optional, at least one required):**

```json
{
 "final_text": "corrected text...",
 "entry_date": "2026-02-17"
}
```

| Field        | Type   | Required | Description                        |
| ------------ | ------ | -------- | ---------------------------------- |
| `final_text` | string | no\*     | Corrected text (triggers re-embed) |
| `entry_date` | string | no\*     | ISO 8601 date (YYYY-MM-DD)         |

\* At least one of `final_text` or `entry_date` must be provided.

**Response (200):** Updated entry detail (same shape as GET /api/entries/{id}). When `final_text` was updated, the
response includes an additional `entity_extraction_job_id` field (string) with the ID of the queued background extraction
job. Omitted when only `entry_date` was changed or if the job could not be queued.

**Response (400):**

```json
{ "error": "At least one of 'final_text' or 'entry_date' is required" }
```

**Response (404):**

```json
{ "error": "not_found", "message": "Entry 999 not found" }
```

### POST /api/entries/{id}/verify-doubts

Mark all OCR doubts on an entry as verified and correct. Sets `doubts_verified = true` on the entry. The underlying
uncertain span rows are preserved in the database for future analysis. After verification, GET and list endpoints return
`uncertain_span_count: 0` and an empty `uncertain_spans` array.

**Request body:** None required.

**Response (200):** Updated entry detail (same shape as GET /api/entries/{id}) with `doubts_verified: true` and
`uncertain_spans: []`.

**Response (404):**

```json
{ "error": "Entry 999 not found" }
```

### DELETE /api/entries/{id}

Delete an entry. Removes the SQLite row (cascading to pages, tags, people, places, mood scores, and source files) and
purges the entry's chunks from the vector store.

If the entry has queued or running background jobs (entity extraction, mood scoring, etc.), deletion is blocked with a
**409 Conflict** to prevent race conditions where a job tries to write to a deleted entry.

**Response (200):**

```json
{ "deleted": true, "id": 1 }
```

**Response (404):**

```json
{ "error": "Entry 999 not found" }
```

**Response (409):**

```json
{
  "error": "Entry has active jobs",
  "message": "Entry 1 has 2 running/queued job(s). Wait for them to finish before deleting.",
  "job_ids": ["uuid-1", "uuid-2"]
}
```

### GET /api/entries/{id}/chunks

Return the persisted chunks for an entry, with each chunk's source character range and token count. Used by the webapp
overlay to draw chunk boundaries on top of the entry text.

Chunks are persisted to SQLite (`entry_chunks` table) at ingestion time, so this endpoint is a straight SELECT — the
chunker is not re-run. Chunks produced before migration 0003 are not automatically populated; re-ingest the entry or run
the backfill service to populate them.

**Response (200):**

```json
{
 "entry_id": 1,
 "chunks": [
  {
   "index": 0,
   "text": "First chunk text, normalised paragraph separators.",
   "char_start": 0,
   "char_end": 51,
   "token_count": 14
  },
  {
   "index": 1,
   "text": "Second chunk picks up where the first ends.",
   "char_start": 53,
   "char_end": 96,
   "token_count": 12
  }
 ]
}
```

`char_start` / `char_end` are character offsets into the entry's `final_text` (or `raw_text` as fallback). `char_end` is
exclusive. Slicing `final_text[char_start:char_end]` yields the source range the chunk covers — which may include
slightly more whitespace than `text`, because paragraph and sentence separators are normalised when the chunk was
rendered.

**Response (404, entry not found):**

```json
{
 "error": "entry_not_found",
 "message": "Entry 999 not found"
}
```

**Response (404, chunks not backfilled):**

```json
{
 "error": "chunks_not_backfilled",
 "message": "This entry was ingested before chunk persistence was available. Re-ingest the entry or run the backfill service to populate chunks."
}
```

### GET /api/entries/{id}/tokens

Tokenise an entry's text on demand using tiktoken `cl100k_base` — the encoding that matches `text-embedding-3-large`.
Returns per-token records including the token ID, the token text, and the character range the token covers in
`final_text`.

Computed per request — the call is cheap (< 10 ms for a ~2000-word entry) and avoids any cache invalidation when the user
edits the entry's text.

**Response (200):**

```json
{
 "entry_id": 1,
 "encoding": "cl100k_base",
 "model_hint": "text-embedding-3-large",
 "token_count": 357,
 "tokens": [
  {
   "index": 0,
   "token_id": 9906,
   "text": "Hello",
   "char_start": 0,
   "char_end": 5
  },
  {
   "index": 1,
   "token_id": 1917,
   "text": " world",
   "char_start": 5,
   "char_end": 11
  }
 ]
}
```

For valid UTF-8 input the offsets slice `final_text` exactly — concatenating
`[final_text[t.char_start:t.char_end] for t in tokens]` reconstructs the entry text. Note that leading whitespace is part
of the token (e.g. `" world"` above), which matches how the embedding model sees the text.

**Response (404):**

```json
{
 "error": "entry_not_found",
 "message": "Entry 999 not found"
}
```

### GET /health

Operational health endpoint. **Unauthenticated** — the server binds to loopback only (see `docs/security.md`), so any
caller that can reach `/health` already has a shell on the box. The field that would worry us most — most-frequent search
terms — is deliberately **not** exposed. The query stats block carries counts-by-type only, not query strings.

If you ever front this server with a reverse proxy, exclude `/health` from the public route or scrub the
`queries.by_type` block before serving it outside loopback.

**Response (200):**

```json
{
 "status": "ok",
 "checks": [
  { "name": "sqlite", "status": "ok", "detail": "SELECT 1 succeeded", "error": null },
  { "name": "chromadb", "status": "ok", "detail": "collection count = 0", "error": null },
  { "name": "anthropic", "status": "ok", "detail": "anthropic API key is configured (51 chars)", "error": null },
  { "name": "openai", "status": "degraded", "detail": "openai API key is not configured", "error": null }
 ],
 "ingestion": {
  "total_entries": 42,
  "entries_last_7d": 3,
  "entries_last_30d": 12,
  "by_source_type": { "photo": 30, "voice": 12 },
  "avg_words_per_entry": 187.5,
  "avg_chunks_per_entry": 2.3,
  "last_ingestion_at": "2026-04-11T08:12:33Z",
  "total_chunks": 98,
  "row_counts": {
   "entries": 42,
   "entry_pages": 53,
   "entry_chunks": 98,
   "mood_scores": 0,
   "source_files": 45,
   "entities": 0,
   "entity_aliases": 0,
   "entity_mentions": 0,
   "entity_relationships": 0
  }
 },
 "queries": {
  "total_queries": 17,
  "uptime_seconds": 3821.42,
  "started_at": "2026-04-11T07:30:00+00:00",
  "by_type": {
   "hybrid_search": { "count": 17, "latency": { "p50_ms": 184.2, "p95_ms": 412.7, "p99_ms": 588.3 } }
  }
 }
}
```

**Status semantics:**

- `"status": "ok"` — every component check returned `ok`.
- `"status": "degraded"` — at least one component is degraded (missing API key, short API key). The server is still
  serving requests. The endpoint still returns HTTP 200 so a probe can distinguish "config is wrong" from "container is
  not listening".
- `"status": "error"` — at least one component check failed outright (e.g. SQLite unreachable, ChromaDB connection
  refused). Still HTTP 200; callers should inspect the `status` field rather than relying on status codes.

**Component checks:**

| Component   | What it checks                                                      |
| ----------- | ------------------------------------------------------------------- |
| `sqlite`    | `SELECT 1` against the connection                                   |
| `chromadb`  | `collection.count()` against the vector store                       |
| `anthropic` | API key is set and has a plausible length — **no LLM call is made** |
| `openai`    | API key is set and has a plausible length — **no API call is made** |

**Privacy:** The payload intentionally omits any field that would surface query _content_ — only counts and latency
percentiles per query type. Adding a "most frequent search terms" field was proposed and rejected per the Tier 1 plan
open question on privacy.

**CLI equivalent:** `uv run journal health` prints the same payload as pretty JSON. Add `--compact` for single-line
output suitable for piping to `jq`. The CLI exits non-zero only when the rolled-up `status` is `error`.

---

### GET /api/search

Hybrid full-text + semantic search across journal entries. There is **no** mode toggle — every request runs the full
pipeline. The `mode` query parameter has been retired and passing it now returns `400 mode_removed`. See
[`search.md`](search.md) for the architecture deep-dive.

**Pipeline:**

1. **L1a — BM25 retrieval.** SQLite FTS5 (`entries_fts`) over `raw_text`, entry-level, top-N candidates.
2. **L1b — Dense retrieval.** Query is embedded via the configured embeddings provider, ChromaDB returns the top-N
   chunks by cosine distance, projected to entries by keeping the best-scoring chunk per entry.
3. **Fusion.** Reciprocal Rank Fusion across the two ranked lists with `k = 60` (Cormack et al.). Truncated to the
   top-M fused entries.
4. **L2 — Listwise rerank.** The configured `Reranker` (default: Anthropic Claude Haiku 4.5; can be swapped to
   `none` to skip L2) scores the fused candidates given the query and returns a final order.
5. **Sort + slice.** Results are reordered per the `sort` parameter and paged by `offset` + `limit`. Sort + paging
   are applied on top of a 5-minute LRU cache of the reranked candidate list — paging or changing sort does not
   re-run the pipeline.

**Query parameters:**

| Parameter    | Type   | Required | Default     | Description                                                  |
| ------------ | ------ | -------- | ----------- | ------------------------------------------------------------ |
| `q`          | string | yes      |             | Search query (FTS5 syntax permitted; bare terms recommended) |
| `start_date` | string | no       |             | Filter from date (ISO 8601)                                  |
| `end_date`   | string | no       |             | Filter until date (ISO 8601)                                 |
| `limit`      | int    | no       | 10          | Max entries returned (clamped to 1–50)                       |
| `offset`     | int    | no       | 0           | Pagination offset                                            |
| `sort`       | string | no       | `relevance` | `relevance`, `date_desc`, or `date_asc`                      |

**Response (200):**

```json
{
 "query": "vienna with atlas",
 "limit": 10,
 "offset": 0,
 "sort": "relevance",
 "reranker": "ClaudeListwiseReranker",
 "items": [
  {
   "entry_id": 42,
   "entry_date": "2026-03-22",
   "text": "Walked through Vienna with Atlas. Later we met Robyn...",
   "score": 0.92,
   "snippet": "...walked through Vienna with Atlas...",
   "matching_chunks": [
    {
     "text": "Walked through Vienna with Atlas",
     "score": 0.871,
     "chunk_index": 0,
     "char_start": 0,
     "char_end": 32
    }
   ]
  }
 ]
}
```

**Item shape:**

- `entry_id`, `entry_date`, `text` — the matched entry's id, date, and full text (`final_text` if present, else
  `raw_text`).
- `score` — the reranker's score for the entry (or the RRF score when the reranker is set to `none`). Comparable
  within a single response, not across responses.
- `snippet` — populated **only when BM25 contributed to the match**. An FTS5 excerpt with ASCII `\x02` / `\x03`
  control characters wrapping the matched terms; the client replaces them with whatever highlight markup it wants
  (for example `<mark>`). `null` when only dense retrieval matched.
- `matching_chunks` — populated **only when dense retrieval contributed**. One entry per chunk that matched, sorted
  by similarity descending. Each chunk has `text`, `score` (cosine similarity), `chunk_index`, `char_start`, and
  `char_end`. `char_start`/`char_end` are `null` for entries ingested before chunk persistence (migration 0003).
  Empty list when only BM25 matched.

Either `snippet` or `matching_chunks` (or both) will carry data for any successful hit. The `reranker` field at the
top level names the reranker class actually used (e.g. `ClaudeListwiseReranker`, `NoOpReranker`) so clients can
debug or cache-bust based on the L2 stage.

**Error responses:**

- `400 missing_query` — `q` is missing or empty.
- `400 mode_removed` — the request included a `mode` query parameter (retired when hybrid shipped).
- `400 invalid_sort` — `sort` is not one of `relevance`, `date_desc`, `date_asc`.
- `400 invalid_query` — FTS5 could not parse `q` (unterminated quote, bare boolean operator, etc.).
- `503` — `{"error": "Server not initialized"}` while the server is still booting.

---

### GET /api/dashboard/mood-dimensions

Return the currently-loaded mood-scoring dimensions. Used by the webapp's mood chart to discover the active facet set,
their scale types, and score ranges without hardcoding anything in the frontend.

**No query parameters.**

**Response (200):**

```json
{
 "dimensions": [
  {
   "name": "joy_sadness",
   "positive_pole": "joy",
   "negative_pole": "sadness",
   "scale_type": "bipolar",
   "score_min": -1.0,
   "score_max": 1.0,
   "notes": "..."
  },
  {
   "name": "agency",
   "positive_pole": "agency",
   "negative_pole": "apathy",
   "scale_type": "unipolar",
   "score_min": 0.0,
   "score_max": 1.0,
   "notes": "..."
  }
 ],
 "meta": {
  "version": "2026-04-14",
  "description": "Six bipolar facets + two unipolar drives."
 }
}
```

The `meta` block carries `version` and `description` from the loaded mood-dimensions TOML; both fields are always
present (empty strings when the file is absent or the feature is disabled).

When mood scoring is disabled (`JOURNAL_ENABLE_MOOD_SCORING` unset or false) the endpoint returns 200 with an empty
`dimensions` array (and an empty `meta` block). Callers should treat that as "no mood data to display" rather than an
error.

See `docs/mood-scoring.md` for the full rationale, facet definitions, bipolar vs unipolar semantics, and the rebuild
procedure.

---

### GET /api/dashboard/mood-trends

Aggregate mood scores per time bucket, grouped by dimension. Used by the dashboard's mood chart.

**Query parameters:**

| Parameter   | Type   | Required | Default | Description                            |
| ----------- | ------ | -------- | ------- | -------------------------------------- |
| `bin`       | string | no       | `week`  | `week`, `month`, `quarter`, or `year`  |
| `from`      | string | no       |         | Inclusive ISO-8601 start date          |
| `to`        | string | no       |         | Inclusive ISO-8601 end date            |
| `dimension` | string | no       |         | Filter to a single dimension by `name` |

**Response (200):**

```json
{
 "from": "2026-01-01",
 "to": "2026-04-11",
 "bin": "week",
 "bins": [
  { "period": "2026-01-05", "dimension": "joy_sadness", "avg_score": 0.3, "entry_count": 4, "score_min": -0.1, "score_max": 0.7 },
  { "period": "2026-01-05", "dimension": "agency", "avg_score": 0.6, "entry_count": 4, "score_min": 0.3, "score_max": 0.9 },
  { "period": "2026-01-12", "dimension": "joy_sadness", "avg_score": 0.5, "entry_count": 5, "score_min": 0.2, "score_max": 0.8 }
 ]
}
```

- `period` is the canonical ISO-8601 date of the bucket start (Monday for weeks, first of month/quarter/year for the
  others).
- `avg_score` is the mean across every scored entry in the bucket.
- `score_min` / `score_max` are the minimum and maximum individual entry scores in the bucket (used for variance bands on the chart).
- Empty buckets are omitted.

**Error responses:**

- `400` — `bin` is not one of `week`/`month`/`quarter`/`year`
- `503` — server not initialised

---

### GET /api/dashboard/mood-drilldown

Return per-entry mood scores for a single dimension within a date window. Used by both the Dashboard and Insights pages when the user clicks a data point on the mood chart to see which entries contributed to that period's average score.

**Query parameters:**

| Parameter   | Type   | Required | Description                   |
| ----------- | ------ | -------- | ----------------------------- |
| `dimension` | string | yes      | The mood dimension name       |
| `from`      | string | yes      | Inclusive ISO-8601 start date |
| `to`        | string | yes      | Inclusive ISO-8601 end date   |

**Response (200):**

```json
{
 "dimension": "agency",
 "from": "2026-04-14",
 "to": "2026-04-20",
 "entries": [
  {
   "entry_id": 42,
   "entry_date": "2026-04-15",
   "score": 0.72,
   "confidence": 0.88,
   "rationale": "The writer describes taking initiative on the project and feeling capable of driving the outcome."
  }
 ]
}
```

- `rationale` is `null` for entries scored before migration 0014. Run `journal backfill-mood --force` to populate rationales.
- Entries are ordered by `entry_date` ascending.

**Error responses:**

- `400` — missing `dimension`, `from`, or `to` parameter
- `503` — server not initialised

---

### GET /api/dashboard/entity-distribution

Return entity mention counts grouped by entity name, filtered by type and date range. Used by the Insights page for the "What I Write About" doughnut chart.

**Query parameters:**

| Parameter | Type   | Required | Default | Description                                            |
| --------- | ------ | -------- | ------- | ------------------------------------------------------ |
| `type`    | string | no       |         | Entity type filter: person, place, activity, etc.      |
| `from`    | string | no       |         | Inclusive ISO-8601 start date                          |
| `to`      | string | no       |         | Inclusive ISO-8601 end date                            |
| `limit`   | int    | no       | 50      | Max items to return (capped at 200)                    |

**Response (200):**

```json
{
 "type": "topic",
 "from": "2026-01-01",
 "to": "2026-04-20",
 "total": 2,
 "items": [
  { "canonical_name": "meditation", "entity_type": "topic", "mention_count": 14 },
  { "canonical_name": "running", "entity_type": "topic", "mention_count": 9 }
 ]
}
```

- Items are ordered by `mention_count` descending.
- `mention_count` counts total mentions (not distinct entries).

**Error responses:**

- `400` — invalid `type` value
- `503` — server not initialised

---

### GET /api/dashboard/writing-stats

Aggregated writing frequency and word count per time bucket, used by the webapp's `/dashboard` view. Bearer-authenticated
via the app-wide middleware.

**Query parameters:**

| Parameter | Type   | Required | Default | Description                           |
| --------- | ------ | -------- | ------- | ------------------------------------- |
| `bin`     | string | no       | `week`  | `week`, `month`, `quarter`, or `year` |
| `from`    | string | no       |         | Inclusive ISO-8601 start date         |
| `to`      | string | no       |         | Inclusive ISO-8601 end date           |

**Response (200):**

```json
{
 "from": "2026-01-01",
 "to": "2026-04-11",
 "bin": "month",
 "bins": [
  { "bin_start": "2026-01-01", "entry_count": 5, "total_words": 980 },
  { "bin_start": "2026-02-01", "entry_count": 3, "total_words": 612 },
  { "bin_start": "2026-03-01", "entry_count": 12, "total_words": 2240 },
  { "bin_start": "2026-04-01", "entry_count": 2, "total_words": 380 }
 ]
}
```

**Bucket semantics:**

- `week` bins start on the Monday of the ISO week (Sunday rolls into the preceding Monday).
- `month` bins start on the 1st of the month.
- `quarter` bins start on the 1st of Jan/Apr/Jul/Oct.
- `year` bins start on January 1st.
- **Empty buckets are omitted.** A month with zero entries does not appear in the `bins` array. Clients rendering a dense
  line chart should fill gaps on the client side.

**Error responses:**

- `400` — `bin` is not one of `week`/`month`/`quarter`/`year`
- `503` — server not initialised

---

## Entry creation endpoints

Three endpoints for creating journal entries from the webapp. Text and file ingestion are synchronous; image ingestion is
asynchronous (returns a job ID).

### POST /api/entries/ingest/text

Create a journal entry from plain text (no OCR). Synchronous.

**Request body (JSON):**

| Field         | Type   | Required | Default    | Description       |
| ------------- | ------ | -------- | ---------- | ----------------- |
| `text`        | string | yes      |            | Entry content     |
| `entry_date`  | string | no       | today      | ISO 8601 date     |
| `source_type` | string | no       | `"text_entry"` | Source type label (e.g. `text_entry`, `imported_text_file`) |

**Response (201):**

```json
{
 "entry": { "id": 1, "entry_date": "2026-04-12", "source_type": "text_entry", "...": "..." },
 "mood_job_id": "uuid-or-null",
 "entity_extraction_job_id": "uuid-or-null"
}
```

`mood_job_id` is non-null when `JOURNAL_ENABLE_MOOD_SCORING=true`. `entity_extraction_job_id` is always non-null (entity
extraction runs on every new entry). Poll `GET /api/jobs/{id}` to check completion.

**Errors:** 400 (missing/empty text, invalid JSON).

---

### POST /api/entries/ingest/file

Create a journal entry from an uploaded `.md` or `.txt` file. Synchronous. The new entry is stored with
`source_type = "imported_text_file"`.

**Request body (multipart/form-data):**

| Field        | Type   | Required | Default | Description                   |
| ------------ | ------ | -------- | ------- | ----------------------------- |
| `file`       | file   | yes      |         | A single `.md` or `.txt` file |
| `entry_date` | string | no       |         | ISO 8601 date (fallback only) |

**Date precedence** (`api/ingestion.py:230-235`): the entry's date is chosen by walking this list and taking the first
hit:

1. A date parsed from the file **content** (e.g. a "TUES 17 FEB 2026" or "2026-02-17" header in the document).
2. A date parsed from the **filename** (e.g. `260217-trip.md`).
3. The `entry_date` form field, if supplied.
4. Today's date (UTC).

**Response (201):** Same shape as `POST /api/entries/ingest/text`.

The file content is stored as both `raw_text` and `final_text`. A `source_files` record is created with the original
filename and SHA256 hash for duplicate detection.

**Errors:** 400 (wrong extension, empty file, UTF-8 decode error, no file).

---

### POST /api/entries/ingest/images

Upload one or more journal page images for OCR. Asynchronous — returns a job ID immediately. The job runs OCR on each
page, combines the text into a single entry, chunks, embeds, and stores.

**Date extraction:** After OCR, the server searches the first 500 characters of extracted text for a date (e.g., "TUES 17
FEB 2026", "17/02/2026", "2026-02-17"). If found, it overrides the `entry_date` parameter. This means journal pages with
handwritten dates are automatically dated correctly.

**Request body (multipart/form-data):**

| Field        | Type    | Required | Default | Description                                     |
| ------------ | ------- | -------- | ------- | ----------------------------------------------- |
| `images`     | file(s) | yes      |         | One or more image files (JPEG, PNG, GIF, WebP, HEIC) |
| `entry_date` | string  | no       | today   | ISO 8601 date (overridden by OCR date if found) |

**Limits:** 10 MB per file, 50 MB total.

**Response (202):**

```json
{
 "job_id": "uuid",
 "status": "queued"
}
```

Poll `GET /api/jobs/{job_id}` for progress. On success, `result.entry_id` contains the new entry's ID.

HEIC/HEIF images (common on macOS and iOS) are automatically converted to JPEG on upload before OCR processing.

**Errors:** 400 (no images, unsupported type), 413 (total size exceeded).

---

### POST /api/entries/ingest/audio

Upload one or more audio recordings for transcription. Asynchronous — returns a job ID immediately. The job transcribes
each recording via the configured transcription stack (default OpenAI `gpt-4o-transcribe` with a `whisper-1`
retry/fallback; an optional Gemini provider can run as primary or shadow — see
[`transcription-providers.md`](transcription-providers.md)), concatenates the texts into a single entry, chunks,
embeds, and stores.

Multiple recordings are joined with a single newline separator and stored as one voice entry. This supports the workflow
of recording a journal entry in multiple segments (e.g., start, pause, continue).

**Request body (multipart/form-data):**

| Field         | Type    | Required | Default   | Description                                                     |
| ------------- | ------- | -------- | --------- | --------------------------------------------------------------- |
| `audio`       | file(s) | yes      |           | One or more audio files (MP3, MP4, WAV, WebM, OGG, FLAC, M4A)  |
| `entry_date`  | string  | no       | today     | ISO 8601 date                                                   |
| `source_type` | string  | no       | `"voice"` | `"voice"` (live recording) or `"imported_audio_file"`           |

**Limits:** 100 MB per file, 500 MB total.

**Response (202):**

```json
{
 "job_id": "uuid",
 "status": "queued"
}
```

Poll `GET /api/jobs/{job_id}` for progress. On success, `result.entry_id` contains the new entry's ID.

**Errors:** 400 (no audio, unsupported type), 413 (total size exceeded).

---

## Entity endpoints

Endpoints that expose the extracted-entity graph built by the entity extraction pipeline. See
[entity-tracking.md](entity-tracking.md) for how entities, mentions, and relationships are produced, and
[jobs.md](jobs.md) for how the extraction runs are scheduled.

### GET /api/entities

List entities with optional type filter, case-insensitive substring search, and pagination. The search filter is applied
after the store query, so `total` reflects the unfiltered count for the given `entity_type`.

**Query parameters:**

| Parameter | Type   | Required | Default | Description                                                         |
| --------- | ------ | -------- | ------- | ------------------------------------------------------------------- |
| `type`    | string | no       |         | Filter by entity type (e.g. `person`, `place`, `activity`)          |
| `search`  | string | no       |         | Case-insensitive substring match against canonical name and aliases |
| `limit`   | int    | no       | 50      | Max results (capped at 200)                                         |
| `offset`  | int    | no       | 0       | Pagination offset                                                   |

**Response (200):**

```json
{
 "items": [
  {
   "id": 7,
   "canonical_name": "Atlas",
   "entity_type": "person",
   "aliases": ["Atlas G."],
   "mention_count": 23,
   "first_seen": "2026-01-04"
  }
 ],
 "total": 42,
 "limit": 50,
 "offset": 0
}
```

### GET /api/entities/{entity_id}

Return the full detail record for a single entity.

**Response (200):**

```json
{
 "id": 7,
 "canonical_name": "Atlas",
 "entity_type": "person",
 "aliases": ["Atlas G."],
 "description": "Long-time friend, lives in Vienna.",
 "first_seen": "2026-01-04",
 "created_at": "2026-01-04T10:22:13+00:00",
 "updated_at": "2026-03-18T14:01:55+00:00"
}
```

**Response (404):**

```json
{ "error": "Entity 999 not found" }
```

### GET /api/entities/{entity_id}/mentions

List every recorded mention of an entity, oldest first. Each mention carries the entry it was extracted from, the exact
quoted span, and the extractor's confidence.

**Query parameters:**

| Parameter | Type | Required | Default | Description                  |
| --------- | ---- | -------- | ------- | ---------------------------- |
| `limit`   | int  | no       | 50      | Max mentions (capped at 200) |
| `offset`  | int  | no       | 0       | Pagination offset            |

**Response (200):**

```json
{
 "entity_id": 7,
 "mentions": [
  {
   "id": 103,
   "entity_id": 7,
   "entry_id": 42,
   "entry_date": "2026-03-22",
   "quote": "Walked through Vienna with Atlas",
   "confidence": 0.93,
   "extraction_run_id": "b1e2...",
   "created_at": "2026-03-22T18:04:11+00:00"
  }
 ],
 "total": 1
}
```

`total` is the length of the returned page, not the unpaginated count — clients that need a hard total should rely on the
entity summary's `mention_count` instead.

**Response (404):**

```json
{ "error": "Entity 999 not found" }
```

### GET /api/entities/{entity_id}/relationships

Return the subject-predicate-object relationships that touch an entity, split into outgoing (entity is the subject) and
incoming (entity is the object) lists.

**No query parameters.**

**Response (200):**

```json
{
 "entity_id": 7,
 "outgoing": [
  {
   "id": 12,
   "subject_entity_id": 7,
   "predicate": "lives_in",
   "object_entity_id": 19,
   "quote": "Atlas lives in Vienna",
   "entry_id": 42,
   "confidence": 0.88,
   "extraction_run_id": "b1e2...",
   "created_at": "2026-03-22T18:04:11+00:00"
  }
 ],
 "incoming": []
}
```

**Response (404):**

```json
{ "error": "Entity 999 not found" }
```

### GET /api/entries/{entry_id}/entities

List the entities extracted from a single entry, with per-entity mention counts and verbatim quotes scoped to that entry.
The `quotes` array contains the deduplicated text spans that Claude extracted from the entry — these may differ from the
`canonical_name` (e.g. the entry says "quiet reflection" but the canonical entity is "prayer").

**No query parameters.**

**Response (200):**

```json
{
 "entry_id": 42,
 "items": [
  {
   "id": 7,
   "canonical_name": "Atlas",
   "entity_type": "person",
   "aliases": ["Atlas G."],
   "mention_count": 2,
   "quotes": ["Atlas", "Atlas G."],
   "first_seen": "2026-01-04"
  }
 ],
 "total": 1
}
```

**Response (404):**

```json
{ "error": "Entry 999 not found" }
```

### PATCH /api/entities/{entity_id}

Update an entity's canonical name, type, or description.

**Request body:**

```json
{
 "canonical_name": "Lizzie Extance",
 "entity_type": "person",
 "description": "John's sister"
}
```

All fields are optional — include only the ones you want to change.

**Response (200):** Full entity detail object (same shape as GET /api/entities/{id}).

When the patch changes `description`, the response also includes `reembed_job_id` — the id of an
async background job that recomputes the entity's stored embedding so future entity recognition
reflects the new text. Poll `GET /api/jobs/{id}` to follow it. The field is omitted when the
description was unchanged or no job runner is wired up.

**Response (400):** `canonical_name` is empty, or `entity_type` is invalid.

**Response (404):** Entity not found.

### POST /api/entities/{entity_id}/aliases

Add an alias to an entity.

**Request body:**

```json
{ "alias": "Mum" }
```

**Response (201):** Full entity detail object (same shape as GET /api/entities/{entity_id}) with
the new alias included. Idempotent: re-asserting an existing alias on the same entity just returns
the entity unchanged.

**Response (400):** `alias` is missing or empty.

**Response (404):** Entity not found.

**Response (409):** The alias is already mapped to a *different* entity for this user. The body
includes the existing entity's id, name, and type so the caller can offer a merge:

```json
{
 "error": "alias already maps to a different entity",
 "alias": "Mum",
 "existing_entity_id": 7,
 "existing_canonical_name": "Sarah",
 "existing_entity_type": "person"
}
```

The webapp surfaces this as a "merge into the existing entity?" dialog and, on confirm, calls
`POST /api/entities/merge` with the existing entity as survivor.

### DELETE /api/entities/{entity_id}/aliases/{alias}

Remove an alias from an entity. The alias is normalised (lowercased + whitespace-stripped) before
matching, so case doesn't matter.

**Response (200):** Updated entity detail object.

**Response (404):** Entity not found, or the alias is not attached to this entity.

### GET /api/entities/aliases/lookup

Non-mutating, type-agnostic collision check. Used by the webapp before submitting a new alias to
warn the user inline.

**Query parameters:**

| Parameter | Type   | Required | Description                  |
| --------- | ------ | -------- | ---------------------------- |
| `alias`   | string | yes      | The alias text to look up.   |

**Response (200):** When the alias is unowned for this user:

```json
{ "entity_id": null }
```

When some entity owns it:

```json
{ "entity_id": 7, "canonical_name": "Sarah", "entity_type": "person" }
```

**Response (400):** `alias` query parameter missing.

### DELETE /api/entities/{entity_id}

Delete an entity and all its mentions, relationships, and aliases (via FK CASCADE).

**Response (200):**

```json
{ "deleted": true, "id": 42 }
```

**Response (404):** Entity not found.

### POST /api/entities/merge

Merge one or more entities into a survivor. All mentions, relationships, and aliases from the absorbed entities are
reassigned to the survivor. Absorbed entities are deleted. A snapshot of each absorbed entity is saved to
`entity_merge_history` for audit/undo.

**Request body:**

```json
{
 "survivor_id": 5,
 "absorbed_ids": [12, 17]
}
```

**Response (200):**

```json
{
 "survivor": { "id": 5, "canonical_name": "Lizzie Extance", "...": "..." },
 "absorbed_ids": [12, 17],
 "mentions_reassigned": 4,
 "relationships_reassigned": 2,
 "aliases_added": 3
}
```

**Response (400):** Missing fields, entity not found, or trying to merge into self.

### GET /api/entities/merge-candidates

List pending merge candidates (near-miss similarity matches from extraction).

**Query parameters:**

| Parameter | Type   | Default   | Description                                          |
| --------- | ------ | --------- | ---------------------------------------------------- |
| `status`  | string | `pending` | Filter by status: `pending`, `accepted`, `dismissed` |
| `limit`   | int    | 50        | Max results (up to 200)                              |

**Response (200):**

```json
{
 "items": [
  {
   "id": 1,
   "entity_a": { "id": 5, "canonical_name": "Liz", "...": "..." },
   "entity_b": { "id": 12, "canonical_name": "Lizzie", "...": "..." },
   "similarity": 0.82,
   "status": "pending",
   "extraction_run_id": "abc-123",
   "created_at": "2026-04-12T10:00:00Z"
  }
 ],
 "total": 1
}
```

### PATCH /api/entities/merge-candidates/{candidate_id}

Resolve a merge candidate by accepting or dismissing it.

**Request body:**

```json
{ "status": "dismissed" }
```

`status` must be `"accepted"` or `"dismissed"`.

**Response (200):**

```json
{ "id": 1, "status": "dismissed" }
```

### GET /api/entities/{entity_id}/merge-history

Get the merge history for an entity (all entities that were merged into it).

**Response (200):**

```json
{
 "entity_id": 5,
 "history": [
  {
   "id": 1,
   "survivor_id": 5,
   "absorbed_id": 12,
   "absorbed_name": "Vienna's aunt",
   "absorbed_type": "person",
   "absorbed_desc": "",
   "absorbed_aliases": ["aunt", "lizzie"],
   "merged_at": "2026-04-12T10:30:00Z",
   "merged_by": "user"
  }
 ]
}
```

### GET /api/entities/quarantined

List quarantined entities for the authenticated user. Quarantined entities are hidden from
`GET /api/entities` and from chart endpoints (`entity-distribution`, `entity-trends`, `mood-entity-correlation`)
by default; this endpoint is the only path that surfaces them. See
[entity-tracking.md](entity-tracking.md#quarantine) for the semantics.

**Response (200):**

```json
{
 "items": [
  {
   "id": 17,
   "canonical_name": "Hallucinated Name",
   "entity_type": "person",
   "aliases": [],
   "description": "",
   "first_seen": "2026-04-01",
   "created_at": "2026-04-01T09:00:00Z",
   "updated_at": "2026-04-12T10:30:00Z",
   "is_quarantined": true,
   "quarantine_reason": "canonical name not present in any quote",
   "quarantined_at": "2026-04-12T10:30:00Z"
  }
 ],
 "total": 1
}
```

### POST /api/entities/{entity_id}/quarantine

Soft-quarantine an entity. The row stays in the database — descriptions, aliases, and merge history are preserved —
but it is excluded from default lists and charts. Idempotent: calling it again refreshes the reason and timestamp.

**Request body:**

```json
{ "reason": "canonical name absent from all quotes" }
```

`reason` is optional and defaults to an empty string. Must be a string when provided.

**Response (200):** Full entity detail with `is_quarantined: true`, `quarantine_reason`, and `quarantined_at` populated.

**Response (400):** `reason` is not a string.
**Response (404):** Entity not found or not owned by the authenticated user.

### POST /api/entities/{entity_id}/release-quarantine

Clear the quarantine flag, reason, and timestamp. The entity reappears in default lists and charts on the next read.
Idempotent on already-active entities.

**Request body:** none (or `{}`).

**Response (200):** Full entity detail with `is_quarantined: false`, `quarantine_reason: ""`, `quarantined_at: ""`.

**Response (404):** Entity not found or not owned by the authenticated user.

---

## Batch job endpoints

Long-running batch operations (entity extraction, mood backfill) run asynchronously on an in-process single-worker job
runner. Clients submit a job, receive `202 Accepted` with a `job_id`, and then poll `GET /api/jobs/{job_id}` once per
second until `status` reaches a terminal value (`succeeded` or `failed`).

See [jobs.md](jobs.md) for the full data model, threading invariants, restart recovery semantics, and result payload
shapes.

### POST /api/entities/extract

Submit an entity-extraction batch job. This endpoint replaced the previously synchronous entity extraction call — the
single-entry path (`entry_id`) also goes through the jobs table, so there is no synchronous alternative. Unknown keys,
wrong types, and invalid values are rejected synchronously by `JobRunner.submit_entity_extraction` before any row is
written.

**Request body:**

```json
{
 "entry_id": 42,
 "start_date": "2026-03-01",
 "end_date": "2026-03-31",
 "stale_only": true
}
```

All four fields are optional. When `entry_id` is present the runner calls `extract_from_entry` and returns a one-result
batch; otherwise it runs `extract_batch` with the date and staleness filters.

**Response (202):**

```json
{ "job_id": "a3f9...", "status": "queued" }
```

**Response (400):**

```json
{ "error": "Unknown parameter: foo" }
```

Returned when the body is not a JSON object, when it contains unknown keys, or when a field has the wrong type.

**Response (503):**

```json
{ "error": "Server not initialized" }
```

Poll `GET /api/jobs/{job_id}` for progress and the final result. See [jobs.md](jobs.md) for the full result-payload
shape.

### POST /api/mood/backfill

Submit a mood-score backfill batch job. `mode` is required and selects between idempotent rescoring of stale entries and
a full rescore. `prune_retired` and `dry_run` are intentionally not surfaced here — use the CLI for those.

**Request body:**

```json
{
 "mode": "stale-only",
 "start_date": "2026-03-01",
 "end_date": "2026-03-31"
}
```

| Field        | Type   | Required | Description                   |
| ------------ | ------ | -------- | ----------------------------- |
| `mode`       | string | yes      | `"stale-only"` or `"force"`   |
| `start_date` | string | no       | Inclusive ISO-8601 start date |
| `end_date`   | string | no       | Inclusive ISO-8601 end date   |

`stale-only` rescores only entries missing at least one currently-loaded mood dimension; `force` rescores every entry in
the date window.

**Response (202):**

```json
{ "job_id": "8e12...", "status": "queued" }
```

**Response (400):**

```json
{ "error": "mode must be 'stale-only' or 'force'" }
```

Returned when the body is not a JSON object, `mode` is missing or invalid, or an unknown key is present.

**Response (503):**

```json
{ "error": "Server not initialized" }
```

Poll `GET /api/jobs/{job_id}` for progress and the final result. See [jobs.md](jobs.md) for the full result-payload
shape.

### GET /api/jobs

List jobs ordered newest first, with optional filters and pagination. Non-admin users see only their own jobs; admins
see all jobs across users (`api/jobs.py:48`).

**Query parameters:**

| Param    | Type   | Default | Description                                                     |
| -------- | ------ | ------- | --------------------------------------------------------------- |
| `status` | string | (all)   | Filter by status (`queued`, `running`, `succeeded`, `failed`)   |
| `type`   | string | (all)   | Filter by job type (`entity_extraction`, `mood_backfill`, etc.) |
| `limit`  | int    | 50      | Max items to return                                             |
| `offset` | int    | 0       | Pagination offset                                               |

**Response (200):**

```json
{
 "items": [
  /* array of job objects (same shape as GET /api/jobs/{id}) */
 ],
 "total": 42,
 "limit": 50,
 "offset": 0
}
```

### GET /api/jobs/{job_id}

Return the full serialised state of a batch job. Clients should poll this endpoint once per second until `status` is
`succeeded` or `failed`. Every field is always present in the response — absent values are `null` rather than missing
keys — so clients can rely on a fixed schema.

**No query parameters.**

**Response (200):**

```json
{
 "id": "a3f9...",
 "type": "entity_extraction",
 "status": "running",
 "params": { "stale_only": true },
 "progress_current": 12,
 "progress_total": 48,
 "result": null,
 "error_message": null,
 "created_at": "2026-04-12T09:14:33+00:00",
 "started_at": "2026-04-12T09:14:33+00:00",
 "finished_at": null
}
```

- `type` is `entity_extraction` or `mood_backfill`.
- `status` transitions `queued` → `running` → `succeeded` | `failed`.
- `result` is populated on success (shape depends on `type` — see [jobs.md](jobs.md)).
- `error_message` is populated when `status = failed`, including the sentinel `"server restarted before job completed"`
  for jobs reconciled on startup after an unclean shutdown.

**Response (404):**

```json
{ "error": "Job not found" }
```

---

### GET /api/stats

Journal statistics with optional date filtering.

**Query parameters:**

| Parameter    | Type   | Required | Description                  |
| ------------ | ------ | -------- | ---------------------------- |
| `start_date` | string | no       | Filter from date (ISO 8601)  |
| `end_date`   | string | no       | Filter until date (ISO 8601) |

**Response (200):**

```json
{
 "total_entries": 42,
 "date_range_start": "2025-01-15",
 "date_range_end": "2026-04-09",
 "total_words": 18500,
 "avg_words_per_entry": 440.5,
 "entries_per_month": 3.2
}
```

`entries_per_month` is a single float — the average entries-per-month rate over the date range
(`total_entries / months_in_range`). For per-bucket counts, use `/api/dashboard/writing-stats` with
`bin=month` instead.

---

### GET /api/notifications/topics

Return notification topics with the authenticated user's current toggle state. Admin-only topics are hidden for
non-admin users.

**Response:**

```json
{
  "topics": [
    {
      "key": "notif_job_success_ingest_images",
      "label": "Image ingestion succeeded",
      "group": "success",
      "admin_only": false,
      "default": true,
      "enabled": true
    }
  ]
}
```

---

### GET /api/notifications/status

Return whether the authenticated user has Pushover credentials configured (either via user preferences or server
defaults).

**Response:**

```json
{ "configured": true }
```

---

### POST /api/notifications/validate

Validate Pushover credentials against the Pushover API. If valid, the credentials are saved to the user's preferences.

**Request body:**

```json
{ "user_key": "...", "app_token": "..." }
```

**Response:**

```json
{ "valid": true, "error": null }
```

---

### POST /api/notifications/test

Send a test Pushover notification using the authenticated user's saved credentials.

**Response:**

```json
{ "sent": true, "error": null }
```

---

## Auth endpoints

Session and account-lifecycle routes. See [`auth.md`](auth.md) for the data model, password rules, token lifetimes,
and email-dispatch behaviour.

### POST /api/auth/login

**Public.** Authenticate with email + password. On success returns the user JSON and sets the `session_id` cookie
(httpOnly, Secure, SameSite=Lax, 7-day max-age). The cookie is the only place the raw session token appears; the
server stores a hashed copy.

**Request body:**

```json
{ "email": "alice@example.com", "password": "..." }
```

**Response (200):**

```json
{
 "user": {
  "id": 1,
  "email": "alice@example.com",
  "display_name": "Alice",
  "is_admin": false,
  "is_active": true,
  "email_verified": true,
  "created_at": "2026-04-01T10:00:00+00:00",
  "updated_at": "2026-04-01T10:00:00+00:00"
 }
}
```

**Errors:** `400 invalid_body`, `400 missing_fields`, `401 invalid_credentials`, `503 server_not_ready`.

---

### POST /api/auth/logout

Revoke the current session and clear the `session_id` cookie. Idempotent — returning 200 with `{"ok": true}` even if
no session was attached.

**Response (200):**

```json
{ "ok": true }
```

---

### GET /api/auth/me

Return the currently authenticated user.

**Response (200):**

```json
{ "user": { "id": 1, "email": "alice@example.com", "display_name": "Alice", "is_admin": false, "is_active": true, "email_verified": true, "created_at": "...", "updated_at": "..." } }
```

`/api/auth/me` is in `VERIFICATION_EXEMPT_PATHS`, so it is reachable while `email_verified` is still `false` (unlike
most other authenticated routes).

---

### PATCH /api/auth/me

Update the current user's profile. Currently the only mutable field is `display_name`.

**Request body:**

```json
{ "display_name": "Alice Smith" }
```

**Response (200):** `{"user": { ... }}` with the updated record.

**Errors:** `400 invalid_body`, `400 missing_fields` (empty `display_name`), `404 not_found`.

---

### GET /api/auth/config

**Public.** Expose non-sensitive auth configuration that the webapp needs **before** the user is logged in (e.g. to
decide whether to show the Register link).

**Response (200):**

```json
{ "registration_enabled": true }
```

---

### POST /api/auth/register

**Public.** Create a new user, issue a session cookie, and (best-effort) send a verification email. Honours the
runtime `registration_enabled` flag — when disabled, returns `403 registration_disabled`.

**Request body:**

```json
{ "email": "alice@example.com", "password": "long-enough", "display_name": "Alice" }
```

`password` must be 8–1024 characters.

**Response (201):** Same `{"user": ...}` shape as `/login`, plus a `session_id` cookie. The user starts with
`email_verified: false`; verification email dispatch is best-effort and never fails the request.

**Errors:** `400 invalid_body`, `400 missing_fields`, `400 weak_password`, `400 duplicate_email`,
`403 registration_disabled`.

---

### POST /api/auth/forgot-password

**Public.** Request a password-reset email. **Always returns 200** to avoid email enumeration; the actual email is
sent only if the address matches a real user and the email service is configured.

**Request body:**

```json
{ "email": "alice@example.com" }
```

**Response (200):**

```json
{ "message": "If that email exists, a reset link has been sent" }
```

---

### GET /api/auth/verify-reset-token

**Public.** Check whether a password-reset token is still valid. Used by the webapp to gate the password-reset form.

**Query parameters:** `token` (required).

**Response (200):**

```json
{ "valid": true }
```

**Errors:** `400 missing_token`, `400 invalid_token`.

---

### POST /api/auth/reset-password

**Public.** Reset the password for the user identified by a valid reset token.

**Request body:**

```json
{ "token": "...", "password": "new-password" }
```

`password` must be 8–1024 characters.

**Response (200):**

```json
{ "message": "Password has been reset successfully" }
```

**Errors:** `400 invalid_body`, `400 missing_fields`, `400 weak_password`, `400 invalid_token`.

---

### GET /api/auth/verify-email

**Public.** Confirm an email-verification token (linked from the verification email).

**Query parameters:** `token` (required).

**Response (200):**

```json
{ "message": "Email verified successfully" }
```

**Errors:** `400 missing_token`, `400 invalid_token`.

---

### POST /api/auth/resend-verification

Resend the verification email for the current user. Allowed even when `email_verified` is `false` (path is in
`VERIFICATION_EXEMPT_PATHS`).

**Request body:** none.

**Response (200):**

```json
{ "message": "Verification email sent" }
```

If the user is already verified, returns 200 with `{"message": "Email is already verified"}` and does not send.

**Errors:** `500 email_failed` (SMTP send failed), `500 email_not_configured`.

---

### POST /api/auth/api-keys

Create a new API key for the authenticated user. The full key value is returned **once** in the response and is
otherwise unrecoverable — the server only stores a hash.

**Request body:**

```json
{ "name": "nanoclaw bot", "expires_days": 90 }
```

`name` is required. `expires_days` is optional; when set, must be a positive integer.

**Response (201):**

```json
{
 "id": 7,
 "user_id": 1,
 "key_prefix": "jrnl_a1b2",
 "name": "nanoclaw bot",
 "created_at": "2026-04-12T10:00:00+00:00",
 "expires_at": "2026-07-11T10:00:00+00:00",
 "last_used_at": null,
 "revoked_at": null,
 "key": "jrnl_a1b2c3d4e5f6..."
}
```

`key` is the full Bearer token; persist it client-side. Subsequent reads (GET) will not include it.

**Errors:** `400 invalid_body`, `400 missing_fields`, `400 invalid_field`.

---

### GET /api/auth/api-keys

List the authenticated user's API keys. Secret material is never returned.

**Response (200):**

```json
{
 "items": [
  {
   "id": 7,
   "user_id": 1,
   "key_prefix": "jrnl_a1b2",
   "name": "nanoclaw bot",
   "created_at": "...",
   "expires_at": "...",
   "last_used_at": "2026-04-15T08:01:11+00:00",
   "revoked_at": null
  }
 ]
}
```

---

### DELETE /api/auth/api-keys/{key_id}

Revoke an API key owned by the authenticated user.

**Response (200):**

```json
{ "ok": true }
```

**Errors:** `404 not_found` (unknown id, not owned by the caller, or already revoked).

---

## Admin endpoints

**Admin only.** All routes require the caller's `is_admin` flag to be set; non-admin callers receive
`403 forbidden`.

### GET /api/admin/users

List all users with stats (entry counts, last activity, etc.). The exact stat fields come from
`SQLiteUserRepository.get_user_stats`.

**Response (200):**

```json
{
 "items": [
  {
   "id": 1,
   "email": "alice@example.com",
   "display_name": "Alice",
   "is_admin": false,
   "is_active": true,
   "email_verified": true,
   "entry_count": 42,
   "last_entry_at": "2026-04-12"
  }
 ]
}
```

---

### PATCH /api/admin/users/{user_id}

Update a user's role or active flag. Only `is_admin` and `is_active` are accepted; both must be booleans.

**Request body:**

```json
{ "is_active": false }
```

**Response (200):** `{"user": { ... }}` with the updated record.

**Errors:** `400 invalid_body`, `400 invalid_field`, `400 missing_fields`, `404 not_found`.

---

### POST /api/admin/reload/ocr-context

**Admin only.** Re-read the OCR glossary directory and rebuild the OCR provider.

**Response (200):** A summary dict from the helper (paths re-read, glossary entry counts, etc.).

**Errors:** `409 reload_unavailable` if the helper raises (e.g. the underlying feature is disabled or the
configuration is invalid).

---

### POST /api/admin/reload/transcription-context

**Admin only.** Re-read the OCR/transcription glossary directory and rebuild the transcription stack (primary,
fallback, and shadow providers). Same response/error contract as `/reload/ocr-context`.

---

### POST /api/admin/reload/mood-dimensions

**Admin only.** Re-read the mood-dimensions TOML and rebuild the mood-scoring service. Returns the helper summary
(loaded version, dimension count). When mood scoring is disabled, the helper raises `RuntimeError` and the route
returns `409 reload_unavailable`.

---

### POST /api/admin/reload/entity-casing

**Admin only.** Re-read the entity-casing exceptions TOML and rebind it on the entity store. Same response/error
contract as the other reload routes.

---

## Settings & preferences

Server-wide config (`/api/settings*`) is partly read-only and partly admin-only; per-user preferences live under
`/api/users/me/preferences`.

### GET /api/settings

Return a non-secret snapshot of the running server's configuration: provider/model selections (OCR, transcription,
embeddings), chunking parameters, hybrid-search knobs, runtime feature flags, and the current pricing table. Secret
fields (API keys, bearer tokens, Slack tokens) are not included.

**Response (200):** A `{"ocr": {...}, "transcription": {...}, "transcript_formatting": {...}, "embedding": {...},
"chunking": {...}, "entity_extraction": {...}, "search": {...}, "features": {...}, "runtime": [...], "pricing":
[...]}` envelope. Field shapes follow the dataclasses in `journal.config.Config` and the runtime/pricing repository
helpers.

---

### GET /api/settings/runtime

Return all runtime-editable settings with metadata (key, current value, type, description).

**Response (200):**

```json
{ "settings": [ { "key": "registration_enabled", "value": true, "type": "bool", "description": "..." } ] }
```

---

### PATCH /api/settings/runtime

**Admin only.** Update one or more runtime settings.

**Request body:** `{"key": value, ...}` — each key must be a known runtime setting; the value is coerced to the
declared type.

**Response (200):**

```json
{ "updated": ["registration_enabled"], "settings": [ ... ], "warnings": [] }
```

If every key in the body fails validation, returns 400 with a single-key `{"error": "..."}` body. If some succeed and
some fail, the failed keys are returned in `warnings` alongside the successful `updated` list.

---

### GET /api/settings/pricing

Return the model-pricing table used for cost reporting (token-cost-per-million per model).

**Response (200):**

```json
{ "pricing": [ { "model_name": "claude-haiku-4-5", "input_per_mtok": 1.0, "output_per_mtok": 5.0, "cached_input_per_mtok": 0.1, "updated_at": "..." } ] }
```

---

### PATCH /api/settings/pricing

**Admin only.** Update pricing for one or more models. The body is a `{model_name: {field: value, ...}, ...}` map.

**Response codes:**

- `200` — every model in the body was applied.
- `207` — some models updated, others rejected (unknown model, no valid fields). The body's `updated` list and
  `errors` list together describe what happened.
- `400` — every entry in the body failed validation.

**Response body:**

```json
{ "updated": ["claude-haiku-4-5"], "pricing": [ ... ], "errors": ["claude-mystery: unknown model or no valid fields"] }
```

---

### GET /api/users/me/preferences

Return all per-user preference key/value pairs for the authenticated user.

**Response (200):**

```json
{ "preferences": { "pushover_user_key": "...", "notif_job_success_ingest_images": true } }
```

---

### PATCH /api/users/me/preferences

Partial update of the current user's preferences. Body is a JSON object whose keys are preference names and whose
values are any JSON-serialisable scalars/objects.

**Request body:**

```json
{ "notif_job_success_ingest_images": false }
```

**Response (200):** the full preferences map after the update (same shape as GET).

**Errors:**

- `400` — invalid JSON, body not an object, or a non-string key.
- `403` — the body sets a preference key that is in the admin-only set (currently the admin-only notification
  topics defined by `journal.services.notifications.TOPICS`).

---

### GET /api/health

Authenticated mirror of `/health` plus a per-user `fitness` block surfacing the
configured sources' auth status and freshness. Provided because the webapp's
nginx only proxies `/api/*` through to the server, so the unauthenticated
`/health` path is unreachable from the browser. The unauthenticated `/health`
does **not** include the `fitness` block — the per-user filter only applies on
this route.

The `fitness` block, when populated, has one entry per source the user has
configured:

```json
{
 "fitness": {
  "strava": {
   "auth_status": "ok",
   "last_success_at": "2026-05-09T18:42:11Z",
   "auth_broken_since": null
  },
  "garmin": {
   "auth_status": "broken",
   "last_success_at": "2026-05-07T06:11:02Z",
   "auth_broken_since": "2026-05-08T11:14:33Z"
  }
 }
}
```

A source is omitted when the user has no `fitness_auth_state` row and no
`fitness_sync_runs` rows for it (i.e. never connected).

The overall `status` downgrades to `degraded` when any source has been
`auth_status="broken"` for longer than `FITNESS_HEALTH_BROKEN_DEGRADED_HOURS`
(default 48). See [`fitness-operations.md`](fitness-operations.md) for the
operator runbook.

---

## Dashboard endpoints (additional)

The dashboard cluster carries four more routes beyond the five above (`mood-dimensions`,
`mood-trends`, `mood-drilldown`, `entity-distribution`, `writing-stats`).

### GET /api/dashboard/calendar-heatmap

Daily entry counts and word totals, used by the dashboard's calendar heatmap.

**Query parameters:**

| Parameter | Type   | Required | Description                   |
| --------- | ------ | -------- | ----------------------------- |
| `from`    | string | no       | Inclusive ISO-8601 start date |
| `to`      | string | no       | Inclusive ISO-8601 end date   |

**Response (200):**

```json
{
 "from": "2026-01-01",
 "to": "2026-04-20",
 "days": [
  { "date": "2026-04-12", "entry_count": 1, "total_words": 412 }
 ]
}
```

Days with zero entries are omitted.

---

### GET /api/dashboard/entity-trends

Top-N entity mention counts over time, bucketed by `bin`. Used to show which topics wax and wane.

**Query parameters:**

| Parameter | Type   | Required | Default | Description                                                 |
| --------- | ------ | -------- | ------- | ----------------------------------------------------------- |
| `bin`     | string | no       | `month` | `week`, `month`, `quarter`, or `year`                       |
| `from`    | string | no       |         | Inclusive ISO-8601 start date                               |
| `to`      | string | no       |         | Inclusive ISO-8601 end date                                 |
| `type`    | string | no       |         | Filter by entity_type (`person`, `place`, `topic`, ...)     |
| `limit`   | int    | no       | 8       | Top N entities to track (capped at 50)                      |

**Response (200):**

```json
{
 "from": "2026-01-01",
 "to": "2026-04-20",
 "bin": "month",
 "entity_type": "person",
 "entities": ["Atlas", "Lizzie", "Robyn"],
 "bins": [
  { "period": "2026-01-01", "entity_name": "Atlas", "mention_count": 4 },
  { "period": "2026-02-01", "entity_name": "Atlas", "mention_count": 7 }
 ]
}
```

The flat `entities` list at the top level names the top-N selected; `bins` carries one row per (period, entity)
combination that had at least one mention.

**Errors:** `400 invalid_bin`, `503 Server not initialized`.

---

### GET /api/dashboard/mood-entity-correlation

Average mood score per entity, compared against the overall corpus average for the same window. Surfaces "entities
that correlate with feeling X".

**Query parameters:**

| Parameter   | Type   | Required | Default | Description                                  |
| ----------- | ------ | -------- | ------- | -------------------------------------------- |
| `dimension` | string | yes      |         | The mood dimension name                      |
| `from`      | string | no       |         | Inclusive ISO-8601 start date                |
| `to`        | string | no       |         | Inclusive ISO-8601 end date                  |
| `type`      | string | no       |         | Filter by entity_type                        |
| `limit`     | int    | no       | 10      | Top N entities (capped at 50)                |

**Response (200):**

```json
{
 "dimension": "joy_sadness",
 "from": "2026-01-01",
 "to": "2026-04-20",
 "entity_type": null,
 "overall_avg": 0.21,
 "items": [
  { "canonical_name": "Atlas", "entity_type": "person", "avg_score": 0.62, "entry_count": 11 },
  { "canonical_name": "rain", "entity_type": "topic", "avg_score": -0.34, "entry_count": 6 }
 ]
}
```

**Errors:** `400 missing_dimension`, `503`.

---

### GET /api/dashboard/word-count-distribution

Histogram of entry word counts plus summary statistics. Used to characterise typical entry length.

**Query parameters:**

| Parameter     | Type   | Required | Default | Description                                       |
| ------------- | ------ | -------- | ------- | ------------------------------------------------- |
| `from`        | string | no       |         | Inclusive ISO-8601 start date                     |
| `to`          | string | no       |         | Inclusive ISO-8601 end date                       |
| `bucket_size` | int    | no       | 100     | Histogram bucket width in words (minimum 10)      |

**Response (200):**

```json
{
 "from": "2026-01-01",
 "to": "2026-04-20",
 "bucket_size": 100,
 "buckets": [
  { "bucket_start": 0, "bucket_end": 100, "entry_count": 4 },
  { "bucket_start": 100, "bucket_end": 200, "entry_count": 9 }
 ],
 "stats": { "min": 12, "max": 1840, "mean": 412.7, "median": 380, "p90": 940, "total_entries": 42 }
}
```

---

## Entity merge — pair decisions

Companion to `POST /api/entities/merge` and `/api/entities/merge-candidates`. Pair decisions are the user's persistent
"these two entities are NOT duplicates" rejections, surfaced so they can be audited and undone.

### GET /api/entities/pair-decisions

List the authenticated user's stored pair-rejection decisions.

**Query parameters:**

| Parameter | Type | Required | Default | Description                              |
| --------- | ---- | -------- | ------- | ---------------------------------------- |
| `limit`   | int  | no       | 50      | Max decisions to return (capped at 200)  |
| `offset`  | int  | no       | 0       | Pagination offset                        |

**Response (200):**

```json
{
 "items": [
  {
   "id": 4,
   "entity_a": { "id": 5, "canonical_name": "Liz", "entity_type": "person", "...": "..." },
   "entity_b": { "id": 12, "canonical_name": "Lizzie", "entity_type": "person", "...": "..." },
   "decision": "not_duplicate",
   "decided_at": "2026-04-12T10:30:00+00:00"
  }
 ],
 "total": 1
}
```

`total` is the unpaginated count of stored decisions for the user.

---

### DELETE /api/entities/pair-decisions/{decision_id}

Undo a "not a duplicate" decision. Removes the rejection so the pair is once again eligible to be flagged as a merge
candidate by future extraction runs.

**Response (200):**

```json
{ "id": 4, "deleted": true }
```

**Response (404):** `{"error": "Decision not found"}` if the decision id is unknown or not owned by the caller.

---

## Fitness endpoints

Five endpoints expose the W4–W13 fitness pipeline. Read routes live in
`api/fitness.py`; the job-creation `POST /api/fitness/sync/{source}` lives in
`api/ingestion.py` per the codebase's write-in-ingestion-router convention. For
the data flow, see [`fitness-pipeline.md`](fitness-pipeline.md); for re-auth,
backfill, and troubleshooting, see [`fitness-operations.md`](fitness-operations.md).

### GET /api/fitness/activities

Activities (run, ride, swim, walk, hike, strength, other) in a date window.

**Query parameters:**

| Parameter | Type   | Required | Description                                                              |
| --------- | ------ | -------- | ------------------------------------------------------------------------ |
| `start`   | string | yes      | Inclusive start date (`YYYY-MM-DD`).                                     |
| `end`    | string | yes      | Inclusive end date (`YYYY-MM-DD`).                                       |
| `type`    | string | no       | Filter by canonical activity type. Omit for all types.                   |

**Response (200):**

```json
{
 "items": [
  {
   "id": 42,
   "user_id": 1,
   "source": "strava",
   "source_id": "16971789898",
   "activity_type": "run",
   "source_subtype": "Run",
   "start_time": "2026-05-09T07:14:33Z",
   "local_date": "2026-05-09",
   "duration_s": 1832,
   "moving_time_s": 1820,
   "distance_m": 5210.4,
   "elevation_gain_m": 31.5,
   "avg_hr_bpm": 154,
   "max_hr_bpm": 178,
   "avg_pace_s_per_km": 351.6,
   "calories_kcal": 412,
   "perceived_exertion": null,
   "extras": {},
   "raw_ref_id": 891,
   "normalized_at": "2026-05-09T07:25:18Z"
  }
 ]
}
```

Empty `items` is a valid response — out-of-range windows are not an error.

**Errors:** `400` if `start` or `end` is missing; `503` if services aren't
initialised (server still booting).

---

### GET /api/fitness/daily

Daily wellness rollups (sleep, HRV, body battery, stress, training load /
readiness, resting HR) in a date window. Strava does not contribute daily rows;
this endpoint returns Garmin data exclusively today.

**Query parameters:**

| Parameter | Type   | Required | Description                          |
| --------- | ------ | -------- | ------------------------------------ |
| `start`   | string | yes      | Inclusive start date (`YYYY-MM-DD`). |
| `end`    | string | yes      | Inclusive end date (`YYYY-MM-DD`).   |

**Response (200):**

```json
{
 "items": [
  {
   "id": 7,
   "user_id": 1,
   "source": "garmin",
   "local_date": "2026-05-09",
   "sleep_score": 78,
   "sleep_duration_s": 25200,
   "sleep_efficiency_pct": 91.2,
   "hrv_overnight_ms": 78.4,
   "resting_hr_bpm": 41,
   "body_battery_high": 72,
   "body_battery_low": 18,
   "stress_avg": 24,
   "training_load_acute": 412,
   "training_load_chronic": 388,
   "training_readiness": 73,
   "extras": {},
   "raw_ref_ids": [1023, 1024, 1025, 1026, 1027, 1028],
   "normalized_at": "2026-05-09T08:11:02Z"
  }
 ]
}
```

`raw_ref_ids` carries one id per upstream daily endpoint (six for Garmin) so
the soft-pointer integrity check can verify provenance.

---

### GET /api/fitness/sync/status

Per-source snapshot — auth status, last success, and the most recent ten sync
runs. Mirrors `journal fitness-status`.

**Response (200):**

```json
{
 "strava": {
  "auth_status": "ok",
  "auth_broken_since": null,
  "last_success_at": "2026-05-09T18:42:11Z",
  "last_runs": [
   {
    "id": 233,
    "started_at": "2026-05-09T18:42:08Z",
    "finished_at": "2026-05-09T18:42:11Z",
    "status": "success",
    "rows_fetched": 12,
    "rows_normalized": 12,
    "error_class": null,
    "error_message": null
   }
  ]
 },
 "garmin": null
}
```

A source is `null` (rather than a default-populated dict) when this user has
neither a `fitness_auth_state` row nor any `fitness_sync_runs` rows for it.
`null` lets the webapp distinguish "first-use, never connected" from
"configured but no successful sync yet" — only the first deserves a connect
CTA.

---

### POST /api/fitness/sync/{source}

Queue a fitness fetch + normalize job for `source` (`"strava"` or `"garmin"`).
Asynchronous — the job runs through the W8 worker and JobRunner; poll
`GET /api/jobs/{job_id}` for status.

**Path parameter:**

| Parameter | Type   | Description              |
| --------- | ------ | ------------------------ |
| `source`  | string | `"strava"` or `"garmin"` |

**Response (202, queued):**

```json
{ "job_id": "8e12...", "status": "queued" }
```

**Response (202, dedup'd against an in-flight job):**

```json
{ "job_id": "8e12...", "status": "running", "already_running": true }
```

A single in-flight (`queued` or `running`) job per `(user_id, source)` is
allowed. Subsequent submits return the existing job id with `already_running:
true` instead of queueing a duplicate. The W6 fetch service has its own
single-run guard at the layer below — deduping here keeps the operator-facing
audit trail clean (one job row per real sync, not one per button-press).

**Errors:**

- `400` `{"error": "Unknown fitness source: ..."}` — `source` is not
  `"strava"` or `"garmin"`.
- `503` `{"error": "Strava fitness sync is not configured on this server ..."}`
  — the source's credential vars (`STRAVA_CLIENT_ID` /
  `STRAVA_CLIENT_SECRET` for Strava, `GARMIN_USERNAME` / `GARMIN_PASSWORD`
  for Garmin) are unset. Operator can tell *feature off* from *real bug*.

---

### GET /api/fitness/integrity

Soft-pointer orphan report — normalized rows whose `raw_ref_id` (or any id in
`raw_ref_ids_json`) doesn't resolve into the matching per-source raw table.

**Response (200):**

```json
{
 "activities": [],
 "daily": []
}
```

Empty arrays indicate a clean database. A non-empty entry means a normalized
row references a raw row that's been deleted or never existed — a data-shape
regression worth triaging.

---

### POST /api/fitness/garmin/connect

Begin the per-user Garmin Connect login flow (W2 of the multi-user plan). The
calling user's `user_id` is taken from the authenticated session — credentials
in the body authenticate the *upstream* Garmin account, not the journal user.

**Request body:**

```json
{ "username": "alice@example.com", "password": "..." }
```

The plaintext password is consumed once: it's passed to `garminconnect.Garmin`
inside the request handler, the login result is captured, and the password is
dropped before the response is sent. It is never logged or persisted.

**Response (200, no MFA needed):**

```json
{ "connected": true, "upstream_user_id": "alice.j.garmin" }
```

The upstream account identifier (Garmin `displayName`, falling back to the
username) is recorded in `fitness_auth_state.extra_state_json["upstream_user_id"]`
to power the D8 reconnect-with-different-account check.

**Response (200, MFA needed):**

```json
{
  "mfa_required": true,
  "pending_session": "h7-…base64url…-Q",
  "expires_at": "2026-05-10T12:34:56Z"
}
```

The opaque `pending_session` is a 256-bit CSPRNG token (URL-safe base64). It
binds to the calling user's `user_id` for 10 minutes and must be presented to
`POST /api/fitness/garmin/connect/mfa` along with the 6-digit Garmin code. After
expiry, or after one successful consume, the entry is gone — the user repeats
the connect flow.

**Errors:**

- `400` `{"error": "username and password are required"}` — body missing
  either field.
- `401` `{"error": "Garmin rejected those credentials.", "reason":
  "invalid_credentials"}` — `GarminConnectAuthenticationError`. The
  per-email cool-down counter is incremented; after 5 failures within 15
  minutes subsequent attempts are refused with `429` (see below) until the
  window rolls forward.
- `429` `{"error": "Too many failed Garmin login attempts for that account…",
  "retry_after_seconds": 240}` — either local cool-down (per-email failure
  counter tripped) OR upstream `GarminConnectTooManyRequestsError`. The
  cool-down keys on the supplied email so a user typo'ing twice does not
  lock the whole server. The local check fires before any upstream call,
  so it cannot deepen an existing Garmin lockout.
- `502` `{"error": "Garmin login failed: …", "reason": "upstream_error"}` —
  any other exception out of `garminconnect`.

The connect endpoint also enforces the D8 *reconnect with different upstream
account* rule. If the user already has a `fitness_auth_state` row for
`source='garmin'` and the freshly-fetched upstream id differs from the stored
one, the response is `409 {"error": "...", "reason":
"upstream_account_mismatch", "stored_upstream_user_id": "...", "incoming_upstream_user_id": "..."}`
and the user is directed to disconnect first.

---

### POST /api/fitness/garmin/connect/mfa

Complete a pending MFA-required Garmin login. The pending session must have
been issued to the same authenticated user — a token leaked between users is
rejected with 403, not silently consumed.

**Request body:**

```json
{ "pending_session": "…", "code": "123456" }
```

**Response (200):**

```json
{ "connected": true, "upstream_user_id": "alice.j.garmin" }
```

Success persists the token blob (`client.client.dumps()`) into
`fitness_auth_state.extra_state_json["tokens_blob"]` plus the upstream id, sets
`auth_status='ok'`, clears `auth_broken_since`, and stamps
`last_successful_login_at`. Subsequent fitness syncs boot from the DB row, not
the filesystem cache.

**Errors:**

- `400` `{"error": "pending_session and code are required"}` — body missing
  either field.
- `403` `{"error": "Pending session does not belong to this user.", "reason":
  "cross_user_pending_session"}` — the token was issued to another user.
  Cross-user replay protection per D2.
- `410` `{"error": "Pending session expired or unknown. Repeat the connect
  step.", "reason": "expired_pending_session"}` — token unknown, expired
  (10 min TTL), or already consumed.
- `401` `{"error": "Garmin rejected the MFA code.", "reason":
  "invalid_mfa_code"}` — `GarminConnectAuthenticationError` from
  `resume_login`. The pending entry is preserved so the user can retry with
  a fresh code (subject to the per-email cool-down).
- `502` `{"error": "Garmin accepted the MFA code but the post-login profile
  fetch failed.", "reason": "post_mfa_profile_fetch_failed"}` — Garmin's
  user-profile endpoint is intermittently flaky after a fresh MFA flow
  (`python-garminconnect` issues #312/#337). Surface distinctly so the UI
  prompts the user to retry rather than blaming bad credentials.
- `409` upstream-id mismatch — same shape as `connect`.

---

### POST /api/fitness/garmin/disconnect

Delete the calling user's `fitness_auth_state` row for `source='garmin'`.
Idempotent — disconnecting a not-connected source returns `{disconnected:
false}`. Existing `fitness_activities` and `fitness_daily` rows are *not*
deleted; the user keeps their historical data and can reconnect with the same
upstream account to resume syncing.

**Response (200):**

```json
{ "disconnected": true }
```

A subsequent successful connect with a *different* upstream account is
explicitly refused (D8) — disconnecting only ungates the row, it does not
clear the audit trail of which upstream account the prior tokens belonged to.

---

### GET /api/fitness/strava/authorize_url

Issue an authorize URL plus a one-shot CSRF state token for the per-user
Strava OAuth flow (W3 of the multi-user plan). The state token is bound to the
calling user's `user_id` for 10 minutes; presenting it back at
`POST /api/fitness/strava/exchange` under a different authenticated user is
rejected with 403.

**Response (200):**

```json
{
  "authorize_url": "https://www.strava.com/oauth/authorize?client_id=…&redirect_uri=…&response_type=code&approval_prompt=auto&scope=read,activity:read_all&state=h7-…base64url…-Q",
  "state": "h7-…base64url…-Q",
  "expires_at": "2026-05-10T12:34:56Z"
}
```

The `redirect_uri` query parameter is sourced from the `STRAVA_REDIRECT_URI`
env var. Per D4 of the multi-user plan, this is the webapp callback route
(`https://<webapp>/settings/fitness/strava/callback`) — the SPA reads `code`
and `state` from the redirect query, then POSTs them to `exchange`.

**Errors:**

- `503` `{"error": "Server not initialized"}` — services dict not yet wired
  (startup race; retry).
- `500` `{"error": "STRAVA_CLIENT_ID is not configured"}` — operator must set
  the env var. The endpoint refuses to mint an authorize URL with a missing
  client id rather than redirecting users to a guaranteed Strava error page.

---

### POST /api/fitness/strava/exchange

Complete the Strava OAuth flow: validate the state token, exchange the
authorization `code` for a refresh/access pair, capture the upstream
`athlete.id` for D8, and persist into `fitness_auth_state`. Idempotent up to
state-token consumption — once a state is consumed, replaying the same code
under the same state is a 410.

**Request body:**

```json
{ "code": "…", "state": "h7-…base64url…-Q" }
```

**Response (200):**

```json
{ "connected": true, "upstream_user_id": "12345678" }
```

Strava's `athlete.id` is an integer; we store it as a string in
`extra_state_json["upstream_user_id"]` so the D8 mismatch comparison stays
purely string-vs-string (matching the Garmin shape). The triple of access
token, refresh token, and ISO-8601 token expiry lands in the
`fitness_auth_state` columns; `auth_status='ok'`, `auth_broken_since=null`,
and `last_successful_login_at` is stamped.

**Errors:**

- `400` `{"error": "code and state are required"}` — body missing either
  field.
- `403` `{"error": "Pending state does not belong to this user.", "reason":
  "cross_user_pending_session"}` — the state token was issued to another
  user. Cross-user replay protection per D4.
- `410` `{"error": "Pending state expired or unknown. Repeat the connect
  step.", "reason": "expired_pending_state"}` — token unknown, expired
  (10 min TTL), or already consumed.
- `502` `{"error": "Strava rejected the authorization code.", "reason":
  "upstream_error"}` — Strava returned 401/403 on the token exchange
  (revoked code, mismatched client secret).
- `409` upstream-id mismatch — same shape as the Garmin endpoints. If a
  `fitness_auth_state` row already exists for this user/source and the
  newly-exchanged `athlete.id` differs from the stored one, the response is
  `{"error": "...", "reason": "upstream_account_mismatch",
  "stored_upstream_user_id": "...", "incoming_upstream_user_id": "..."}` and
  the user is directed to disconnect first.

---

### POST /api/fitness/strava/disconnect

Delete the calling user's `fitness_auth_state` row for `source='strava'`.
Idempotent — disconnecting a not-connected source returns `{disconnected:
false}`. Existing `fitness_activities` and `fitness_daily` rows are *not*
deleted; the user keeps their historical data and can reconnect with the same
upstream Strava athlete to resume syncing.

**Response (200):**

```json
{ "disconnected": true }
```

A subsequent successful connect with a *different* upstream `athlete.id` is
explicitly refused (D8) — disconnecting only ungates the row.

---

# MCP Tool Reference

The journal MCP server exposes its tools via streamable HTTP transport.

## Query Tools

### journal_search_entries

Hybrid search across journal entries. Each call runs BM25 over SQLite FTS5 in parallel with dense embedding retrieval
over ChromaDB, fuses the two ranked lists with Reciprocal Rank Fusion (`k = 60`), and reranks the fused top-M
candidates with the configured listwise reranker (default: Claude Haiku 4.5). There is no mode toggle — every search
combines keyword and semantic signals. See [`search.md`](search.md) for the full pipeline and configuration.

| Parameter    | Type   | Required | Default | Description                  |
| ------------ | ------ | -------- | ------- | ---------------------------- |
| `query`      | string | yes      |         | Natural language query       |
| `start_date` | string | no       |         | Filter from date (ISO 8601)  |
| `end_date`   | string | no       |         | Filter until date (ISO 8601) |
| `limit`      | int    | no       | 10      | Max results (1-50)           |
| `offset`     | int    | no       | 0       | Pagination offset            |

### journal_get_entries_by_date

Get all entries for a specific date.

| Parameter | Type   | Required | Description             |
| --------- | ------ | -------- | ----------------------- |
| `date`    | string | yes      | Date in ISO 8601 format |

### journal_list_entries

List entries in reverse chronological order.

| Parameter    | Type   | Required | Default | Description        |
| ------------ | ------ | -------- | ------- | ------------------ |
| `start_date` | string | no       |         | Filter from date   |
| `end_date`   | string | no       |         | Filter until date  |
| `limit`      | int    | no       | 20      | Max results (1-50) |
| `offset`     | int    | no       | 0       | Pagination offset  |

## Statistics Tools

### journal_get_statistics

Get journal statistics: entry count, frequency, word counts, date range.

| Parameter    | Type   | Required | Default  | Description     |
| ------------ | ------ | -------- | -------- | --------------- |
| `start_date` | string | no       | all time | Start of period |
| `end_date`   | string | no       | today    | End of period   |

### journal_get_mood_trends

Analyze mood trends over time.

| Parameter     | Type   | Required | Default | Description               |
| ------------- | ------ | -------- | ------- | ------------------------- |
| `start_date`  | string | no       |         | Start of period           |
| `end_date`    | string | no       |         | End of period             |
| `granularity` | string | no       | "week"  | "day", "week", or "month" |

### journal_get_topic_frequency

Count how often a topic, person, or place appears.

| Parameter    | Type   | Required | Description         |
| ------------ | ------ | -------- | ------------------- |
| `topic`      | string | yes      | Topic to search for |
| `start_date` | string | no       | Start of period     |
| `end_date`   | string | no       | End of period       |

## Ingestion Tools

### journal_ingest_text

Create a journal entry from plain text. No OCR or transcription — the text is stored directly, chunked, embedded, and
indexed.

| Parameter     | Type   | Required | Default        | Description                              |
| ------------- | ------ | -------- | -------------- | ---------------------------------------- |
| `text`        | string | yes      |                | The journal entry text content           |
| `date`        | string | no       | today          | Entry date (ISO 8601)                    |
| `source_type` | string | no       | "text_entry"   | Entry source type                        |

For handwritten page images or audio recordings, use `journal_ingest_media_from_url` or `journal_ingest_media` instead.

### journal_ingest_media_from_url

Ingest a **single** journal page image or voice note by downloading it from a URL. This is the preferred media ingestion
method for MCP clients like Nanoclaw, since it avoids base64-encoding large files as tool parameters.

> **Renamed:** Previously `journal_ingest_from_url`. Updated 2026-04-15 for clarity alongside the new
> `journal_ingest_text` tool.

| Parameter     | Type   | Required | Default | Description                                        |
| ------------- | ------ | -------- | ------- | -------------------------------------------------- |
| `source_type` | string | yes      |         | "image" or "voice"                                 |
| `url`         | string | yes      |         | URL to download the file from                      |
| `media_type`  | string | no       |         | MIME type override (inferred from response header) |
| `date`        | string | no       | today   | Entry date (ISO 8601)                              |
| `language`    | string | no       | "en"    | Language for voice transcription                   |

**Slack file URLs** (`files.slack.com`) are automatically authenticated using the `SLACK_BOT_TOKEN` environment variable.
No auth headers needed in the tool call — just pass the raw `url_private` or `url_private_download` URL from Slack.

For other URLs, the server makes a plain HTTP GET with no authentication. The URL must be accessible from the journal
server's network.

> **Multi-page entries:** If a single journal entry spans multiple photos, do NOT call this tool once per page — each
> call creates a separate entry. Use [`journal_ingest_multi_page_from_url`](#journal_ingest_multi_page_from_url) instead.

### journal_ingest_multi_page_from_url

Ingest multiple page images (by URL) as a **single** multi-page journal entry. All images are downloaded, OCR'd
page-by-page, and combined into one entry with one page record per image. This is the preferred way to ingest multi-page
entries from URL-based clients (e.g. Slack-driven agents).

| Parameter     | Type         | Required | Default | Description                                         |
| ------------- | ------------ | -------- | ------- | --------------------------------------------------- |
| `urls`        | list[string] | yes      |         | Ordered list of page image URLs, one per page       |
| `media_types` | list[string] | no       |         | Per-URL MIME type overrides (same length as `urls`) |
| `date`        | string       | no       | today   | Entry date (ISO 8601)                               |

Slack file URLs are authenticated the same way as in `journal_ingest_media_from_url`. If a page within the batch matches
an already-ingested file hash, ingestion fails with an "already ingested" error before any entry is created.

### journal_ingest_media

Ingest a journal entry from a base64-encoded image or voice note. Use `journal_ingest_media_from_url` instead when the
file is available at a URL — this avoids MCP tool parameter size limits.

> **Renamed:** Previously `journal_ingest_entry`. Updated 2026-04-15 for clarity alongside the new
> `journal_ingest_text` tool.

| Parameter     | Type   | Required | Default | Description                      |
| ------------- | ------ | -------- | ------- | -------------------------------- |
| `source_type` | string | yes      |         | "image" or "voice"               |
| `data_base64` | string | yes      |         | Base64-encoded file data         |
| `media_type`  | string | yes      |         | MIME type (e.g. "image/jpeg")    |
| `date`        | string | no       | today   | Entry date (ISO 8601)            |
| `language`    | string | no       | "en"    | Language for voice transcription |

### journal_ingest_multi_page

Ingest multiple images as pages of a single journal entry from base64-encoded data. Images are OCR'd individually and
combined into one entry. Prefer `journal_ingest_multi_page_from_url` when the images are available at URLs.

| Parameter       | Type         | Required | Default | Description                                  |
| --------------- | ------------ | -------- | ------- | -------------------------------------------- |
| `images_base64` | list[string] | yes      |         | Base64-encoded page images (ordered)         |
| `media_types`   | list[string] | yes      |         | Per-image MIME types (same length as images) |
| `date`          | string       | no       | today   | Entry date (ISO 8601)                        |

### journal_update_entry_text

Update an entry's `final_text` to correct OCR errors. Triggers re-chunking, re-embedding, and FTS5 rebuild. The original
`raw_text` is preserved.

| Parameter    | Type   | Required | Description        |
| ------------ | ------ | -------- | ------------------ |
| `entry_id`   | int    | yes      | Entry ID to update |
| `final_text` | string | yes      | Corrected text     |

## Entity Tools

Tools that read and write the extracted-entity graph. Entity extraction itself is exposed as both a legacy synchronous
tool (`journal_extract_entities`) and an async batch wrapper (`journal_extract_entities_batch`, documented under
[Batch Job Tools](#batch-job-tools)).

### journal_extract_entities

Run the entity extraction batch job over one or more entries synchronously. **Legacy** — this tool blocks the MCP call
for the full duration of extraction and does not produce progress events. New code should use
`journal_extract_entities_batch` instead, which routes through the shared JobRunner and matches the semantics used by the
webapp.

| Parameter    | Type   | Required | Default | Description                                                      |
| ------------ | ------ | -------- | ------- | ---------------------------------------------------------------- |
| `entry_id`   | int    | no       |         | Extract from this single entry only                              |
| `start_date` | string | no       |         | Filter entries from this date (ISO 8601)                         |
| `end_date`   | string | no       |         | Filter entries until this date (ISO 8601)                        |
| `stale_only` | bool   | no       | false   | Only process entries flagged stale since the last extraction run |

Returns a human-readable summary string with aggregated counts (`entities_created`, `entities_matched`,
`mentions_created`, `relationships_created`) and any warnings emitted by the extractor.

### journal_list_entities

List extracted entities, optionally filtered by type.

| Parameter     | Type   | Required | Default | Description                                                            |
| ------------- | ------ | -------- | ------- | ---------------------------------------------------------------------- |
| `entity_type` | string | no       |         | One of `person`, `place`, `activity`, `organization`, `topic`, `other` |
| `limit`       | int    | no       | 50      | Max results (capped at 200)                                            |

Returns a string listing each entity as `[id] type: canonical_name — N mentions (aliases: ...)`.

### journal_get_entity_mentions

Return every recorded mention of a specific entity across the journal.

| Parameter   | Type | Required | Default | Description            |
| ----------- | ---- | -------- | ------- | ---------------------- |
| `entity_id` | int  | yes      |         | The entity to look up  |
| `limit`     | int  | no       | 50      | Max mentions to return |

Returns a string with one mention per line — `entry N: "quoted span" (confidence X.XX)`. Returns `Entity {id} not found.`
if the entity does not exist.

### journal_get_entity_relationships

Return the outgoing and incoming relationships that touch an entity. Outgoing edges are triples where the entity is the
subject; incoming edges are triples where it is the object.

| Parameter   | Type | Required | Description                      |
| ----------- | ---- | -------- | -------------------------------- |
| `entity_id` | int  | yes      | The entity whose edges to return |

Returns a string grouped into outgoing and incoming sections. Each edge renders as
`-> predicate -> other_entity (entry N, conf X.XX)` (or the reverse for incoming). Returns `Entity {id} not found.` or
`No relationships recorded for {name}.` when appropriate.

## Batch Job Tools

Async batch-job wrappers around the same `JobRunner` that backs the REST endpoints. The two `_batch` tools **block** the
MCP tool call until the job reaches a terminal state — they poll the jobs table every 500 ms with a default timeout of
3600 s. Failed jobs return a structured dict rather than raising, so the caller can read the error message and respond to
the user.

See [jobs.md](jobs.md) for the full data model, result payload shapes, and restart-recovery semantics.

### journal_extract_entities_batch

Submit an entity-extraction job and block until it finishes. Same parameter shape as the legacy synchronous
`journal_extract_entities` tool, but routed through the shared `JobRunner` so progress is persisted in the jobs table and
the result matches what the webapp consumes.

| Parameter    | Type   | Required | Default | Description                                                      |
| ------------ | ------ | -------- | ------- | ---------------------------------------------------------------- |
| `entry_id`   | int    | no       |         | Extract from this single entry only                              |
| `start_date` | string | no       |         | Filter entries from this date (ISO 8601)                         |
| `end_date`   | string | no       |         | Filter entries until this date (ISO 8601)                        |
| `stale_only` | bool   | no       | false   | Only process entries flagged stale since the last extraction run |

**Returns:**

```json
{
 "status": "succeeded",
 "job_id": "a3f9...",
 "result": {
  "processed": 42,
  "entities_created": 18,
  "entities_matched": 67,
  "mentions_created": 112,
  "relationships_created": 9,
  "warnings": []
 },
 "error_message": null
}
```

`status` is `succeeded`, `failed`, or `timeout`. On validation errors (unknown keys, wrong types) the tool returns
`{"status": "failed", "job_id": null, "result": null, "error_message": "..."}` without ever creating a job row.

### journal_backfill_mood_scores_batch

Submit a mood-score backfill job and block until it finishes. Same execution model as `journal_extract_entities_batch`.

| Parameter    | Type   | Required | Description                                                                                                 |
| ------------ | ------ | -------- | ----------------------------------------------------------------------------------------------------------- |
| `mode`       | string | yes      | `"stale-only"` (score only entries missing a current dimension) or `"force"` (rescore every entry in range) |
| `start_date` | string | no       | Restrict to entries from this date (ISO 8601)                                                               |
| `end_date`   | string | no       | Restrict to entries up to this date (ISO 8601)                                                              |

**Returns:**

```json
{
 "status": "succeeded",
 "job_id": "8e12...",
 "result": {
  "scored": 40,
  "skipped": 2,
  "errors": []
 },
 "error_message": null
}
```

Same failure semantics as `journal_extract_entities_batch` — a bad `mode` or an otherwise-invalid param shape returns a
`failed` dict without writing a job row.

### journal_get_job_status

Non-blocking lookup of a batch job by id. Useful for checking the state of a job submitted elsewhere (for example, one
the webapp started) from inside an MCP conversation.

| Parameter | Type   | Required | Description                                 |
| --------- | ------ | -------- | ------------------------------------------- |
| `job_id`  | string | yes      | The UUID returned by a batch-job submission |

**Returns:** the full serialised job dict —
`{id, type, status, params, progress_current, progress_total, result, error_message, created_at, started_at, finished_at}`.
If the job is not found, the returned dict is `{"error": "Job not found", "job_id": "..."}`.

## Fitness Tools

MCP twins for the fitness REST endpoints plus three correlation queries that
are MCP-only. Read tools mirror `GET /api/fitness/*` exactly so callers can
use either entry point interchangeably. The correlation queries are the
journal × fitness joins from
[`fitness-schema.md` §8](fitness-schema.md#8-correlation-queries-proves-schema-supports-them) — that doc is the source of truth for the queries.

### fitness_list_activities

List activities in a date window. Mirrors `GET /api/fitness/activities`.

| Parameter       | Type   | Required | Description                                                                              |
| --------------- | ------ | -------- | ---------------------------------------------------------------------------------------- |
| `start`         | string | yes      | Inclusive start date (`YYYY-MM-DD`).                                                     |
| `end`           | string | yes      | Inclusive end date (`YYYY-MM-DD`).                                                       |
| `activity_type` | string | no       | Filter by canonical activity type (`run`, `ride`, `swim`, `walk`, `hike`, `strength`, `other`). |

**Returns:** `{"items": [...]}` — same shape as the REST endpoint.

### fitness_list_daily

List daily wellness rollups in a date window. Mirrors `GET /api/fitness/daily`.

| Parameter | Type   | Required | Description                          |
| --------- | ------ | -------- | ------------------------------------ |
| `start`   | string | yes      | Inclusive start date (`YYYY-MM-DD`). |
| `end`     | string | yes      | Inclusive end date (`YYYY-MM-DD`).   |

**Returns:** `{"items": [...]}` — same shape as the REST endpoint.

### fitness_sync_status

Per-source auth + last-runs snapshot. No parameters. Mirrors
`GET /api/fitness/sync/status` exactly — each of `strava` / `garmin` is
either `null` (never connected) or a dict with `auth_status`,
`auth_broken_since`, `last_success_at`, and the last 10 sync runs.

### fitness_integrity_check

Run the soft-pointer integrity check. No parameters. Mirrors
`GET /api/fitness/integrity`. Returns `{"activities": [...], "daily": [...]}`
— empty arrays = clean.

### fitness_trigger_sync

Submit a fitness fetch + normalize job for the given source. Mirrors
`POST /api/fitness/sync/{source}` including the same dedup posture (returns
the existing job id with `already_running: true` instead of queueing a
duplicate).

| Parameter | Type   | Required | Description              |
| --------- | ------ | -------- | ------------------------ |
| `source`  | string | yes      | `"strava"` or `"garmin"` |

**Returns:** `{"job_id", "status", "already_running"?}` on success;
`{"error": "...", "job_id": null}` if the source isn't configured on this
server.

### fitness_correlate_sleep_mood

Daily-grain sleep score × mood (energy & joy). Q1 from
[`fitness-schema.md` §8](fitness-schema.md#8-correlation-queries-proves-schema-supports-them).

| Parameter | Type   | Required | Description                          |
| --------- | ------ | -------- | ------------------------------------ |
| `start`   | string | yes      | Inclusive start date (`YYYY-MM-DD`). |
| `end`     | string | yes      | Inclusive end date (`YYYY-MM-DD`).   |

**Returns:**
`{"rows": [{"local_date", "sleep_score", "sleep_efficiency_pct", "energy", "joy"}, ...]}`.
Days with sleep but no journal entry have `null` mood values.

### fitness_correlate_weekly_runs_stress

Weekly running distance × stress proxy (Monday-of-week buckets). Q2 from
the schema doc. The `stress_proxy` is the average `frustration` mood-dimension
score for entries in that week — closest dimension we have to "stress".

| Parameter | Type   | Required | Description                          |
| --------- | ------ | -------- | ------------------------------------ |
| `start`   | string | yes      | Inclusive start date (`YYYY-MM-DD`). |
| `end`     | string | yes      | Inclusive end date (`YYYY-MM-DD`).   |

**Returns:** `{"rows": [{"week_start", "distance_km", "stress_proxy"}, ...]}`.
`stress_proxy` is `null` for weeks where the user ran but didn't journal.

### fitness_correlate_hrv_mood

Rolling-window HRV × mood (joy & energy). Q3 from the schema doc.
Materialises a calendar-day series so the rolling window measures *days*,
not row-count days — missing days neither corrupt the rolling mean nor
shorten the window.

| Parameter | Type   | Required | Default | Description                                                |
| --------- | ------ | -------- | ------- | ---------------------------------------------------------- |
| `start`   | string | yes      |         | Inclusive start date (`YYYY-MM-DD`).                       |
| `end`     | string | yes      |         | Inclusive end date (`YYYY-MM-DD`).                         |
| `window`  | int    | no       | `7`     | Rolling-window size in calendar days (schema doc recommends 7 or 14). |

**Returns:** `{"rows": [{"d", "hrv_roll", "joy_roll", "energy_roll"}, ...]}`
— one row per calendar day in the window.

## Transport

- **Protocol**: Streamable HTTP (MCP spec 2025-03-26)
- **Default endpoint**: `http://localhost:8400/mcp`
- **Docker Compose**: `http://journal:8400/mcp` (internal service name)

### Direct HTTP Calls

MCP clients normally handle the session protocol automatically. If calling directly (e.g., via curl), the streamable HTTP
transport requires a session handshake:

1. **Initialize** — `POST /mcp` with the MCP `initialize` request. The response includes an `mcp-session-id` header.
2. **Call tools** — `POST /mcp` with headers:
   - `Content-Type: application/json`
   - `Accept: application/json, text/event-stream`
   - `Mcp-Session-Id: <id from step 1>`
