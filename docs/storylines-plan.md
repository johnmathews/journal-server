# Storylines — Plan

**Status:** server cycle closed 2026-05-12 (W1-W12 + W10 acceptance gate passed). Webapp cycle pending — see "Webapp cycle handoff" at the bottom. **Last updated:** 2026-05-12. **Supersedes:** none.

A new feature: LLM-synthesized cross-entry narratives about recurring threads in the journal. Each storyline anchors on a single entity (e.g. "Atlas the son" — entity id 3, 17 mentions; or "Running" — entity id 59, 18 mentions) and renders as two parallel panels:

1. **Curation panel** — verbatim entry excerpts in chronological order, with minimal Haiku-generated transition prose ("Three days later:").
2. **Third-person narrative panel** — a flowing third-person prose account, grounded in source entries via the Anthropic Citations API.

Storylines are open-ended: when a new entry is ingested, the system classifies whether it extends an existing storyline, and if so re-generates the affected portion.

This plan covers the **server-side spike** for two seeded storylines (Running, Atlas) over the last three months. The webapp cycle is a separate worktree.

## Decisions & tradeoffs

1. **Citations API with custom-content documents is the load-bearing grounding mechanism.** One block per entry; the returned `start_block_index` maps back to the entry ID. Pointers are parsed by Anthropic, not generated, so they cannot be fabricated. **Tradeoff:** Citations is incompatible with Structured Outputs — we keep Citations and parse the structured citation blocks ourselves into our schema.

2. **Pass the full corpus to Opus 4.7; no retrieval pipeline for the spike.** 22k words fits in 1M context with 25× headroom. Eliminates BM25/dense/RRF ranking complexity. Retrieval becomes a concern only when storylines auto-discover threads or the corpus grows past a year.

3. **Opus 4.7 for narrative, Haiku 4.5 for glue.** Narrative is the main reading surface — flagship quality matters. Glue is templated; capability isn't the binding constraint. We are not building a Sonnet 4.6 A/B comparator in this spike — if narrative quality issues surface in W10, the response is prompt iteration first, model swap as a fallback decision in a follow-up workstream.

4. **Three-breakpoint prompt caching.** 1h TTL on system prompt + tool defs; 5m TTL on stable entry prefix; 5m TTL on newest entry block. Pre-warm with `max_tokens=0` on app boot. Cached input cost drops from ~$0.14 to ~$0.014/regen.

5. **Hybrid extension classifier.** Entity-overlap prefilter → embedding-similarity prefilter → Haiku 4.5 decider with structured response. Worst-case 1-2 Haiku calls per ingestion. The justification the LLM emits is itself useful UI surface.

6. **Storylines are keyed on `entity_id`, not raw surface forms.** Disambiguation is pushed down to the entity layer. At storyline-create time, if multiple entities match the input (e.g. "Atlas" → person id 3 AND organization id 799), return a disambiguation prompt with type + mention count + example sentence; the user picks.

7. **API returns structured segment lists**, not markdown. Each panel is `list[Segment]` where `Segment = {kind: "text", text: str} | {kind: "citation", entry_id: int, quote: str}`. The webapp has no markdown renderer; this shape lets the frontend render text runs and SPA-routed `<RouterLink>` citations without a new dependency.

8. **FTS fallback as a robustness layer.** For surface forms where entity extraction has known gaps (notably pronominal references like "my son" / "he" → Atlas, which the Tier 3 coreference work has not addressed yet), the storyline generation service falls back to FTS5 search on the surface form for the date window when entity mentions are sparse.

9. **`docs/architecture.md` gets an honest update.** It currently says "no model reads, interprets, or summarizes your journal entries during search." Storylines bakes LLM prose synthesis into served data. Mood scoring is a partial precedent (scalar output); storylines is the first feature to produce LLM prose for display. The doc update is a work unit, not a footnote.

## Non-goals

This plan does **not** include:

1. **Auto-discovery of storylines.** User names the thread. Auto-clustering across the corpus is a separate workstream that depends on knowing the rendering is valuable.
2. **Coreference resolution.** Pronouns "he"/"she"/"my son" remain unresolved. Listed as Tier 3 #8 in the roadmap; not blocking this spike (the seeded Atlas storyline has 17 entity-anchored mentions).
3. **Entity-extraction backfill.** Pre-2026-04-13 entries may not have been re-extracted, but the prereq inspection (W1) will confirm whether this materially affects the two seeded storylines. If yes, the backfill becomes its own follow-up workstream.
4. **Two-pass narrative verification.** Best-of-N, iterative refinement, claim-by-claim audit — out of scope unless qualitative eval in W10 shows residual hallucination after Citations + quote-first + system-prompt restrictions.
5. **Synchronized scrolling between panels.** Webapp cycle; polish item, not core.
6. **Webapp UI.** Separate worktree, separate plan, run after this server cycle ships.

## Kill criteria

The spike has clear failure conditions. Watch for them:

1. **Narrative reads as uncanny / fabricated** despite Citations + quote-first prompting + system-prompt restrictions, even after 3 prompt iterations. If the third-person model puts words in the user's mouth or invents emotional states, the feature fails its acceptance bar regardless of test coverage.
2. **Citation pointers don't correspond to readable text quotes** (Citations API bug, document encoding issue, provider drift). The whole architectural choice rests on this.
3. **Cost-per-regeneration after caching exceeds $0.05.** Would change the product economics — storylines are supposed to be cheap to regenerate as new entries arrive.

If any kill criterion fires, stop and reassess before continuing.

## Ordering rationale

Foundation-first (migration → repo → service → job → API/MCP), then risk-first within layers (the narrative-generation service is W4, before integration plumbing in W5+, so we surface "does the rendering work?" early), then seed + qualitative read (W10) as the gate before docs/journal polish.

W10 is the **real acceptance gate**: storylines exist as production data, readable via MCP/REST, even before any webapp UI is built. If W10's output isn't worth keeping, the webapp cycle doesn't run.

## Work units

### W1 — Prerequisite inspection [S, Risk: Low]

One-shot reads that close gaps the eval report identified. No code changes.

- **Changes:** none — read-only checks via existing MCP tools / SQL.
- **Test impact:** none.
- **Reversibility:** N/A.
- **Acceptance:** four answers documented in the journal entry for this cycle:
  1. Atlas entity id 3 mention count over last 3 months (we expect ~10-17; verify the date filter narrows correctly).
  2. Running entity id 59 mention count over last 3 months (we expect ~10-18).
  3. Whether `journal_get_entity_mentions` returns mentions in a useful order (recon flagged `created_at DESC` — confirm chronological re-sort is needed in W3).
  4. Whether prompt caching fires for typical storyline prompts (4096-token minimum; check `cache_creation_input_tokens` on first real call).

### W2 — Migration 0027_storylines [S, Risk: Low]

- **Changes:** add `src/journal/db/migrations/0027_storylines.sql`. Tables: `storylines` (id, user_id, entity_id FK, name, description, last_generated_at, status, created_at, updated_at) and `storyline_panels` (id, storyline_id, panel_kind ∈ {curation, narrative}, segments_json TEXT, generated_at, model_used, citation_count). Indices on user_id, entity_id, (storyline_id, panel_kind).
- **Test impact:** none directly (migration verified by every test using the `factory` fixture). Add a smoke test in `tests/test_migrations.py` if one exists; otherwise rely on factory teardown.
- **Reversibility:** SQLite migration is forward-only by design here; rollback is a `DROP TABLE IF EXISTS` follow-up migration. For a single-user prod instance with reversible-via-revert app code, acceptable.
- **Acceptance:** migration runs cleanly on a fresh DB; runs idempotently when applied twice (CREATE IF NOT EXISTS); existing tests pass.

### W3 — Repository methods [M, Risk: Low]

- **Changes:** new method on `SQLiteEntityStore` (or a new `_StorylineMixin` on the repository) — `get_dated_mentions_for_entity(entity_id, user_id, start_date, end_date) -> list[DatedEntryExcerpt]`. SQL joins `entity_mentions` + `entries` ordered by `entry_date ASC`. New `SQLiteStorylineRepository` for the storylines table itself: `create_storyline`, `get_storyline`, `list_storylines`, `update_storyline_panels`, `record_extension_decision`.
- **Test impact:** new test file `tests/test_storyline_repository.py` covering create/get/list/update + the dated-mentions join with a small fixture corpus. Reuse `factory` + `run_migrations` pattern from `tests/test_api_jobs.py`.
- **Reversibility:** pure code, revert commit.
- **Acceptance:** dated-mentions query returns entries in `entry_date ASC` order for entity 59 (Running) over a 3-month window; storyline CRUD round-trips via SQLite.

### W4 — Storyline generation service [L, Risk: Medium]

The hardest work unit. Splitting would create artificial seams — narrative and glue share the cached document array and prompt-design discipline.

- **Changes:**
  - New `src/journal/providers/storyline_narrator.py` — `AnthropicStorylineNarrator(model=opus-4-7)`. Implements Citations API with custom-content documents (one block per entry, block index ↔ entry_id mapping), three-breakpoint cache layout (1h system / 5m stable entries / 5m newest entry), quote-first prompt structure (first emit JSON of `{entry_id, quote}` plans, then narrative). System prompt explicitly: third person, no inventions, "I don't know" allowed, external knowledge restricted to provided documents.
  - New `src/journal/providers/storyline_glue.py` — `AnthropicStorylineGlue(model=haiku-4-5)`. Generates 1-2 sentence transitions between adjacent excerpts given (prev_excerpt, next_excerpt, gap_days).
  - New `src/journal/services/storylines/__init__.py` with `StorylineGenerationService` — orchestrates: fetch dated mentions (W3) → optional FTS fallback for sparse entities → build cached document array → call narrator + glue → parse citations into `Segment` list → persist via storyline repository (W3).
  - New `src/journal/services/storylines/segments.py` — `Segment` dataclass and citation-parser utility.
  - `config.py`: new env vars `STORYLINE_NARRATOR_MODEL` (default `claude-opus-4-7`), `STORYLINE_GLUE_MODEL` (default `claude-haiku-4-5`).
- **Test impact:**
  - New `tests/test_storyline_narrator.py` with a fake Anthropic client returning a canned Citations response. Asserts the request shape (cache_control breakpoints, document blocks, citations enabled, system prompt content), the response parser handles citations correctly, and the resulting `Segment` list interleaves text + citation kinds.
  - New `tests/test_storyline_glue.py` similar shape for Haiku.
  - New `tests/test_storyline_generation_service.py` end-to-end with fake providers + real SQLite (factory fixture), asserts segments persist and entity_id → date-ordered selection works.
- **Reversibility:** pure code + new files. Revert commit. No data-side state to undo (panels are derived).
- **Acceptance:** running the service against entity 59 (Running) produces a non-empty `Segment` list for both panels, with each citation segment carrying a valid `entry_id` from the 3-month window; running against entity 3 (Atlas) same; tests pass; ruff clean.

### W5 — Job worker + JobRunner integration [M, Risk: Low]

- **Changes:**
  - New `STORYLINE_GENERATION_KEYS` in `src/journal/services/jobs/validation.py` (params: `storyline_id`).
  - New `src/journal/services/jobs/workers/storyline_generation.py` with `run_storyline_generation(ctx, job_id, params)`. Mirrors `mood_score_entry.py`: mark running → call `ctx.storylines.regenerate(storyline_id)` → mark_succeeded / mark_failed → notification.
  - `JobRunner.submit_storyline_generation(storyline_id, ...)` method in `services/jobs/runner.py`.
  - `WorkerContext` gets a new `storylines: StorylineGenerationService` field.
  - `JobType` literal in `models.py` gains `"storyline_generation"`.
- **Test impact:** new tests in `tests/test_jobs_workers.py` (or a new sibling) using fake `StorylineGenerationService`, asserting terminal state and progress updates.
- **Reversibility:** pure code. Revert commit.
- **Acceptance:** `submit_storyline_generation(storyline_id=1)` produces a `succeeded` job with the result blob containing `panels_generated: 2`; `runner.shutdown(wait=True)` exits cleanly.

### W6 — Extension classifier service [M, Risk: Low]

- **Changes:**
  - New `src/journal/services/storylines/extension.py` with `StorylineExtensionClassifier`. Three-stage pipeline: (1) entity overlap — does the entry's extracted entities include the storyline's anchor entity? (2) embedding similarity — cosine between entry embedding and storyline summary embedding (lazily cached on the storyline row); (3) Haiku 4.5 decider with structured tool-use returning `{decision: "yes"|"no"|"maybe", reasoning: str}`.
  - New `src/journal/providers/storyline_extension_decider.py` — `AnthropicStorylineExtensionDecider(model=haiku-4-5)`. Tool-use pattern from `mood_scorer.py:248`.
- **Test impact:** new `tests/test_storyline_extension.py` with fakes for each stage. Cover: stage 1 short-circuit ("yes" via entity overlap, no Haiku call needed); stage 2 short-circuit ("no" via embedding distance, no Haiku call needed); stage 3 final decision.
- **Reversibility:** pure code. Revert commit.
- **Acceptance:** classifying a "running" entry against the Running storyline returns "yes"; classifying an unrelated entry returns "no".

### W7 — Ingestion hook [S, Risk: Low]

- **Changes:** `services/jobs/runner.py:569` (`_queue_post_ingestion_jobs`) gains a call to `self.submit_storyline_extension_check(entry_id, user_id, ...)` after the entity-extraction job is queued. This covers text + image + voice ingestion uniformly. The extension-check job itself: for each active storyline owned by user_id, run the classifier (W6); for each "yes", regenerate the affected storyline panels (W5).
  - New worker `run_storyline_extension_check` in `workers/storyline_extension_check.py` orchestrating the per-storyline classifier loop.
  - `JobRunner.submit_storyline_extension_check` method.
- **Test impact:** integration test in `tests/test_jobs_ingestion_hook.py` — ingest a new entry, assert that a `storyline_extension_check` job is queued, that it classifies correctly, and that storylines with positive extensions trigger regeneration jobs.
- **Reversibility:** pure code. Revert commit. The hook can also be feature-flagged via `STORYLINE_AUTO_EXTEND` env var (default `true`); flip to `false` if the classification proves noisy in prod.
- **Acceptance:** ingest a new running-themed text entry; observe an extension-check job queued and completing; observe a regenerate job queued for the Running storyline.

### W8 — API routes [M, Risk: Low]

- **Changes:**
  - New `src/journal/api/storylines.py`: `GET /api/storylines` (list+pagination, standard envelope), `GET /api/storylines/{id}` (detail with both panels as `Segment` lists).
  - `src/journal/api/ingestion.py` gains: `POST /api/storylines` (create — body: `{entity_id, name, description?}`), `POST /api/storylines/{id}/regenerate` (queues a generation job, returns `{job_id}`).
  - Register in `api/__init__.py`.
- **Test impact:** new `tests/test_api_storylines.py` covering all four endpoints + auth (use the existing auth fixture pattern from `test_api_jobs.py`).
- **Reversibility:** pure code. Revert commit.
- **Acceptance:** `curl GET /api/storylines` returns the seeded pair; `POST /api/storylines/1/regenerate` returns a job_id and the job completes; both endpoints respect user_id auth.

### W9 — MCP tools [S, Risk: Low]

- **Changes:** new `src/journal/mcp_server/tools/storylines.py` with `journal_list_storylines`, `journal_get_storyline`, `journal_create_storyline`, `journal_regenerate_storyline`. `_get_storyline_service(ctx)` helper in `tools/_ctx.py`. `journal_regenerate_storyline` uses `_poll_job_until_terminal`.
- **Test impact:** new `tests/test_mcp_tools_storylines.py` — same fake-service pattern.
- **Reversibility:** pure code. Revert commit.
- **Acceptance:** MCP tools are listed by `mcp__journal__*` discovery; calling `journal_get_storyline(1)` returns formatted output.

### W10 — Seed the two spike storylines + qualitative read [M, Risk: Low]

The acceptance gate for the entire spike. **This work unit is the answer to "does this feature work?"**

- **Changes:**
  - Seed via `POST /api/storylines` (or CLI): `(entity_id=59, name="Running")` and `(entity_id=3, name="Atlas")`. Trigger regeneration for both.
  - Read the generated curation and narrative panels for each. **Read carefully.**
  - Iterate prompts in W4 if the narrative reads as fabricated, generic, or emotionally extrapolative. Up to 3 prompt iterations before invoking kill criterion #1.
- **Test impact:** none added. This is a qualitative gate, not a test gate.
- **Reversibility:** the storylines are data; delete via SQL or a follow-up `DELETE /api/storylines/{id}` route if added.
- **Acceptance:** **the user reads both storylines and judges them honest, useful, and non-fabricating.** If yes, proceed to W11/W12. If no, kill criterion #1 has fired — stop, reassess.

### W11 — Docs [S, Risk: Low]

- **Changes:**
  - New `docs/storylines.md` — reference doc (data model, panel shapes, generation pipeline, provider mapping, regeneration semantics, extension classifier). Analogous to `docs/entity-tracking.md` / `docs/mood-scoring.md`.
  - Update `docs/architecture.md` — honest acknowledgment that the service now includes in-service LLM prose synthesis. Section: "How storylines work" or similar. Update the "no model reads or interprets" claim under "How Search Works" to be accurate about scope.
  - Update `docs/roadmap.md` — flip this plan's entry from "Active planning docs" to a closed entry once W10 passes; add a Tier 1 (or Tier 2) entry pointing at `docs/storylines.md` and this plan.
  - Update `docs/api.md` and any MCP tool index.
- **Test impact:** none.
- **Reversibility:** pure docs. Revert commit.
- **Acceptance:** docs accurately reflect what shipped; no claim in any active doc contradicts the running code.

### W12 — Journal entry [S, Risk: Low]

- **Changes:** `journal/<YYMMDD>-storylines-server-spike.md` — what was built, decisions taken during implementation, gotchas encountered, qualitative read of the generated output, follow-up items (Atlas backfill? auto-discovery? extended-thinking-Sonnet better?).
- **Test impact:** none.
- **Reversibility:** N/A (purely additive history).
- **Acceptance:** entry exists; readable; captures what a future session needs to know to extend or revert this work.

## Plan summary

12 work units. Estimated 1-3 sessions for server cycle. W4 + W10 are the load-bearing units — everything else is connective tissue. If W10 passes, the webapp cycle begins (separate plan, separate worktree).

## W10 acceptance — closed 2026-05-12

Ran the seeded pair on prod data (Running entity id 59, 18 mentions; Atlas entity id 3, 17 mentions). Narrative panel reads as faithful and restrained — no invented events, citations track real entries, voice stays in third person without emotional extrapolation. User: *"it looks good enough for an experiment."* Kill criteria did not fire.

Two production bugs surfaced and were fixed before the read passed (see `journal/260512-*.md`):

1. **FTS fallback used a phantom `limit=` kwarg** on `_SearchMixin.search_text` (`2089531`). Caught because the unit-test fake had a permissive signature. Regression test now wires the real `SQLiteEntryRepository` end-to-end.
2. **Embedder input exceeded 8192 tokens** (this commit). `_join_narrative_text` was concatenating prose plus every citation's `quote` text — and citation `quote` is the whole wrapped entry for `source: "content"` documents. Fix: text-only segments contribute to the embed input, plus a 32k-char belt-and-suspenders cap. Two regression tests added.

## Webapp cycle handoff

Server side is done. Webapp UI is a separate worktree on the `webapp/` repo. Scope:

1. **List view** at `/storylines` mirroring `EntryListView.vue`
2. **Detail view** at `/storylines/:id` with the two-panel layout
3. **Pinia store** mirroring `entries.ts`
4. **API client** at `src/api/storylines.ts` typed against the server's response shapes
5. **Segment renderer** — a small Vue component that renders `{kind: "text"}` runs and `<RouterLink :to="/entries/${entry_id}">` for `{kind: "citation"}` segments. No markdown library needed.

Webapp-cycle follow-ups:

1. ~~**Citation granularity.**~~ **Closed 2026-05-12.** The narrator now builds one `source="text"` document per entry; citations carry the `char_location` shape and `cited_text` is a sentence-level excerpt. The webapp `<details>` disclosure that hid bloated quotes can be simplified or removed in a webapp-side cleanup. See `journal/260512-storylines-cite-text.md`.
2. **Entity backfill.** Storyline 3 (Running) and 4 (Atlas) on prod used entity IDs 513 and 511 (new entities, not the 59/3 from the original recon — the entity layer reshuffled). With fewer than 3 entity mentions each in window, FTS fallback fired and pulled most of the corpus. A `journal extract-entities --stale-only` pass might reduce dependence on FTS but isn't required for the feature to work.

## Related docs

- `docs/roadmap.md` — index
- `docs/storylines.md` — feature reference
- `docs/entity-tracking.md` — substrate (mentions, aliases, dedup)
- `docs/mood-scoring.md` — precedent for LLM-output baked into service data
- `docs/jobs.md` — job runner this work plugs into
- `docs/architecture.md` — updated 2026-05-12 to name storylines as an in-service LLM-comprehension feature
- `.engineering-team/storylines/recon-backend.md`, `recon-frontend-preview.md`, `recon-research.md`, `recon-product.md`, `evaluation-report.md` — Phase 1 reconnaissance backing this plan
