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
separate entities unless we push back. The service runs four stages per extracted entity:

1. **Stage 0 — LLM-asserted match (WU4).** Before the extraction call, the service vector-pre-filters the user's
   curated entities to a small candidate set (top `ENTITY_LLM_CANDIDATE_TOP_K` by cosine similarity to the entry text,
   above `ENTITY_LLM_CANDIDATE_THRESHOLD`) and passes them to the LLM as a `known_entities` JSON block. The model can
   set `matches_known_id` on any extracted entity to declare a link. The service accepts the link only after running
   four guards:

   - **Guard A — ownership:** the asserted id resolves to an entity owned by the current user.
   - **Guard B — candidate-set membership:** the asserted id was in the catalog we passed to the LLM. Anything outside
     is hallucination by definition.
   - **Guard C — type match:** the asserted entity's `entity_type` equals the type the LLM is claiming for this mention.
   - **Guard D — cosine sanity:** cosine of the new mention's embedding against the asserted match's stored embedding
     is ≥ `ENTITY_LLM_MATCH_MIN_COSINE` (default `0.3`). Catches semantic drift where the LLM picks the closest available
     candidate even when none is genuinely a match.

   On any guard failure the assertion is rejected, the failure is logged with the LLM's `match_justification`, and
   resolution falls through to stages a/b/c.
2. **Stage a — exact canonical name.** Case-insensitive lookup against `entities.canonical_name` filtered by
   `entity_type`. This catches the common case where the LLM produces the same canonical form across runs.
3. **Stage b — alias match.** Lookup against `entity_aliases.alias_normalised` (lowercased, stripped). The service
   searches for the new canonical form itself first, then each alias the LLM suggested. This catches "Atlas" matching a
   previously-seen entity whose canonical is "Atlas Wong" with "atlas" recorded as an alias.
4. **Stage c — embedding similarity fallback.** If all above fail, the service asks the embeddings provider to encode
   `f"{canonical_name} {description}"` as a single vector, then computes cosine similarity against every existing entity
   of the same type that has an embedding stored. If the best match is at or above `ENTITY_DEDUP_SIMILARITY_THRESHOLD`
   (default `0.88`), the service reuses that entity and adds a **warning** to the extraction result so the user can audit
   the merge. Below the threshold, a brand-new entity row is written and its embedding saved for future stage-c lookups.

Aliases the LLM produces for an entity are added to `entity_aliases` unconditionally (deduped via the unique index), so
they improve stage-b matching on the next run.

Each mention records which stage placed it in `entity_mentions.match_source` (one of `stage_a`, `stage_b`, `stage_c`,
`llm_asserted`, or NULL when a brand-new entity was created). This is telemetry-only today, used to retune thresholds
from real data and as a hook for a future audit UI.

## Description edits and recognition

The stored embedding (used by stage c above and as the cosine target for guard D in stage 0) is computed from the
entity's name and description. So an entity's description text genuinely influences future recognition — but only if the
embedding stays in sync with the description.

When a user edits an entity's description via `PATCH /api/entities/{id}`, the API enqueues an async `entity_reembed`
background job that recomputes the embedding from the new text and persists it. The PATCH response includes
`reembed_job_id` so the webapp can track it through the existing jobs / toast pipeline. Empty or whitespace-only
descriptions short-circuit (the job records `embedded=false` rather than overwriting with a meaningless vector).

Renaming an entity (canonical_name change) does NOT currently enqueue a re-embed — the existing embedding text mixes
the old name with the description, but recognition leans more heavily on description content than on name in practice,
and rename is comparatively rare. Revisit if recognition quality drops after batch renames.

For entities that pre-date this feature (whose embeddings were computed once at creation and never refreshed), run the
one-shot CLI:

```bash
journal backfill-entity-embeddings [--user-id N] [--dry-run]
```

It selects every entity with a non-empty description and re-embeds them serially. Idempotent. Cost is small: at
text-embedding-3-large pricing, ~$0.0001 per entity.

## Alias CRUD

Aliases are now a first-class user-managed concept, not just an LLM byproduct. Three endpoints surface this:

- `POST /api/entities/{id}/aliases` — adds an alias. Returns 409 with `existing_entity_id` / `existing_canonical_name`
  / `existing_entity_type` if the alias is already mapped to a different entity for this user, so the webapp can offer
  to merge into the existing entity. Idempotent on the same-entity case.
- `DELETE /api/entities/{id}/aliases/{alias}` — removes an alias.
- `GET /api/entities/aliases/lookup?alias=X` — non-mutating, type-agnostic collision check used by the webapp before
  submit.

See `docs/api.md` for full request/response shapes.

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

## Entity lifecycle and orphan cleanup

Entities are created during extraction and remain in the database as long as they have at least one mention. Two
operations can orphan an entity (leave it with zero mentions):

- **Entry deletion** — `DELETE /api/entries/{id}` removes the entry row, which cascades to `entity_mentions`. The API
  handler snapshots the entity IDs linked to the entry before deletion, then calls `delete_orphaned_entities()` to clean
  up any that lost all mentions.
- **Re-extraction after edit** — `PATCH /api/entries/{id}` with `final_text` queues an async entity extraction job. The
  extraction service deletes all existing mentions for the entry, re-extracts, and then prunes entities that lost all
  mentions as a result (`extract_from_entry()` in `entity_extraction.py`).

Both paths use the same `EntityStore.delete_orphaned_entities()` method, which only deletes entities from the candidate
set that have zero remaining mentions across all entries — so an entity mentioned in other entries is never pruned.

## Merging entities

Two or more entities that the extractor failed to consolidate (for example `"Atlas"` and `"Atlas Wong"` if neither alias
nor stage-c similarity caught them) can be merged manually. Merging collapses the absorbed entities into a single
**survivor**: their mentions and relationships move onto the survivor, their canonical names and aliases become aliases
on the survivor, and a snapshot of each absorbed entity is recorded in `entity_merge_history` (migration `0008`) so the
operation is auditable and — eventually — undoable.

### API

`POST /api/entities/merge`

```json
{
  "survivor_id": 42,
  "absorbed_ids": [88, 91]
}
```

Both fields are required. `survivor_id` is an integer; `absorbed_ids` is a non-empty list of integers. The handler
verifies the authenticated user owns every entity in the request before delegating to `EntityStore.merge_entities()`.

Response (200):

```json
{
  "survivor": { /* full entity detail, same shape as GET /api/entities/{id} */ },
  "absorbed_ids": [88, 91],
  "mentions_reassigned": 17,
  "relationships_reassigned": 4,
  "aliases_added": 3
}
```

Error cases: `400` for invalid payload shape or merging an entity into itself, `404` if any referenced entity is missing
or owned by another user.

### Data-model behaviour

`SQLiteEntityStore.merge_entities()` (`src/journal/entitystore/store.py`, around lines 664–757) does the following for
each absorbed entity, inside one transaction:

1. **Snapshot.** Inserts a row into `entity_merge_history` capturing `survivor_id`, `absorbed_id`, and the absorbed
   entity's `canonical_name`, `entity_type`, `description`, and `aliases` (JSON list) at the moment of merge.
2. **Reassign mentions.** `UPDATE entity_mentions SET entity_id = survivor_id WHERE entity_id = absorbed_id`. Mention
   rows are NOT deleted — the FK reassignment is enough.
3. **Reassign relationships.** Both the subject and the object side are rewritten (`subject_entity_id` and
   `object_entity_id`).
4. **Promote aliases.** The absorbed entity's existing aliases plus its own canonical name are added to
   `entity_aliases` for the survivor (`INSERT OR IGNORE`, normalised to lowercase). The survivor's own canonical name
   is excluded so it never appears as an alias of itself.
5. **Delete the absorbed row.** `DELETE FROM entities WHERE id = absorbed_id`. The `entity_aliases` rows belonging to
   the absorbed entity are removed by FK cascade.
6. **Auto-resolve candidates.** Any pending rows in `entity_merge_candidates` that reference the absorbed entity (on
   either side) are marked `accepted` with `resolved_at` set to the current UTC timestamp — so a manual merge
   automatically clears the merge-review queue for that entity.

A single call returns one `MergeResult` summarising totals across all absorbed entities.

### UI flow

From the entity list view (`src/views/EntityListView.vue`), each row exposes a checkbox. Selecting two or more rows
reveals a "Merge selected" action. Clicking it opens a modal that lists the selected entities with a radio button per
row; the operator picks the survivor and confirms. The webapp posts to `/api/entities/merge` and reloads the list. The
absorbed entities disappear; the survivor's alias list grows by however many distinct surface forms it just inherited.

### Merge candidates

The string-signature heuristic in `src/journal/services/entity_extraction/signature.py` is the sole producer of merge
candidates. It flags pairs of same-type entities whose canonical names differ only in case, whitespace, trivial
punctuation, or short divergent tails (place names like `"Zij Kanaal C Zuid"` vs `"Zij Kanaal C Weg"` that the
embedding distance alone misses). Tail shape is filtered: possessive markers (`'s`), purely numeric specifiers,
and word-shaped tails are rejected so relational suffixes (`"John Mathews' mother"`), specifiers (`"Psalms 63"`),
and parent/child place pairs (`"Haarlem"` vs `"Haarlem Centraal"`) do not produce false positives. The helper
`_is_likely_word_tail` centralises that judgment.

Embedding similarity above `ENTITY_DEDUP_SIMILARITY_THRESHOLD` (default `0.88`) auto-merges as before — that path
is unchanged. Embedding similarity *below* the threshold no longer creates a candidate: the previous "near-miss"
band (`max(threshold - 0.15, 0.5)` to threshold) was removed in WU4 because in real-world data it produced zero
useful suggestions over many false positives (semantically related but distinct entities like Hermione vs Neville).

Candidates are stored per-pair in `entity_merge_candidates` (migration `0022`) — the table's `UNIQUE(entity_id_a,
entity_id_b)` constraint means repeated extraction runs UPSERT into the same row instead of inserting a new
'pending' row each time. The UPSERT keeps the higher similarity and never resurrects an already-dismissed pair.

Pending candidates surface on the entity list view as a "Possible duplicates to review" banner. Each pair can be:

- **Accepted** — the webapp issues `POST /api/entities/merge` to fold the lower-mention-count entity into the
  higher-mention-count one (or vice versa, at the operator's discretion).
- **Dismissed** — `PATCH /api/entities/merge-candidates/{id}` with `{"status": "dismissed"}`. The dismissal also
  writes a row to `entity_pair_decisions` (migration `0021`), which the extraction service consults before
  creating new candidates. So a dismissed pair never resurfaces — even after future extractions, edits, or
  re-embedding.

When two entities are merged, any rejection rows involving the absorbed entity are transferred to the survivor
(`_transfer_pair_rejections_for_merge`), so the user's "these are not the same" decision survives a subsequent
merge of A into a third entity.

`GET /api/entities/merge-candidates?status=pending&limit=50` lists current candidates;
`GET /api/entities/{id}/merge-history` returns the audit trail for a survivor;
`GET /api/entities/pair-decisions` returns the user's persisted rejections, and
`DELETE /api/entities/pair-decisions/{id}` undoes one. The webapp surfaces the latter as a "Past dismissals"
panel on the entity list view.

## Quarantine

Quarantine is a **soft-hide** for entities that look broken but are worth keeping around. A quarantined entity row
stays in the database — its description, aliases, and merge history are preserved — but it is excluded from the default
entity list and from chart endpoints, so it doesn't pollute the UI while the operator decides what to do with it.

The schema columns landed in migration `0018_entity_quarantine.sql`: `is_quarantined` (0/1), `quarantine_reason`
(free-text), and `quarantined_at` (UTC ISO-8601 string, empty when not quarantined). Migration adds a partial index
`idx_entities_quarantined` so that listing the (typically small) quarantined set stays fast without touching the
active-entity hot path.

### When entities are quarantined

Two paths:

1. **Automatic, post-extraction sanity sweep.** After extraction completes for an entry, the service re-checks every
   entity that was touched by the run (created or matched). If the entity's `canonical_name` does not appear (case-
   insensitively, whitespace-tolerant) in any of its mention quotes, and does not appear in the `final_text` of any
   entry it is mentioned in, the entity is flagged as quarantined. This catches the LLM-hallucination failure mode
   where a canonical name was invented out of partial context and never actually written by the author, including the
   "zombie rebound" case where a hallucinated canonical (e.g. `"Zij Kanaal C Zuid"`) re-binds to a corrected entry
   text via embedding similarity. The journal author entity is exempt — first-person prose ("I went...") legitimately
   produces an author mention whose canonical (the user's display name) is never written verbatim. The implementation
   lives in `EntityExtractionService.extract_from_entry` (the sweep) and `_canonical_name_supported` (the matcher) in
   `src/journal/services/entity_extraction.py`.
2. **Automatic, hallucinated-name rejection at the LLM layer.** When the extraction provider receives an entity whose
   `canonical_name` is not a substring of its source quote, it first tries a longest-token-substring repair against the
   quote (rebinding `"Zij Kanaal C Zuid"` to `"Zij Kanaal C"` when the quote contains only the shorter form). If no
   token-aligned substring of length ≥ 3 chars matches, the entity is still surfaced (so the audit trail isn't lost)
   but is flagged with `pending_quarantine_reason`; the extraction service quarantines the new row at creation time.
   See `_longest_canonical_substring_in_quote` and the WU4 branch in `_parse_tool_response`
   (`src/journal/providers/extraction.py`).
3. **Manual.** An operator can quarantine an entity directly from the entity detail view — useful for deliberately
   hiding noise (a single accidental mention of a public figure, for instance) without deleting the row outright.

### Endpoints

- `GET /api/entities/quarantined` — returns the authed user's quarantined entities (full detail shape, including
  `is_quarantined`, `quarantine_reason`, and `quarantined_at`).
- `POST /api/entities/{id}/quarantine` — body `{"reason": "<free text>"}`. Sets the flag, stamps the timestamp, and
  returns the updated detail. 404 if the entity is missing or not owned by the user; 400 if `reason` is non-string.
- `POST /api/entities/{id}/release-quarantine` — clears the flag, reason, and timestamp. Returns the updated detail.
  Idempotent on already-active entities.

The default `GET /api/entities` list and the dashboard chart endpoints (`entity-distribution`, `entity-trends`,
`mood-entity-correlation`) exclude quarantined rows. The store-level methods accept an `include_quarantined=True`
flag for callers (e.g. an admin "show everything" view) that need the unfiltered set.

### Operator guidance

If a quarantined entity is a duplicate of a clean survivor — which is the common case after an LLM hallucination —
prefer **merging** it into the survivor (see "Merging entities" above) over releasing it. Merging preserves the
hallucinated row's mentions on the correct entity and records the absorbed name in `entity_merge_history`, whereas a
plain release leaves the bad canonical name visible to the UI again.

## Casing normalization

Entity canonical names are smart-title-cased at write time, both when a row is inserted in
`SQLiteEntityStore.create_entity()` *and* when an admin edits an existing row via
`SQLiteEntityStore.update_entity()`. The transformation is performed by `smart_title_case()` in
`services/entity_naming.py`. **The DB is the single source of truth** — there is no client-side
display normaliser; the webapp renders `canonical_name` verbatim everywhere it appears.

### Algorithm (per word)

For each space-separated word, in order:

1. Whole-string match in the exceptions table → return the table's preserved-case value.
2. Word-by-word:
   - If the word is a fully-uppercase acronym (length > 1, e.g. `NASA`) → preserve verbatim.
   - If the word has an *intra-word* uppercase — uppercase letter after a lowercase letter
     in the same word (`iOS`, `eBay`, `McDonald's`, `DeepMind`) → preserve verbatim.
   - If the word is an article / preposition / Dutch particle in a non-leading position →
     lowercase (`The Lord of the Rings`, `Vincent van Gogh`).
   - Otherwise → title-case. Hyphen-segments are title-cased independently (`anglo-saxon →
     Anglo-Saxon`).

The per-word check is what makes inputs like `iOS app → iOS App` work. An older revision
short-circuited on a whole-string mid-word-uppercase check, which incorrectly froze the
entire input verbatim whenever any single word had non-leading uppercase.

### Exception list

Names that need explicit overrides — acronyms (`SQL`, `API`, `IKEA`), mixed-case brands
(`iOS`, `iPhone`, `GitHub`, `PostgreSQL`), contractions (`O'Brien`, `McDonald's`) — live in
`config/entity-casing-exceptions.toml`. Keys are lowercased; values are the preserved-case
form. The file is operator-managed: edit it to add an entry, then call `POST
/api/admin/reload/entity-casing` to refresh the in-memory cache without a server restart.
The reload route is wired through `services/reload.py` alongside the other hot-reload
endpoints.

### Backfilling existing rows

After landing the normaliser, or after extending the exceptions TOML, run:

```bash
uv run journal renormalise-entity-casing             # dry-run — prints proposed renames
uv run journal renormalise-entity-casing --apply      # writes the changes
```

This walks every row in `entities`, applies `smart_title_case` + the loaded exceptions, and
updates `canonical_name` for any row that would change. If a proposed rename would collide
with an existing entity of the same `(user_id, entity_type)`, the CLI surfaces it but does
not auto-merge — the merge UI is the right place to resolve those.

`UNIQUE(user_id, entity_type, canonical_name)` (migration `0011`) uses SQLite's default
`BINARY` collation, so the backfill cannot silently merge `running` and `Running` — it will
flag the collision. Resolve via the merge UI, then re-run the backfill if needed.

## Post-LLM canonical_name validation

The model has two failure modes worth defending against. Both run inside `_parse_tool_response` in
`src/journal/providers/extraction.py` so they apply to every extraction call regardless of provider.

### Mode 1 — clipped trailing character (`Nautilin`/`Nautiline`)

`_repair_canonical_name` checks every entity against its own `quote` at the **token** level:

1. If any whitespace-separated token in the quote (after stripping surrounding punctuation) **equals** the canonical_name
   case-insensitively, the LLM's choice is trusted unchanged. This protects deliberately-shorter canonicals like `"Bob"` for a
   quote `"Robert 'Bob' Smith"` where the short form is genuinely a separate token.
2. If the only longer token differs only by an inflection suffix (`'s`, `s'`, `s`), the canonical is also trusted — we do not
   promote `"Hermione"` to `"Hermione's"` or `"Daniel"` to `"Daniels"`.
3. Otherwise, if the canonical_name is a strict prefix of some longer token in the quote, the longer token is returned (case
   preserved from the original token). This catches the clipped-trailing-character failure mode.

A separate `WARNING` log fires whenever a repair triggers, so the rate of LLM mis-extraction is visible.

### Mode 2 — hallucinated canonical (`Zij Kanaal C Zuid`)

When the canonical_name is not a substring of the quote at all and the prefix-repair above produces nothing,
`_longest_canonical_substring_in_quote` looks for the longest **token-aligned** substring of the canonical that does appear in
the quote (case-insensitive, whitespace-tolerant, minimum 3 characters). If found, the canonical is renamed to that substring
and an `INFO` log is emitted. If not, the original canonical is preserved (for the audit trail) but the parsed entity carries
a `pending_quarantine_reason` field; the extraction service soft-quarantines the new row immediately on creation. Together with
the post-extraction sanity sweep above, this catches both fresh hallucinations and zombie rebounds.

### `journal repair-entity-names`

A CLI subcommand for cleaning up existing entities that were created before the validator shipped. It iterates every entity,
runs the same repair logic against each entity's mention quotes, and proposes updates. Dry-run by default; pass `--apply` to
update rows. Skips proposed repairs that would collide with the canonical_name of another entity for the same user.

```
docker exec journal-server uv run journal repair-entity-names               # dry run, prints proposed repairs
docker exec journal-server uv run journal repair-entity-names --apply       # actually update
```

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
