# 260512 — Storylines server spike (W1–W12)

Server-side shipped end-to-end for the storylines feature
([`docs/storylines-plan.md`](../docs/storylines-plan.md)). The
implementation covers W1–W12 minus the post-deploy W10 acceptance
gate (manual seed + qualitative read against production data) and
the webapp UI, which is a separate worktree.

## What shipped

Five commits on `worktree-eng-storylines`:

1. **`4b65fbf` foundation** — migration 0027 (`storylines` +
   `storyline_panels` tables), `SQLiteStorylineRepository`,
   dataclasses on `models.py`, the dated-mentions query on
   `_MentionsMixin`, segments helpers.
2. **`92ccb18` generation service** — Opus narrator + Haiku glue
   providers, `StorylineGenerationService` orchestrator with FTS
   fallback for sparse-mention storylines.
3. **`7530f45` integration layer** — job worker, extension
   classifier (entity overlap → surface-form + LLM decider →
   no_match), ingestion hook in `_queue_post_ingestion_jobs`.
4. **`bf78908` REST API + MCP tools** — read endpoints in
   `api/storylines.py`, write endpoints in `api/ingestion.py`,
   four MCP tools.
5. **`0190a28` bootstrap wiring + config** — opt-in via
   `ANTHROPIC_API_KEY`; six new env vars with sensible defaults.

## Decisions taken during implementation

* **Storyline panels are split across two rows**, not stored as
  one JSON blob. Two reasons: glue iteration doesn't rewrite the
  narrative (cheaper LLM cost on prompt-tuning loops), and the
  webapp can fetch panels independently if it wants to load the
  cheaper curation panel first.
* **Segments stay as plain dicts, not dataclasses.** They
  round-trip through JSON to the wire and SQLite — adding a
  dataclass layer would add an encode/decode hop with no win.
  Module `services/storylines/segments.py` has helpers
  (`text_segment`, `citation_segment`, `collect_source_entry_ids`)
  so producers don't reinvent the keys.
* **FTS fallback fires at `< 3` entity-mention threshold.** Below
  that, the service searches FTS5 for the entity's `canonical_name`
  in the date window, deduplicates against the entity-mention
  set, and synthesizes a context snippet (±120 chars) as the
  curation panel's verbatim quote. This catches pronominal
  references the entity extractor misses ("my son", "he"), at the
  cost of a noisier excerpt list. Documented as a robustness layer,
  not the primary path.
* **Extension classifier records `last_extension_check_at` on
  every storyline it inspects**, not just the matches. UI can show
  "last checked", which is more useful than "last extended".
* **The extension-check hook only fires when both the classifier
  is wired AND a `user_id` is known.** This keeps the existing
  ingestion paths untouched on servers without storylines or with
  service ingestion (no user attribution).
* **Architecture doc updated honestly.** `docs/architecture.md`
  used to say "no model reads, interprets, or summarizes your
  journal entries during search." Storylines breaks that, so the
  doc now names mood scoring + storylines as the two features
  that bake LLM comprehension into stored data, and explains the
  Citations-API grounding so a reader knows pointers are parsed
  not generated.

## Gotchas hit during implementation

* **`from __future__ import annotations` + `Context` in MCP
  tools.** The existing `entities.py` and friends don't use
  `from __future__ import annotations`, so their `Context` import
  is "used at runtime" as far as ruff is concerned. I added the
  future import in my first draft, and ruff's TC002 flagged the
  Context import. Removed the future import; matched the existing
  pattern.
* **`entries.word_count NOT NULL`.** Test fixtures need to set it
  explicitly. The repo's `create_entry` path computes it, but raw
  SQL inserts (used in test seeds) must supply it.
* **Migration 0011 already seeds an admin user with id=1.** Test
  fixtures that try to INSERT a user with id=1 hit a UNIQUE
  failure. Drop the seed and use the migration's default.
* **FK CASCADE needs `PRAGMA foreign_keys=ON`.** SQLite defaults to
  off; the test that exercises `storyline_panels` cascade had to
  flip the pragma explicitly. Production sets this elsewhere
  (`db/connection.py`); the test path doesn't.

## What didn't ship in this worktree

* **W10 — qualitative acceptance gate.** Requires running against
  production data + real Anthropic API. Happens post-deploy.
  Expected flow: deploy this branch, call
  `journal_create_storyline(entity_id=59, name="Running")` and
  `journal_create_storyline(entity_id=3, name="Atlas")`, then
  `journal_regenerate_storyline(…)` for each, then read the
  output. If the narrative reads as fabricated or generic after
  three prompt iterations, kill criterion #1 fires (see
  `docs/storylines-plan.md` §Kill criteria).
* **Webapp UI.** Separate worktree (eng-storylines-webapp). Tracks
  the two-panel layout, the storylines list view, the citation
  RouterLink renderer, and a Pinia store + API client.
* **Auto-discovery of storylines.** Out of scope per the plan's
  non-goals — covered by a future workstream once the rendering
  shows it's worth keeping.
* **Coreference resolution.** Pronominal references to entities
  ("my son" → Atlas) remain unresolved. The FTS fallback is the
  spike's workaround; the real fix is Tier 3 #8 on the roadmap.

## Stats

* 6 new files in `src/`: migration, repository, segments,
  service, narrator provider, glue provider, decider provider,
  classifier, two workers, API module, MCP tool module.
* 4 new test files: 72 new unit tests.
* Final test count: 2365 unit tests pass; integration tests
  deselected (no Chroma running locally for this session).
* `ruff check src/journal/ tests/` is clean.

## Open follow-ups

* **Prompt iteration after W10.** The narrative system prompt
  needs real-data eyes. The plan permits up to 3 iterations
  before invoking kill criterion #1.
* **Atlas entity backfill consideration.** Atlas has 17 person-
  type entity mentions in the corpus (more than the FTS-fallback
  threshold), so the seed should work. But a global
  `journal extract-entities --stale-only` pass would close gaps
  from pre-2026-04-13 entries; deferred to a follow-up
  workstream.
* **Cost telemetry.** The `raw_usage` field on `NarrativeResult` /
  `GlueResult` captures cache-hit metrics from the Anthropic
  response, but the worker doesn't yet log them. Worth adding so
  W10 reveals the actual cost per regen.
* **Sonnet 4.6 comparator.** Dropped from the spike at the user's
  explicit request. If W10 shows narrative quality issues, the
  follow-up workstream gets to decide whether the extended-
  thinking model improves grounding.
