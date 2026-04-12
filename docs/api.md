# API Reference

## REST API Endpoints

The journal server exposes REST API endpoints alongside the MCP protocol, both on the same port. These endpoints are registered via `mcp.custom_route()` and served by the same Starlette/ASGI application.

CORS is configurable via the `API_CORS_ORIGINS` environment variable (see [configuration.md](configuration.md)).

### GET /api/entries

List entries with pagination and optional date filtering.

**Query parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `start_date` | string | no | | Filter from date (ISO 8601) |
| `end_date` | string | no | | Filter until date (ISO 8601) |
| `limit` | int | no | 20 | Max results (1-100) |
| `offset` | int | no | 0 | Pagination offset |

**Response (200):**
```json
{
  "items": [
    {
      "id": 1,
      "entry_date": "2026-04-09",
      "source_type": "image",
      "page_count": 2,
      "word_count": 450,
      "chunk_count": 5,
      "created_at": "2026-04-09T10:30:00"
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
  "source_type": "image",
  "raw_text": "original OCR output...",
  "final_text": "corrected text...",
  "page_count": 2,
  "word_count": 450,
  "chunk_count": 5,
  "language": "en",
  "created_at": "2026-04-09T10:30:00",
  "updated_at": "2026-04-09T11:00:00",
  "uncertain_spans": [
    { "char_start": 6, "char_end": 12 },
    { "char_start": 18, "char_end": 24 }
  ]
}
```

`uncertain_spans` is a list of half-open `[char_start, char_end)`
character ranges into `raw_text` — each pair covers one word or short
phrase the OCR model flagged as uncertain at ingestion time. Entries
ingested before migration `0005` return an empty array. The field is
always present; callers never need to check for its existence. See
[`ocr-context.md`](ocr-context.md) for the sentinel protocol and the
webapp's Review toggle for the consumer UI.

`uncertain_spans` is preserved across `PATCH /api/entries/{id}` —
edits change `final_text` but leave `raw_text` and its span list
untouched.

**Response (404):**
```json
{ "error": "not_found", "message": "Entry 999 not found" }
```

### PATCH /api/entries/{id}

Update an entry's `final_text`. Triggers re-chunking, re-embedding, and FTS5 rebuild.

**Request body:**
```json
{ "final_text": "corrected text..." }
```

**Response (200):** Updated entry detail (same shape as GET /api/entries/{id}).

**Response (400):**
```json
{ "error": "validation_error", "message": "final_text is required" }
```

**Response (404):**
```json
{ "error": "not_found", "message": "Entry 999 not found" }
```

### DELETE /api/entries/{id}

Delete an entry. Removes the SQLite row (cascading to pages, tags, people,
places, mood scores, and source files) and purges the entry's chunks from
the vector store.

**Response (200):**
```json
{ "deleted": true, "id": 1 }
```

**Response (404):**
```json
{ "error": "Entry 999 not found" }
```

### GET /api/entries/{id}/chunks

Return the persisted chunks for an entry, with each chunk's source
character range and token count. Used by the webapp overlay to draw
chunk boundaries on top of the entry text.

Chunks are persisted to SQLite (`entry_chunks` table) at ingestion
time, so this endpoint is a straight SELECT — the chunker is not
re-run. Chunks produced before migration 0003 are not automatically
populated; re-ingest the entry or run the backfill service to
populate them.

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

`char_start` / `char_end` are character offsets into the entry's
`final_text` (or `raw_text` as fallback). `char_end` is exclusive.
Slicing `final_text[char_start:char_end]` yields the source range the
chunk covers — which may include slightly more whitespace than `text`,
because paragraph and sentence separators are normalised when the
chunk was rendered.

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

Tokenise an entry's text on demand using tiktoken `cl100k_base` — the
encoding that matches `text-embedding-3-large`. Returns per-token
records including the token ID, the token text, and the character
range the token covers in `final_text`.

Computed per request — the call is cheap (< 10 ms for a ~2000-word
entry) and avoids any cache invalidation when the user edits the
entry's text.

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

For valid UTF-8 input the offsets slice `final_text` exactly —
concatenating `[final_text[t.char_start:t.char_end] for t in tokens]`
reconstructs the entry text. Note that leading whitespace is part of
the token (e.g. `" world"` above), which matches how the embedding
model sees the text.

**Response (404):**
```json
{
  "error": "entry_not_found",
  "message": "Entry 999 not found"
}
```

### GET /health

Operational health endpoint. **Unauthenticated** — the server
binds to loopback only (see `docs/security.md`), so any caller
that can reach `/health` already has a shell on the box. The
field that would worry us most — most-frequent search terms — is
deliberately **not** exposed. The query stats block carries
counts-by-type only, not query strings.

If you ever front this server with a reverse proxy, exclude
`/health` from the public route or scrub the `queries.by_type`
block before serving it outside loopback.

**Response (200):**

```json
{
  "status": "ok",
  "checks": [
    {"name": "sqlite",    "status": "ok",       "detail": "SELECT 1 succeeded",                              "error": null},
    {"name": "chromadb",  "status": "ok",       "detail": "collection count = 0",                            "error": null},
    {"name": "anthropic", "status": "ok",       "detail": "anthropic API key is configured (51 chars)",      "error": null},
    {"name": "openai",    "status": "degraded", "detail": "openai API key is not configured",                "error": null}
  ],
  "ingestion": {
    "total_entries": 42,
    "entries_last_7d": 3,
    "entries_last_30d": 12,
    "by_source_type": {"ocr": 30, "voice": 12},
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
      "semantic_search": {"count": 12, "latency": {"p50_ms": 41.2, "p95_ms": 89.7, "p99_ms": 102.3}},
      "keyword_search":  {"count":  5, "latency": {"p50_ms":  4.1, "p95_ms":  9.8, "p99_ms":  12.1}}
    }
  }
}
```

**Status semantics:**

- `"status": "ok"` — every component check returned `ok`.
- `"status": "degraded"` — at least one component is degraded
  (missing API key, short API key). The server is still serving
  requests. The endpoint still returns HTTP 200 so a probe can
  distinguish "config is wrong" from "container is not listening".
- `"status": "error"` — at least one component check failed
  outright (e.g. SQLite unreachable, ChromaDB connection refused).
  Still HTTP 200; callers should inspect the `status` field
  rather than relying on status codes.

**Component checks:**

| Component   | What it checks                                                           |
|-------------|--------------------------------------------------------------------------|
| `sqlite`    | `SELECT 1` against the connection                                        |
| `chromadb`  | `collection.count()` against the vector store                            |
| `anthropic` | API key is set and has a plausible length — **no LLM call is made**      |
| `openai`    | API key is set and has a plausible length — **no API call is made**     |

**Privacy:** The payload intentionally omits any field that would
surface query *content* — only counts and latency percentiles per
query type. Adding a "most frequent search terms" field was
proposed and rejected per the Tier 1 plan open question on
privacy.

**CLI equivalent:** `uv run journal health` prints the same
payload as pretty JSON. Add `--compact` for single-line output
suitable for piping to `jq`. The CLI exits non-zero only when
the rolled-up `status` is `error`.

---

### GET /api/search

Full-text search across journal entries. Supports two modes:

- **`semantic`** (default): vector similarity over persisted chunk
  embeddings. Results are ranked by cosine similarity and each matching
  chunk carries its character offsets into the parent entry's
  `final_text` so a client can render in-place highlights without a
  second round-trip.
- **`keyword`**: SQLite FTS5 over `final_text`. Ranked by FTS5's `rank`
  score. Each hit includes a `snippet` string with ASCII `\x02`
  (start) and `\x03` (end) control characters wrapping the matched
  terms — the client replaces these with whatever highlight markup it
  wants (for example `<mark>`).

**Query parameters:**

| Parameter    | Type   | Required | Default    | Description |
|--------------|--------|----------|------------|-------------|
| `q`          | string | yes      |            | Search query |
| `mode`       | string | no       | `semantic` | `semantic` or `keyword` |
| `start_date` | string | no       |            | Filter from date (ISO 8601) |
| `end_date`   | string | no       |            | Filter until date (ISO 8601) |
| `limit`      | int    | no       | 10         | Max entries returned (1–50) |
| `offset`     | int    | no       | 0          | Pagination offset |

**Response (200):**
```json
{
  "query": "vienna with atlas",
  "mode": "semantic",
  "limit": 10,
  "offset": 0,
  "items": [
    {
      "entry_id": 42,
      "entry_date": "2026-03-22",
      "text": "Walked through Vienna with Atlas. Later we met Robyn...",
      "score": 0.871,
      "snippet": null,
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

**Mode differences:**

- In `semantic` mode, `snippet` is always `null` and `matching_chunks`
  is populated with one entry per chunk that matched, sorted by score
  descending. Each chunk has `char_start`/`char_end`/`chunk_index`
  when the entry has persisted chunks in SQLite (entries ingested
  before chunk persistence return `null` offsets).
- In `keyword` mode, `matching_chunks` is an empty list and `snippet`
  is a string like `"...walked through \x02Vienna\x03 with Atlas..."`.
  The `score` field carries a small positive float derived from FTS5's
  `rank` so rows sort by relevance; it is not comparable across modes.

**Error responses:**

- `400` — `q` missing or empty, or `mode` not one of `semantic` /
  `keyword`
- `503` — server not initialised

---

### GET /api/dashboard/mood-dimensions

Return the currently-loaded mood-scoring dimensions. Used by
the webapp's mood chart to discover the active facet set, their
scale types, and score ranges without hardcoding anything in
the frontend.

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
  ]
}
```

When mood scoring is disabled (`JOURNAL_ENABLE_MOOD_SCORING`
unset or false) the endpoint returns 200 with an empty
`dimensions` array. Callers should treat that as "no mood data
to display" rather than an error.

See `docs/mood-scoring.md` for the full rationale, facet
definitions, bipolar vs unipolar semantics, and the rebuild
procedure.

---

### GET /api/dashboard/mood-trends

Aggregate mood scores per time bucket, grouped by dimension.
Used by the dashboard's mood chart.

**Query parameters:**

| Parameter   | Type   | Required | Default | Description                                   |
|-------------|--------|----------|---------|-----------------------------------------------|
| `bin`       | string | no       | `week`  | `week`, `month`, `quarter`, or `year`         |
| `from`      | string | no       |         | Inclusive ISO-8601 start date                 |
| `to`        | string | no       |         | Inclusive ISO-8601 end date                   |
| `dimension` | string | no       |         | Filter to a single dimension by `name`        |

**Response (200):**

```json
{
  "from": "2026-01-01",
  "to": "2026-04-11",
  "bin": "week",
  "bins": [
    {"period": "2026-01-05", "dimension": "joy_sadness", "avg_score": 0.3, "entry_count": 4},
    {"period": "2026-01-05", "dimension": "agency",      "avg_score": 0.6, "entry_count": 4},
    {"period": "2026-01-12", "dimension": "joy_sadness", "avg_score": 0.5, "entry_count": 5}
  ]
}
```

- `period` is the canonical ISO-8601 date of the bucket start
  (Monday for weeks, first of month/quarter/year for the others).
- `avg_score` is the mean across every scored entry in the bucket.
- Empty buckets are omitted.

**Error responses:**

- `400` — `bin` is not one of `week`/`month`/`quarter`/`year`
- `503` — server not initialised

---

### GET /api/dashboard/writing-stats

Aggregated writing frequency and word count per time bucket,
used by the webapp's `/dashboard` view. Bearer-authenticated via
the app-wide middleware.

**Query parameters:**

| Parameter | Type   | Required | Default | Description                           |
|-----------|--------|----------|---------|---------------------------------------|
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
    {"bin_start": "2026-01-01", "entry_count": 5,  "total_words": 980},
    {"bin_start": "2026-02-01", "entry_count": 3,  "total_words": 612},
    {"bin_start": "2026-03-01", "entry_count": 12, "total_words": 2240},
    {"bin_start": "2026-04-01", "entry_count": 2,  "total_words": 380}
  ]
}
```

**Bucket semantics:**

- `week` bins start on the Monday of the ISO week (Sunday rolls
  into the preceding Monday).
- `month` bins start on the 1st of the month.
- `quarter` bins start on the 1st of Jan/Apr/Jul/Oct.
- `year` bins start on January 1st.
- **Empty buckets are omitted.** A month with zero entries does
  not appear in the `bins` array. Clients rendering a dense line
  chart should fill gaps on the client side.

**Error responses:**

- `400` — `bin` is not one of `week`/`month`/`quarter`/`year`
- `503` — server not initialised

---

---

## Entry creation endpoints

Three endpoints for creating journal entries from the webapp. Text and file
ingestion are synchronous; image ingestion is asynchronous (returns a job ID).

### POST /api/entries/ingest/text

Create a journal entry from plain text (no OCR). Synchronous.

**Request body (JSON):**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `text` | string | yes | | Entry content |
| `entry_date` | string | no | today | ISO 8601 date |
| `source_type` | string | no | `"manual"` | Source type label |

**Response (201):**
```json
{
  "entry": { "id": 1, "entry_date": "2026-04-12", "source_type": "manual", "..." : "..." },
  "mood_job_id": "uuid-or-null"
}
```

`mood_job_id` is non-null when `JOURNAL_ENABLE_MOOD_SCORING=true`. Poll
`GET /api/jobs/{mood_job_id}` to check scoring completion.

**Errors:** 400 (missing/empty text, invalid JSON).

---

### POST /api/entries/ingest/file

Create a journal entry from an uploaded `.md` or `.txt` file. Synchronous.

**Request body (multipart/form-data):**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `file` | file | yes | | A single `.md` or `.txt` file |
| `entry_date` | string | no | today | ISO 8601 date |

**Response (201):** Same shape as `POST /api/entries/ingest/text`.

The file content is stored as both `raw_text` and `final_text`. A
`source_files` record is created with the original filename and SHA256
hash for duplicate detection.

**Errors:** 400 (wrong extension, empty file, UTF-8 decode error, no file).

---

### POST /api/entries/ingest/images

Upload one or more journal page images for OCR. Asynchronous — returns a
job ID immediately. The job runs OCR on each page, combines the text into
a single entry, chunks, embeds, and stores.

**Request body (multipart/form-data):**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `images` | file(s) | yes | | One or more image files (JPEG, PNG, GIF, WebP) |
| `entry_date` | string | no | today | ISO 8601 date |

**Limits:** 10 MB per file, 50 MB total.

**Response (202):**
```json
{
  "job_id": "uuid",
  "status": "queued"
}
```

Poll `GET /api/jobs/{job_id}` for progress. On success, `result.entry_id`
contains the new entry's ID.

**Errors:** 400 (no images, unsupported type), 413 (total size exceeded).

---

---

## Entity endpoints

Endpoints that expose the extracted-entity graph built by the entity
extraction pipeline. See [entity-extraction.md](entity-extraction.md)
for how entities, mentions, and relationships are produced, and
[jobs.md](jobs.md) for how the extraction runs are scheduled.

### GET /api/entities

List entities with optional type filter, case-insensitive substring
search, and pagination. The search filter is applied after the store
query, so `total` reflects the unfiltered count for the given
`entity_type`.

**Query parameters:**

| Parameter | Type   | Required | Default | Description                                                          |
|-----------|--------|----------|---------|----------------------------------------------------------------------|
| `type`    | string | no       |         | Filter by entity type (e.g. `person`, `place`, `activity`)           |
| `search`  | string | no       |         | Case-insensitive substring match against canonical name and aliases  |
| `limit`   | int    | no       | 50      | Max results (capped at 200)                                          |
| `offset`  | int    | no       | 0       | Pagination offset                                                    |

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

List every recorded mention of an entity, oldest first. Each mention
carries the entry it was extracted from, the exact quoted span, and
the extractor's confidence.

**Query parameters:**

| Parameter | Type | Required | Default | Description                   |
|-----------|------|----------|---------|-------------------------------|
| `limit`   | int  | no       | 50      | Max mentions (capped at 200)  |
| `offset`  | int  | no       | 0       | Pagination offset             |

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

`total` is the length of the returned page, not the unpaginated
count — clients that need a hard total should rely on the entity
summary's `mention_count` instead.

**Response (404):**
```json
{ "error": "Entity 999 not found" }
```

### GET /api/entities/{entity_id}/relationships

Return the subject-predicate-object relationships that touch an
entity, split into outgoing (entity is the subject) and incoming
(entity is the object) lists.

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

List the entities extracted from a single entry, with per-entity
mention counts scoped to that entry.

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

---

## Batch job endpoints

Long-running batch operations (entity extraction, mood backfill) run
asynchronously on an in-process single-worker job runner. Clients
submit a job, receive `202 Accepted` with a `job_id`, and then poll
`GET /api/jobs/{job_id}` once per second until `status` reaches a
terminal value (`succeeded` or `failed`).

See [jobs.md](jobs.md) for the full data model, threading
invariants, restart recovery semantics, and result payload shapes.

### POST /api/entities/extract

Submit an entity-extraction batch job. This endpoint replaced the
previously synchronous entity extraction call — the single-entry path
(`entry_id`) also goes through the jobs table, so there is no
synchronous alternative. Unknown keys, wrong types, and invalid
values are rejected synchronously by `JobRunner.submit_entity_extraction`
before any row is written.

**Request body:**
```json
{
  "entry_id": 42,
  "start_date": "2026-03-01",
  "end_date": "2026-03-31",
  "stale_only": true
}
```

All four fields are optional. When `entry_id` is present the runner
calls `extract_from_entry` and returns a one-result batch; otherwise
it runs `extract_batch` with the date and staleness filters.

**Response (202):**
```json
{ "job_id": "a3f9...", "status": "queued" }
```

**Response (400):**
```json
{ "error": "Unknown parameter: foo" }
```

Returned when the body is not a JSON object, when it contains
unknown keys, or when a field has the wrong type.

**Response (503):**
```json
{ "error": "Server not initialized" }
```

Poll `GET /api/jobs/{job_id}` for progress and the final result. See
[jobs.md](jobs.md) for the full result-payload shape.

### POST /api/mood/backfill

Submit a mood-score backfill batch job. `mode` is required and
selects between idempotent rescoring of stale entries and a full
rescore. `prune_retired` and `dry_run` are intentionally not surfaced
here — use the CLI for those.

**Request body:**
```json
{
  "mode": "stale-only",
  "start_date": "2026-03-01",
  "end_date": "2026-03-31"
}
```

| Field        | Type   | Required | Description                                                |
|--------------|--------|----------|------------------------------------------------------------|
| `mode`       | string | yes      | `"stale-only"` or `"force"`                                |
| `start_date` | string | no       | Inclusive ISO-8601 start date                              |
| `end_date`   | string | no       | Inclusive ISO-8601 end date                                |

`stale-only` rescores only entries missing at least one currently-loaded
mood dimension; `force` rescores every entry in the date window.

**Response (202):**
```json
{ "job_id": "8e12...", "status": "queued" }
```

**Response (400):**
```json
{ "error": "mode must be 'stale-only' or 'force'" }
```

Returned when the body is not a JSON object, `mode` is missing or
invalid, or an unknown key is present.

**Response (503):**
```json
{ "error": "Server not initialized" }
```

Poll `GET /api/jobs/{job_id}` for progress and the final result. See
[jobs.md](jobs.md) for the full result-payload shape.

### GET /api/jobs/{job_id}

Return the full serialised state of a batch job. Clients should poll
this endpoint once per second until `status` is `succeeded` or
`failed`. Every field is always present in the response — absent
values are `null` rather than missing keys — so clients can rely on
a fixed schema.

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
- `result` is populated on success (shape depends on `type` — see
  [jobs.md](jobs.md)).
- `error_message` is populated when `status = failed`, including the
  sentinel `"server restarted before job completed"` for jobs
  reconciled on startup after an unclean shutdown.

**Response (404):**
```json
{ "error": "Job not found" }
```

---

### GET /api/stats

Journal statistics with optional date filtering.

**Query parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `start_date` | string | no | Filter from date (ISO 8601) |
| `end_date` | string | no | Filter until date (ISO 8601) |

**Response (200):**
```json
{
  "total_entries": 42,
  "date_range_start": "2025-01-15",
  "date_range_end": "2026-04-09",
  "total_words": 18500,
  "avg_words_per_entry": 440,
  "entries_per_month": {
    "2026-03": 8,
    "2026-04": 3
  }
}
```

---

# MCP Tool Reference

The journal MCP server exposes its tools via streamable HTTP transport.

## Query Tools

### journal_search_entries

Semantic similarity search across journal entries. The query is converted to a vector by the embedding model (not an LLM) and matched against stored entry vectors by cosine distance. Results are ranked by similarity score. No model reads or interprets the entries — you get raw text back, ranked by how close the meaning is to your query.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | yes | | Natural language query |
| `start_date` | string | no | | Filter from date (ISO 8601) |
| `end_date` | string | no | | Filter until date (ISO 8601) |
| `limit` | int | no | 10 | Max results (1-50) |
| `offset` | int | no | 0 | Pagination offset |

### journal_get_entries_by_date

Get all entries for a specific date.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `date` | string | yes | Date in ISO 8601 format |

### journal_list_entries

List entries in reverse chronological order.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `start_date` | string | no | | Filter from date |
| `end_date` | string | no | | Filter until date |
| `limit` | int | no | 20 | Max results (1-50) |
| `offset` | int | no | 0 | Pagination offset |

## Statistics Tools

### journal_get_statistics

Get journal statistics: entry count, frequency, word counts, date range.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `start_date` | string | no | all time | Start of period |
| `end_date` | string | no | today | End of period |

### journal_get_mood_trends

Analyze mood trends over time.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `start_date` | string | no | | Start of period |
| `end_date` | string | no | | End of period |
| `granularity` | string | no | "week" | "day", "week", or "month" |

### journal_get_topic_frequency

Count how often a topic, person, or place appears.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `topic` | string | yes | Topic to search for |
| `start_date` | string | no | Start of period |
| `end_date` | string | no | End of period |

## Ingestion Tools

### journal_ingest_from_url

Ingest a **single** journal page image or voice note by downloading it from a URL. This is
the preferred ingestion method for MCP clients like Nanoclaw, since it avoids base64-encoding
large files as tool parameters.

| Parameter     | Type   | Required | Default | Description                                       |
|---------------|--------|----------|---------|---------------------------------------------------|
| `source_type` | string | yes      |         | "image" or "voice"                                |
| `url`         | string | yes      |         | URL to download the file from                     |
| `media_type`  | string | no       |         | MIME type override (inferred from response header) |
| `date`        | string | no       | today   | Entry date (ISO 8601)                             |
| `language`    | string | no       | "en"    | Language for voice transcription                  |

**Slack file URLs** (`files.slack.com`) are automatically authenticated using the
`SLACK_BOT_TOKEN` environment variable. No auth headers needed in the tool call — just
pass the raw `url_private` or `url_private_download` URL from Slack.

For other URLs, the server makes a plain HTTP GET with no authentication. The URL must be
accessible from the journal server's network.

> **Multi-page entries:** If a single journal entry spans multiple photos, do NOT call this
> tool once per page — each call creates a separate entry. Use
> [`journal_ingest_multi_page_from_url`](#journal_ingest_multi_page_from_url) instead.

### journal_ingest_multi_page_from_url

Ingest multiple page images (by URL) as a **single** multi-page journal entry. All images
are downloaded, OCR'd page-by-page, and combined into one entry with one page record per
image. This is the preferred way to ingest multi-page entries from URL-based clients (e.g.
Slack-driven agents).

| Parameter     | Type         | Required | Default | Description                                                |
|---------------|--------------|----------|---------|------------------------------------------------------------|
| `urls`        | list[string] | yes      |         | Ordered list of page image URLs, one per page              |
| `media_types` | list[string] | no       |         | Per-URL MIME type overrides (same length as `urls`)        |
| `date`        | string       | no       | today   | Entry date (ISO 8601)                                      |

Slack file URLs are authenticated the same way as in `journal_ingest_from_url`. If a page
within the batch matches an already-ingested file hash, ingestion fails with an
"already ingested" error before any entry is created.

### journal_ingest_entry

Ingest a journal entry from base64-encoded data. Use `journal_ingest_from_url` instead when
the file is available at a URL — this avoids MCP tool parameter size limits.

| Parameter     | Type   | Required | Default | Description                          |
|---------------|--------|----------|---------|--------------------------------------|
| `source_type` | string | yes      |         | "image" or "voice"                   |
| `data_base64` | string | yes      |         | Base64-encoded file data             |
| `media_type`  | string | yes      |         | MIME type (e.g. "image/jpeg")        |
| `date`        | string | no       | today   | Entry date (ISO 8601)                |
| `language`    | string | no       | "en"    | Language for voice transcription     |

### journal_ingest_multi_page

Ingest multiple images as pages of a single journal entry from base64-encoded data. Images
are OCR'd individually and combined into one entry. Prefer
`journal_ingest_multi_page_from_url` when the images are available at URLs.

| Parameter       | Type         | Required | Default | Description                                   |
|-----------------|--------------|----------|---------|-----------------------------------------------|
| `images_base64` | list[string] | yes      |         | Base64-encoded page images (ordered)          |
| `media_types`   | list[string] | yes      |         | Per-image MIME types (same length as images)  |
| `date`          | string       | no       | today   | Entry date (ISO 8601)                         |

### journal_update_entry_text

Update an entry's `final_text` to correct OCR errors. Triggers re-chunking, re-embedding, and FTS5 rebuild. The original `raw_text` is preserved.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `entry_id` | int | yes | Entry ID to update |
| `final_text` | string | yes | Corrected text |

## Entity Tools

Tools that read and write the extracted-entity graph. Entity
extraction itself is exposed as both a legacy synchronous tool
(`journal_extract_entities`) and an async batch wrapper
(`journal_extract_entities_batch`, documented under
[Batch Job Tools](#batch-job-tools)).

### journal_extract_entities

Run the entity extraction batch job over one or more entries
synchronously. **Legacy** — this tool blocks the MCP call for the
full duration of extraction and does not produce progress events.
New code should use `journal_extract_entities_batch` instead, which
routes through the shared JobRunner and matches the semantics used
by the webapp.

| Parameter    | Type | Required | Default | Description                                                           |
|--------------|------|----------|---------|-----------------------------------------------------------------------|
| `entry_id`   | int  | no       |         | Extract from this single entry only                                   |
| `start_date` | string | no     |         | Filter entries from this date (ISO 8601)                              |
| `end_date`   | string | no     |         | Filter entries until this date (ISO 8601)                             |
| `stale_only` | bool | no       | false   | Only process entries flagged stale since the last extraction run      |

Returns a human-readable summary string with aggregated counts
(`entities_created`, `entities_matched`, `mentions_created`,
`relationships_created`) and any warnings emitted by the extractor.

### journal_list_entities

List extracted entities, optionally filtered by type.

| Parameter     | Type   | Required | Default | Description                                                                    |
|---------------|--------|----------|---------|--------------------------------------------------------------------------------|
| `entity_type` | string | no       |         | One of `person`, `place`, `activity`, `organization`, `topic`, `other`         |
| `limit`       | int    | no       | 50      | Max results (capped at 200)                                                    |

Returns a string listing each entity as
`[id] type: canonical_name — N mentions (aliases: ...)`.

### journal_get_entity_mentions

Return every recorded mention of a specific entity across the
journal.

| Parameter   | Type | Required | Default | Description                  |
|-------------|------|----------|---------|------------------------------|
| `entity_id` | int  | yes      |         | The entity to look up        |
| `limit`     | int  | no       | 50      | Max mentions to return       |

Returns a string with one mention per line —
`entry N: "quoted span" (confidence X.XX)`. Returns
`Entity {id} not found.` if the entity does not exist.

### journal_get_entity_relationships

Return the outgoing and incoming relationships that touch an entity.
Outgoing edges are triples where the entity is the subject;
incoming edges are triples where it is the object.

| Parameter   | Type | Required | Description              |
|-------------|------|----------|--------------------------|
| `entity_id` | int  | yes      | The entity whose edges to return |

Returns a string grouped into outgoing and incoming sections. Each
edge renders as `-> predicate -> other_entity (entry N, conf X.XX)`
(or the reverse for incoming). Returns `Entity {id} not found.` or
`No relationships recorded for {name}.` when appropriate.

## Batch Job Tools

Async batch-job wrappers around the same `JobRunner` that backs the
REST endpoints. The two `_batch` tools **block** the MCP tool call
until the job reaches a terminal state — they poll the jobs table
every 500 ms with a default timeout of 3600 s. Failed jobs return a
structured dict rather than raising, so the caller can read the
error message and respond to the user.

See [jobs.md](jobs.md) for the full data model, result payload
shapes, and restart-recovery semantics.

### journal_extract_entities_batch

Submit an entity-extraction job and block until it finishes. Same
parameter shape as the legacy synchronous `journal_extract_entities`
tool, but routed through the shared `JobRunner` so progress is
persisted in the jobs table and the result matches what the webapp
consumes.

| Parameter    | Type   | Required | Default | Description                                                      |
|--------------|--------|----------|---------|------------------------------------------------------------------|
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

`status` is `succeeded`, `failed`, or `timeout`. On validation
errors (unknown keys, wrong types) the tool returns
`{"status": "failed", "job_id": null, "result": null, "error_message": "..."}`
without ever creating a job row.

### journal_backfill_mood_scores_batch

Submit a mood-score backfill job and block until it finishes. Same
execution model as `journal_extract_entities_batch`.

| Parameter    | Type   | Required | Description                                                                 |
|--------------|--------|----------|-----------------------------------------------------------------------------|
| `mode`       | string | yes      | `"stale-only"` (score only entries missing a current dimension) or `"force"` (rescore every entry in range) |
| `start_date` | string | no       | Restrict to entries from this date (ISO 8601)                               |
| `end_date`   | string | no       | Restrict to entries up to this date (ISO 8601)                              |

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

Same failure semantics as `journal_extract_entities_batch` — a bad
`mode` or an otherwise-invalid param shape returns a `failed` dict
without writing a job row.

### journal_get_job_status

Non-blocking lookup of a batch job by id. Useful for checking the
state of a job submitted elsewhere (for example, one the webapp
started) from inside an MCP conversation.

| Parameter | Type   | Required | Description                                  |
|-----------|--------|----------|----------------------------------------------|
| `job_id`  | string | yes      | The UUID returned by a batch-job submission  |

**Returns:** the full serialised job dict —
`{id, type, status, params, progress_current, progress_total, result, error_message, created_at, started_at, finished_at}`.
If the job is not found, the returned dict is
`{"error": "Job not found", "job_id": "..."}`.

## Transport

- **Protocol**: Streamable HTTP (MCP spec 2025-03-26)
- **Default endpoint**: `http://localhost:8400/mcp`
- **Docker Compose**: `http://journal:8400/mcp` (internal service name)

### Direct HTTP Calls

MCP clients normally handle the session protocol automatically. If calling directly (e.g.,
via curl), the streamable HTTP transport requires a session handshake:

1. **Initialize** — `POST /mcp` with the MCP `initialize` request. The response includes an
   `mcp-session-id` header.
2. **Call tools** — `POST /mcp` with headers:
   - `Content-Type: application/json`
   - `Accept: application/json, text/event-stream`
   - `Mcp-Session-Id: <id from step 1>`
