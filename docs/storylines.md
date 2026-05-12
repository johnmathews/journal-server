# Storylines

**Status:** active reference.

A storyline is a synthesized cross-entry narrative anchored on a single entity. Two parallel panels are rendered for each storyline:

* **Curation panel** — chronologically-ordered verbatim excerpts from journal entries that mention the storyline's anchor entity, separated by minimal Haiku-generated transition prose ("Three days later:").
* **Narrative panel** — a flowing third-person prose narrative grounded via the Anthropic Citations API. Pointers from narrative text back to source entries are parsed by Anthropic from custom-content documents, so they cannot be fabricated.

This document describes how the feature works in code. The design plan and tradeoffs live in [`storylines-plan.md`](./storylines-plan.md).

## Data model

Migration `0027_storylines.sql`. Two tables:

* `storylines` — one row per storyline. Key columns:
  * `(user_id, entity_id, name)` is UNIQUE (a user can't have two storylines about the same entity with the same name).
  * `start_date` / `end_date` — optional ISO date bounds; when null, the service uses the last `STORYLINE_DEFAULT_WINDOW_DAYS` days.
  * `summary_embedding_json` — JSON-encoded embedding of the narrative panel text, cached for the future extension classifier embedding-similarity stage.
  * `last_generated_at`, `last_extension_check_at` — observability timestamps.
* `storyline_panels` — one row per `(storyline_id, panel_kind)`. Panels are split so the curation pass doesn't rewrite the narrative when only the glue is iterated.

The `segments_json` column on `storyline_panels` is a list of dicts in one of two shapes (see `services/storylines/segments.py`):

```python
{"kind": "text",     "text": "..."}
{"kind": "citation", "entry_id": 42, "quote": "..."}
```

The webapp renders text runs as plain text and citations as `<RouterLink :to="/entries/${entry_id}">` links. No markdown is involved on the wire.

## Generation pipeline

`services/storylines/service.StorylineGenerationService.regenerate(storyline_id)`:

1. Resolve the storyline; resolve the (start_date, end_date) window (storyline-specific bounds or the default 90-day window).
2. Fetch dated entity excerpts via `SQLiteEntityStore.get_dated_entity_excerpts(entity_id, user_id, start_date, end_date)`. This joins `entity_mentions` + `entries`, sorts by `entries.entry_date ASC`, and aggregates the verbatim quotes per entry.
3. If fewer than `STORYLINE_FTS_FALLBACK_THRESHOLD` excerpts are returned, run **FTS5 fallback**: search journal entries for the entity's canonical name in the same date window, deduplicate against the entity-mention set, attach a context snippet (±120 chars around the surface form) as the "quote". The fallback catches pronominal mentions ("my son" → Atlas) and gaps from entries ingested before auto-reextraction shipped.
4. Build the narrator's input: one `source="text"` document per excerpt. Each document's `data` is the entry's `final_text`; the entry id and date live in the document's `title` (`Entry N (YYYY-MM-DD)`), which the model can see but cannot cite from. Citations enabled. The Anthropic API auto-chunks each document at sentence boundaries.
5. Call the narrator (`providers/storyline_narrator.AnthropicStorylineNarrator`). System prompt restricts to provided documents, forbids invention, permits "I don't know". Cache control breakpoints: 1h TTL on the system prompt, 5m TTL on the document corpus (`cache_control` attaches to the last document only — a single breakpoint covering every preceding document, well under the four-breakpoint request limit).
6. Parse the response. Each text block with attached citations becomes a `text` segment followed by one `citation` segment per cited source. Citations carry the `char_location` shape; we map `document_index` back to `entry_id` via the index → entry map we built in step 4, and use `cited_text` (a sentence-level excerpt) as the citation's `quote`.
7. Call the glue (`providers/storyline_glue.AnthropicStorylineGlue`). One batched request returns N-1 transition phrases as a JSON array. On API failure or malformed response, fall back to deterministic gap-bucketed phrases (`"Two weeks later:"`).
8. Build the curation panel by interleaving verbatim quotes (or FTS snippets) with transitions.
9. Persist both panels via `SQLiteStorylineRepository.upsert_panel`.
10. If an embedder is wired, embed the narrative text and store it on `storylines.summary_embedding_json`.
11. Record `last_generated_at`.

## Extension classifier

`services/storylines/extension.StorylineExtensionClassifier.classify_for_entry(entry_id, user_id)` iterates the user's active storylines and returns one `ExtensionResult` per storyline. Pipeline per storyline:

1. **Entity overlap** (deterministic). If the storyline's anchor `entity_id` appears in the entry's extracted mentions, return `yes` immediately. Zero LLM calls.
2. **Surface form + LLM decider**. If the storyline's anchor entity's `canonical_name` is in the entry text (case-insensitive), call Haiku via `providers/storyline_extension_decider.AnthropicStorylineExtensionDecider` with a `record_decision` tool. Returns `yes`/`no`/`maybe` with one-sentence reasoning. On API failure or malformed response, the decider returns `maybe` so the entry surfaces for manual review.
3. **No match**. Neither signal fires — definite `no`, no LLM call.

The classifier records `last_extension_check_at` on every storyline it inspects, not just the matches.

## Ingestion hook

`JobRunner._queue_post_ingestion_jobs`, called from the text/image/audio ingest paths, queues a `storyline_extension_check` job alongside the existing `mood_score_entry` + `entity_extraction` jobs — but only when the classifier service is wired AND the entry has a known `user_id` (storylines are user-scoped).

The `run_storyline_extension_check` worker:

* Calls the classifier
* For each `yes` decision, queues a `storyline_generation` job via `JobRunner.submit_storyline_generation`
* Records the classifications (including reasoning) on the job's result blob
* Notifies only on failure (per-ingestion success notifications would be noisy)

## REST API

Read-side (`api/storylines.py`):

* `GET /api/storylines` — paginated list (standard `{items, total, limit, offset}` envelope), filterable by `status`
* `GET /api/storylines/{id}` — storyline + both panels as `{panels: {curation: {...}, narrative: {...}}}`

Write-side (`api/ingestion.py`):

* `POST /api/storylines` — body `{entity_id, name, description?, start_date?, end_date?}`. 201 on success, 409 if `(user, entity, name)` already exists, 400 on bad input.
* `POST /api/storylines/{id}/regenerate` — queues a `storyline_generation` job. 202 with `{"job_id"}`; clients poll `GET /api/jobs/{id}`.
* `DELETE /api/storylines/{id}` — removes the storyline (CASCADE drops its panels).

All routes return 503 when the storylines feature is not configured on this server (missing `ANTHROPIC_API_KEY`).

## MCP tools

In `mcp_server/tools/storylines.py`:

* `journal_list_storylines` — text-formatted list
* `journal_get_storyline` — detail view with both panels printed inline
* `journal_create_storyline` — seed a storyline; refuses if `(user, entity, name)` already exists
* `journal_regenerate_storyline` — queues a job and polls until terminal (default 120s)

Each tool returns an actionable string when the storylines feature isn't configured. MCP clients (Nanoclaw, Claude Code, etc.) can use these tools to seed and read storylines without a webapp.

## Configuration

All env vars are optional; defaults make the feature work out of the box once `ANTHROPIC_API_KEY` is set.

| Env var                                  | Default               | Purpose                                              |
| ---------------------------------------- | --------------------- | ---------------------------------------------------- |
| `ANTHROPIC_API_KEY`                      | (none)                | Gates the entire feature on/off                      |
| `STORYLINE_NARRATOR_MODEL`               | `claude-opus-4-7`     | Model for the narrative panel                        |
| `STORYLINE_NARRATOR_MAX_TOKENS`          | `4096`                | Max output tokens for narrative                      |
| `STORYLINE_GLUE_MODEL`                   | `claude-haiku-4-5`    | Model for curation transitions                       |
| `STORYLINE_EXTENSION_DECIDER_MODEL`      | `claude-haiku-4-5`    | Model for the extension classifier's decider stage   |
| `STORYLINE_DEFAULT_WINDOW_DAYS`          | `90`                  | Default window when storyline has no explicit bounds |
| `STORYLINE_FTS_FALLBACK_THRESHOLD`       | `3`                   | Below this many entity mentions, FTS fallback fires  |

## Providers

* `AnthropicStorylineNarrator` — Citations API with one `source="text"` document per entry; two-breakpoint caching (1h system, 5m corpus). Tested via canned response fakes; the parser handles missing or unknown `document_index`, plain text blocks without citations, and tool_use blocks (ignored).
* `AnthropicStorylineGlue` — Haiku batched call; the response parser accepts plain JSON, fenced-code-block JSON, and JSON embedded in prose. Deterministic fallback on failure.
* `AnthropicStorylineExtensionDecider` — Haiku tool-use (`record_decision` tool). `maybe` fallback on any non-happy path.

## Testing

* `tests/test_storyline_repository.py` — CRUD, panel upsert, dated mentions query, segments helpers (25 tests).
* `tests/test_storyline_generation.py` — citation parser, glue parser, FTS fallback, end-to-end service with fake providers (26 tests).
* `tests/test_storyline_jobs.py` — worker + classifier + decider + JobRunner integration (14 tests).
* `tests/test_api_storylines.py` — REST endpoints with TestClient + fake generation service (7 tests).

Total: 72 new tests for this feature. Real Anthropic API calls are never made in tests — providers accept an injected `client=` to receive a fake.

## Related docs

* [`storylines-plan.md`](./storylines-plan.md) — design plan with decisions and tradeoffs
* [`entity-tracking.md`](./entity-tracking.md) — entity store this feature is anchored on
* [`mood-scoring.md`](./mood-scoring.md) — precedent for LLM-output baked into service data
* [`jobs.md`](./jobs.md) — job runner this feature plugs into
* [`architecture.md`](./architecture.md) — high-level service architecture
