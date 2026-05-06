# Known-entity context injection + stage-0 LLM-asserted match — Slice B

**Date:** 2026-05-07
**Branch:** `worktree-eng-known-entity-context`
**Plan:** `../.engineering-team/plan-entity-aliases-and-recognition.md`
(parent workspace).

## Context

Slice A (server foundation: alias CRUD, async re-embed-on-description-edit,
backfill CLI) shipped 2026-05-06 in commit `0ff7fe3` with the coverage gate
bumped from 65% to 80% in `6d801ea`. Slice B is the architecturally distinct
piece of WU4: feeding curated entities to the extraction LLM and processing
its `matches_known_id` response through a four-guard hybrid sanity check.

User decision recap (locked at plan time):

1. Hybrid sanity check, **not** trust-the-LLM. Don't skip stage-c.
2. Threshold `ENTITY_LLM_MATCH_MIN_COSINE` defaults to 0.3.
3. Notification topic for re-embed defaulted off (Slice A).
4. Backfill via CLI only (Slice A).

## What changed

### WU4-A — `match_source` column

New migration `0020_entity_mentions_match_source.sql` adds a nullable TEXT
column. `EntityMention.match_source: str | None` field added; populated by
`create_mention()`. Values: `"stage_a"` (exact name), `"stage_b"` (alias),
`"stage_c"` (embedding similarity ≥ threshold), `"llm_asserted"` (passed all
four WU4-D guards), or NULL (new entity created).

Telemetry only for now — useful when we eventually want to audit
LLM-asserted matches in a webapp UI, or quarantine "all mentions linked via
stage X" if a guard bug shows up.

### WU4-B — extraction tool schema + prompt

`record_entities` tool now accepts `matches_known_id: integer|null` and
`match_justification: string|null` per entity item. System prompt gained a
"Known entities protocol" section describing:

- the per-entry `known_entities` JSON block in the user message
- when to set `matches_known_id` (only ids in the supplied list)
- when to set `match_justification` (5–15 words, only when matches_known_id
  is non-null)
- the explicit NIL fallback: *"The list is NOT exhaustive. If no known
  entity is a good fit, leave both fields unset and propose a new entity
  as you normally would. Do not force a match."* This is the single most
  important defense against the anchoring/over-attribution failure mode
  documented in PromptNER, EntGPT, and the biomedical-EL paper.

The system prompt is wrapped in `cache_control: ephemeral` (was already the
case pre-Slice-B; Slice B keeps it). Per-entry candidate JSON goes in the
**user** message, not system, so the cache breakpoint isn't busted by the
varying candidate set.

### WU4-C — `build_known_entity_candidates`

New public method on `EntityExtractionService`. Given entry text and a
user_id:

1. Returns `([], {})` if user_id is None or the user has no entities with
   stored embeddings (skipping the OpenAI embed call entirely — no point
   embedding the entry if there's nothing to compare against).
2. Otherwise: iterates the six `ENTITY_TYPES`, calls
   `list_entities_of_type_with_embeddings()` per type, embeds the entry
   once via the configured embeddings provider, computes cosine
   similarity, drops anything below `llm_candidate_threshold` (default
   0.4), takes top `llm_candidate_top_k` (default 30) sorted by score
   desc.
3. Returns `(candidates, embeddings_by_id)`. The candidates list is the
   shape sent to the LLM (`id`, `canonical_name`, `entity_type`,
   `aliases`, `description`). `embeddings_by_id` is kept by the service
   so guard D doesn't re-fetch each candidate's embedding from the store.

Three new config knobs: `ENTITY_LLM_CANDIDATE_TOP_K` (30),
`ENTITY_LLM_CANDIDATE_THRESHOLD` (0.4), `ENTITY_LLM_MATCH_MIN_COSINE`
(0.3). All three threaded through `EntityExtractionService.__init__` and
the two construction sites (`mcp_server.py`, `cli.py`).

### WU4-D — stage-0 LLM-asserted match with four guards

`_resolve_entity` now accepts `matches_known_id` and `match_justification`.
When `matches_known_id` is set, runs the new `_try_llm_asserted_match` helper
before stage-a/b/c:

1. **Guard A (ownership):** `entity_store.get_entity(asserted_id, user_id)`
   must return non-None. Catches hallucinated ids and cross-user references.
2. **Guard B (candidate-set membership):** `asserted_id` must be in
   `self._current_candidate_ids` populated by `extract_from_entry` before
   the LLM call. Anything outside the catalog is hallucination by
   definition.
3. **Guard C (type match):** the asserted entity's `entity_type` must
   equal what the LLM is claiming for this mention. Catches person/topic
   confusions where two entities share a name.
4. **Guard D (cosine sanity):** `_cosine_similarity(new_mention_embedding,
   asserted_match_stored_embedding)` ≥ `llm_match_min_cosine` (default
   0.3). The new mention's embedding gets computed regardless (stage-c
   would too) so the cost is the comparison, not an extra OpenAI call.
   Catches semantic drift where the LLM picks the closest available
   candidate even when none is genuinely a match.

On reject: log at INFO with the failing guard and the LLM's
`match_justification` so the threshold can be retuned from real data.
Falls through to stage-a/b/c → may end up creating a new entity.

On accept: returns `(entity_id, created=False, ..., match_source="llm_asserted")`,
which propagates into `create_mention(match_source=...)`.

Stages a/b/c also now report their match_source. This is purely additive —
the existing tests still pass.

### Tests

18 new test cases:

- `TestBuildKnownEntityCandidates` — 5 cases: top-k+threshold ordering,
  top-k cap, no-embedded-entities skip, no-user-id skip, candidate dict
  shape (id, name, type, aliases, description).
- `TestExtractFromEntryPassesKnownEntities` — 2 cases verifying
  `extract_from_entry` calls `build_known_entity_candidates` and forwards
  the result via `known_entities=` kwarg.
- `TestLLMAssertedMatch` — 6 cases, one per guard (A/B/C/D rejection),
  one happy path with all guards passing, one no-matches_known_id-runs-
  normal-resolution.
- `TestMentions` — 2 cases for the new `match_source` column round-trip.
- `TestAnthropicExtractionProvider` — 3 cases for prompt content,
  per-call user-message formatting, and known_entities=None default.

Plus one `side_effect` signature touch-up (`**kwargs`) so existing batch-
extraction tests don't choke on the new `known_entities=` kwarg.

## Test summary

Baseline (Slice A merged main): 1751 passed, 84% coverage.
Final: **1769 passed (+18)**, 84% coverage held. Lint clean.

## Decisions worth flagging for future me

1. **Candidate scratch state lives on `self`.** `_current_candidate_ids` and
   `_current_candidate_embeddings` are populated at the top of
   `extract_from_entry` and read by `_resolve_entity`. Single-worker
   `JobRunner` makes this safe; if you ever bump `max_workers` above 1 or
   call `_resolve_entity` from multiple threads, plumb these as method
   args instead.
2. **Guard D uses the candidate's pre-fetched embedding** when available
   (`_current_candidate_embeddings[id]`) and falls back to
   `store.get_entity_embedding(id)` when missing. The fallback exists for
   robustness even though within `extract_from_entry` it's always present.
3. **The candidate set is union-across-types.** I considered restricting
   candidates to the same `entity_type` as the entry's likely mentions,
   but that requires a pre-pass classification we don't have. Simpler to
   send all top-K across all types and let the LLM pick — guard C catches
   wrong-type matches anyway.
4. **System prompt got bigger but stays cacheable.** Adding the
   known-entities protocol section is +20 lines, but it's static per
   author so the cache write still amortises.

## What's next

- Slice C: webapp — alias edit UI on `EntityDetailView`, collision
  dialog, manual merge from detail view, job-toast pipeline for re-embed.
- Possible future iteration on WU4: track LLM-asserted match
  acceptance/rejection rates from the logs after a few weeks of usage and
  retune the cosine threshold from real data.
