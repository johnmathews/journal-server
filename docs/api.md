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
  "updated_at": "2026-04-09T11:00:00"
}
```

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

The journal MCP server exposes 10 tools via streamable HTTP transport.

## Query Tools

### journal_search_entries

Semantic similarity search across journal entries.

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

Ingest a journal entry by downloading an image or voice note from a URL. This is the
preferred ingestion method for MCP clients like Nanoclaw, since it avoids base64-encoding
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

Ingest multiple images as pages of a single journal entry, or add pages to an existing entry. Images are OCR'd individually and combined into one entry.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `urls` | list[string] | yes | | URLs of page images (ordered) |
| `date` | string | no | today | Entry date (ISO 8601) |
| `entry_id` | int | no | | Existing entry ID to add pages to |

### journal_update_entry_text

Update an entry's `final_text` to correct OCR errors. Triggers re-chunking, re-embedding, and FTS5 rebuild. The original `raw_text` is preserved.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `entry_id` | int | yes | Entry ID to update |
| `final_text` | string | yes | Corrected text |

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
