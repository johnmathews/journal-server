# Entity Tracking

Journal entries are freeform text. Entity tracking adds a structured layer on top: after an entry is ingested and
(optionally) corrected, an on-demand batch job sends each entry's `final_text` to Claude, asks for named entities and the
relationships between them, and persists the results to SQLite.

The pipeline is intentionally decoupled from ingestion — extraction is expensive, and running it only when the user asks
means a bad OCR pass can be corrected before any LLM cost is sunk.

## What gets extracted

Two things:

1. **Entities** — people, places, activities, organizations, topics, or a catch-all "other". Each has a `canonical_name`
   (the form the author most often uses), an optional free-text description, zero or more alternate surface forms
   (aliases), and the date of the earliest entry the entity was seen in.
2. **Relationships** — directed edges between two entities in the context of a specific entry.
   `(subject, predicate, object, quote, confidence, entry_id)`. Predicates are free text, but the extractor's system
   prompt suggests a small preferred list (`at`, `visited`, `works_for`, `knows`, `plays`, `attended`, `mentioned`,
   `part_of`, `located_in`) so the graph stays broadly consistent across runs.

A **mention** is the link between an entity and an entry — every time an entity appears in an entry's text, one mention
row is written, carrying a verbatim quote and the extraction's confidence. A long entry can mention the same entity many
times.

## How extraction runs

Kicked off via:

- `journal extract-entities --entry-id N` — single entry
- `journal extract-entities --start-date YYYY-MM-DD --end-date ...` — batch
- `journal extract-entities --stale-only` — only entries whose text changed since their last extraction run
- `POST /api/entities/extract` with `{entry_id?, start_date?, end_date?, stale_only?}`
- MCP tool `journal_extract_entities(...)`
- **Automatically** when entry text is saved via `PATCH /api/entries/{id}` with `final_text` — the API queues an async
  extraction job so entity mentions stay in sync with corrected text. The response includes `entity_extraction_job_id`
  when a job is queued.

Every invocation assigns a fresh UUID `extraction_run_id` which is written on every mention and relationship row the run
produces. Re- running extraction for the same entry is safe — the service deletes any existing mentions and relationships
for that `entry_id` before writing new ones, so the count can never double.

A trigger on `entries.final_text` sets the `entity_extraction_stale` flag back to `1` whenever an entry is edited. The
service clears it once extraction succeeds. `--stale-only` uses this flag to skip entries that have already been
processed and not touched since.

## Dedup strategy

Entity consolidation is the hardest part — the LLM will happily produce "Atlas", "atlas", and "Atlas Wong" as three
separate entities unless we push back. The service runs three stages per extracted entity:

1. **Stage a — exact canonical name.** Case-insensitive lookup against `entities.canonical_name` filtered by
   `entity_type`. This catches the common case where the LLM produces the same canonical form across runs.
2. **Stage b — alias match.** Lookup against `entity_aliases.alias_normalised` (lowercased, stripped). The service
   searches for the new canonical form itself first, then each alias the LLM suggested. This catches "Atlas" matching a
   previously-seen entity whose canonical is "Atlas Wong" with "atlas" recorded as an alias.
3. **Stage c — embedding similarity fallback.** If both above fail, the service asks the embeddings provider to encode
   `f"{canonical_name} {description}"` as a single vector, then computes cosine similarity against every existing entity
   of the same type that has an embedding stored. If the best match is at or above `ENTITY_DEDUP_SIMILARITY_THRESHOLD`
   (default `0.88`), the service reuses that entity and adds a **warning** to the extraction result so the user can audit
   the merge. Below the threshold, a brand-new entity row is written and its embedding saved for future stage-c lookups.

Aliases the LLM produces for an entity are added to `entity_aliases` unconditionally (deduped via the unique index), so
they improve stage-b matching on the next run.

## Author handling

First-person statements are a common case: "I went to Blue Bottle" should become `(John, visited, Blue Bottle)`. The
extraction service is configured with `journal_author_name` (default `"John"`, overridable via `JOURNAL_AUTHOR_NAME`).
The adapter's system prompt tells the model to use this name as the subject of first-person actions, and the service
ensures an author entity of type `person` exists — creating one lazily on first use — before wiring a relationship that
references the author.

## Query surface

Storage-agnostic read API, both REST and MCP:

- `GET /api/entities?type=person&search=atlas&limit=50`
- `GET /api/entities/{id}` — full detail with aliases
- `GET /api/entities/{id}/mentions` — every mention with entry date
- `GET /api/entities/{id}/relationships` — outgoing and incoming edges
- `GET /api/entries/{id}/entities` — entities tied to a specific entry
- MCP: `journal_list_entities`, `journal_get_entity_mentions`, `journal_get_entity_relationships`

## Known risks

- **Predicate drift.** Predicates are free text. Over time an author's "met", "saw", "caught up with" will all refer to
  the same relationship. A future normalisation pass (mapping free text → canonical predicate, possibly via a small LLM
  call) is deferred until there's enough data to see the drift.
- **Entity drift.** The stage-c threshold is a tuning knob — raising it misses real merges, lowering it causes false
  joins. The `warnings` field on the extraction result is the user's escape hatch: every stage-c merge surfaces a
  "potential merge" line they can review.
- **Idempotency at the batch level.** Within a single entry the service deletes prior mentions and relationships before
  writing new ones, so a rerun replaces cleanly. Across entries in a batch, a failed extraction for one entry does NOT
  roll back any other entry — it is captured as a warning on a synthetic `ExtractionResult` and the batch continues.
- **Cost at scale.** Extraction is one Claude call per entry per run. A year of daily journaling is ~365 calls if you
  re-extract everything from scratch; stale-only reruns keep the day-to-day cost small.

## Storage-agnostic Protocol

Everything the service touches goes through the `EntityStore` Protocol (`src/journal/entitystore/store.py`). The SQLite
implementation is the source of truth for now, but a graph-DB backend (Memgraph, LadybugDB, Neo4j, ...) can be dropped in
against the same Protocol without touching `EntityExtractionService`. The goal of Phase 2 is to experiment with a native
graph backend once there's enough data to make the comparison meaningful.

## Migration

`0004_entities.sql` adds:

- `entities`, `entity_aliases`, `entity_mentions`, `entity_relationships` tables.
- `entries.entity_extraction_stale` column (default `1` so all existing entries are flagged for first-pass extraction).
- `entries_entity_stale_on_final_text` trigger that re-flags an entry as stale whenever its `final_text` is updated.

Legacy `people` and `places` tables from migration `0001` are intentionally left untouched — they are unused in the
current codebase but removing them would risk a schema mismatch on an existing database. The entity extraction feature
does not read from or write to them.
