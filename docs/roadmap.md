# Journal Tool — Consolidated Roadmap

**Status:** active. **Last updated:** 2026-05-10 (W14 fitness docs shipped — Tier 1 #1 server-side
complete, only W15 webapp remaining). **Supersedes:**
[`archive/phase-2-brief.md`](./archive/phase-2-brief.md) (2026-03-23) and
`webapp/docs/archive/future-features.md`.
Pulls in all outstanding TODOs from memory files and recent journal entries.

This is the single source of truth for "what do we work on next". When you finish an item, cross it out here; when you
defer one, move it to the "Deferred / known gaps" section with a reason.

## Active planning docs

Live plans linked here so they don't become shadow inventory. For each, the `Status:` header at
the top of the linked doc tells you whether it's active, closed, or superseded.

- [`archive/tier-1-plan.md`](./archive/tier-1-plan.md) — **closed 2026-05-09**, all four Tier 1 items done
  (Items 2/3a/3b/4 shipped 2026-04-11; Item 3c shipped 2026-04-21 with renamed endpoints;
  Item 1 de facto complete via the entity-quality workstream). Kept as a record of decisions.
- [`refactor-round-3.md`](./refactor-round-3.md) — current entry point for refactor work.
  Supersedes [`archive/code-quality-refactor-plan.md`](./archive/code-quality-refactor-plan.md) (v2, closed)
  and [`archive/refactor-follow-ups.md`](./archive/refactor-follow-ups.md) (closed). Most recent shipped
  units: api.py / repository / mcp_server / auth_api / ingestion / cli splits and
  item-6 exceptions batch 1 (all by 2026-05-08).
  - [`archive/refactor-repository-plan.md`](./archive/refactor-repository-plan.md) — child plan, Recommendation 3 (closed 2026-05-07).
  - [`archive/refactor-item-6-exceptions-plan.md`](./archive/refactor-item-6-exceptions-plan.md) — child plan, § B (closed 2026-05-08; all three items dispositioned).
  - [`archive/refactor-mcp-server-plan.md`](./archive/refactor-mcp-server-plan.md) — child plan, Recommendation 2 (closed; split landed 2026-05-07).
- [`security-roadmap.md`](./security-roadmap.md) — multi-tier security hardening. Tier 1
  completed 2026-04-15; later tiers remain.
- [`fitness-integration-plan.md`](./fitness-integration-plan.md) — fitness-tracker
  ingestion design (open questions resolved 2026-05-08). See also
  [`fitness-schema.md`](./fitness-schema.md) (concrete schema),
  [`fitness-tier-plan.md`](./fitness-tier-plan.md) (execution sequencing — 15
  work units across 5 phases; W1–W14 shipped 2026-05-09 → 2026-05-10; W15 is
  webapp-only and tracked separately),
  [`fitness-pipeline.md`](./fitness-pipeline.md) (engineer-facing data-flow
  overview), and [`fitness-operations.md`](./fitness-operations.md) (operator
  runbook for re-auth, backfill, and troubleshooting). Promoted to Tier 1 below.
- [`code-quality-principles.md`](./code-quality-principles.md) — standing rules referenced
  by the refactor docs.
- [`mood-scoring.md`](./mood-scoring.md) — pipeline reference. Note: mood scoring is now
  **on by default** (opt-out via `JOURNAL_ENABLE_MOOD_SCORING=false`). Toggleable at runtime
  from the webapp's Settings page.
- [`search.md`](./search.md), [`transcription-providers.md`](./transcription-providers.md) —
  reference docs for the hybrid search and multi-provider transcription stacks (both shipped
  2026-05-01).

Scope is cross-cutting: some items are pure backend (`journal-server`), some pure frontend (`journal-webapp`), many touch
both. Each item is tagged with `[server]`, `[webapp]`, or `[both]`.

---

## Ordering rationale

Items are grouped by **readiness**, not by a linear "Phase 2 / Phase 3 / Phase 4" numbering, because the earlier
phase-based docs kept drifting out of sync with actual sequencing. The three tiers below reflect the real blockers:

1. **Tier 1 — Ready to start now.** No upstream dependency. Pick any.
2. **Tier 2 — Blocked on real data or on a Tier 1 item.** Can't meaningfully start until the corpus grows or until the
   dependency ships.
3. **Tier 3 — Polish and research.** Valuable but not urgent.

Inside each tier, items are loosely ordered by "what the user would get most use out of on a single-user personal journal
with a small corpus".

---

## Tier 1 — Ready to start now

> The four original Tier 1 items (entity-extraction first run, `/health`, dashboard, search
> UI) all closed by 2026-04-21 — see [`archive/tier-1-plan.md`](./archive/tier-1-plan.md) and
> Closed items 13–15, 28 below for the shipped detail. T1.1.b dedup-threshold tuning (`0.88`)
> was never executed but no work was blocked. The next item meeting the Tier 1 criterion
> (no upstream dependency, ready to start) is **fitness integration**.

### 1. Fitness integration `[server]` — server-side complete, W15 webapp pending

Ingestion pipeline for fitness-tracker data (Strava + Garmin Connect). Decisions in
[`fitness-integration-plan.md`](./fitness-integration-plan.md), schema in
[`fitness-schema.md`](./fitness-schema.md), execution sequencing in
[`fitness-tier-plan.md`](./fitness-tier-plan.md), engineer-facing data flow in
[`fitness-pipeline.md`](./fitness-pipeline.md), operator runbook in
[`fitness-operations.md`](./fitness-operations.md). The first live exercise
against real credentials is captured in
`journal/260510-fitness-first-fetch.md`.

**Status:** W1–W14 of 15 work units shipped 2026-05-09 → 2026-05-10. End-to-end
pipeline in production: schema (W1–W3), Strava + Garmin providers (W4/W5),
fetch service with auth-state machine (W6), normalize service (W7), job workers
(W8), REST endpoints (W9), MCP tools (W10), CLI re-auth + sync (W11), health
endpoint extension (W12), backfill orchestrator + first live smoke (W13),
operator + engineer documentation (W14). Production currently holds 80 Strava
activities + 80 Garmin activities + 129 Garmin daily wellness rows for
2026-01-07 → 2026-05-09.

**Next:** W15 — webapp views for fitness data (sync-status panel, auth-broken
banner, charts grounded in real backfilled data, Strava↔Garmin distinct-workout
reconciliation). Lives in the `webapp/` repo, not `server/`. Three small
optional follow-ups documented in `journal/260510-fitness-first-fetch.md` are
deferred (the `--code <code>` CLI flag, the W7 watermark fix, and an explicit
`Rowing → other` activity-type map entry).

**Why Tier 1:** independent of the journal-text pipeline, opens a new analytical surface
(mood vs activity correlation), and the planning doc is the most recently added active
workstream.

---

## Tier 2 — Blocked on data, Tier 1, or both

### 2. Entity graph visualization view `[webapp]`

New `/graph` route using **Cytoscape.js** (library bake-off already happened — see
`journal-webapp/journal/260411-auth-header-overlay-cache-entity-views.md`). Renders the entity-and-relationship graph as
an interactive force- directed layout.

**Features**

1. Node colours by entity type (person / place / activity / organization / topic / other)
2. Click a node → side panel showing that entity's canonical name, aliases, mentions, incoming/outgoing relationships,
   and the list of entries it appears in (reuse `EntityDetailView` data)
3. Edge labels showing predicates
4. Filter bar: entity-type checkboxes, date-range slider, min confidence threshold
5. Search box to focus on a specific named entity

**Blocker cleared 2026-05-09:** the original session notes set a "at least 30–50 entities and a handful of
relationships" threshold; prod now has **475 entities and 895 relationships** (plus 222 aliases, 50 merge-history rows,
34 open merge candidates). Item is unblocked — only remaining work is the webapp implementation. Consider promoting
to Tier 1 next time you pick this up.

**Source:** `webapp/journal/260411-auth-header-overlay-cache-entity-views.md` "Deferred to Phase 2";
`server/docs/entity-tracking.md`.

---

### 3. LadybugDB graph-backend experiment `[server]`

Swap in a second `EntityStore` implementation backed by LadybugDB (Kuzu's successor) while keeping SQLite as the
fallback. The `EntityStore` Protocol in `src/journal/entitystore/protocol.py` (re-exported from `entitystore/store.py`)
already exists specifically to make this pluggable — the experiment is meant to be a zero-architectural-risk bet.

**Goals**

1. Validate that the Protocol abstraction actually holds up when a second backend is plugged in — any leakage of SQLite
   assumptions is a design bug to fix.
2. Benchmark: how much faster is a multi-hop relationship query (e.g. "who does Atlas know, and where have they been
   together?") against a native graph backend vs SQLite JOINs?
3. Evaluate operational cost — LadybugDB adds another moving piece. Is the query speedup worth the ops overhead on a
   single-user tool?

**Decision point:** once the benchmark is run, either commit to graph DB as the default (feature-flagged, config-driven)
or stay on SQLite and delete the experimental branch. Do not ship two backends as permanent production paths.

**Blocker:** ~~needs real entity data~~ **cleared 2026-05-09** — prod has 475 entities / 895 relationships, plenty
of graph structure to benchmark against. Real blocker now is bandwidth: this is a research bet, not a user-facing
feature.

**Source:** `docs/entity-tracking.md` "Storage-agnostic Protocol",
`server/journal/260411-security-ocr-context-entity-tracking.md` "Deferred to a future session".

---

> **Tier 2 closed items removed 2026-05-09:** the previously-listed manual "entity extraction
> trigger UI" (superseded by auto-reextraction on save — Closed item 23) and "entity
> management combine/rename/delete" (shipped 2026-04-12 — Closed item 21) had full duplicated
> detail blocks here; deleted to avoid drift.

---

## Tier 3 — Polish and research

> Multi-page ingestion UI (previously listed here) is shipped as part of `/entries/new` —
> see Closed item 17.

### 4. Voice note playback `[both]`

Audio player alongside transcript in `EntryDetailView` for voice entries. Needs:

1. A `GET /api/entries/{id}/audio` endpoint that serves the original audio file. `source_files` already stores the path.
2. Frontend `<audio>` element with transcript scrubbing (timestamp markers if Whisper gave us word-level timestamps,
   otherwise simple playback).

**Source:** `journal-webapp/docs/archive/future-features.md` "Phase 4".

---

### 5. Export `[both]`

Export entries (or a filtered subset) to Markdown, PDF, or JSON. `GET /api/export?format=markdown&from=...&to=...` with
server-side rendering. Button on `EntryListView` above the filtered list.

**Source:** `journal-webapp/docs/archive/future-features.md` "Phase 5".

---

### 6. Semantic-chunker percentile tuning `[server]`

`SemanticChunker` ships with `boundary_percentile=25` and `decisive_percentile=10` as defaults. These were picked by gut
feel because the user had 2 real entries at the time — meaningless stats. The 20-entry threshold the original session
notes called for is **met three times over** (prod corpus: 69 entries / 500 chunks as of 2026-05-09); ready to run.

Open questions to answer during tuning:

1. Does raising boundary_percentile to 30/35 produce more coherent chunks or just fewer chunks?
2. How do ratios compare between `fixed` (150/40) and `semantic` (25/10)? The user flipped the default to `semantic` in
   commit `d1343ac` — verify that decision still holds at 69 entries.
3. Consider building a golden-query retrieval set against the current corpus.

**Source:** `server/journal/260410-semantic-chunking.md` "What's deferred to the next session".

---

### 7. Predicate normalisation for the entity graph `[server]`

Relationship predicates (`met`, `saw`, `caught up with`, `had lunch with`) are free-text. Over time they drift and a
single underlying relationship gets expressed as N different predicates. A normalisation pass — small clustering LLM call
that maps free-text predicates to a canonical set — keeps the graph queryable.

**Blocker:** needs real data to see the drift. Don't preempt the drift with a hand-crafted mapping; let it accumulate,
then cluster.

**Source:** `docs/entity-tracking.md` "Known risks",
`journal-server/journal/260411-security-ocr-context-entity-tracking.md` "Deferred to a future session".

---

### 8. Coreference resolution `[server]`

Currently only first-person (`I`, `me`, `my`) is resolved, via the `JOURNAL_AUTHOR_NAME` config. Pronouns like `we`,
`she`, `him`, `they` are not resolved — the extractor sees them as strings with no entity link, so "she told me..."
contributes nothing to the graph.

**Approach:** most likely a second LLM pass over the entry that's given the already-extracted entity list and asked to
fill in pronoun references. Expensive if done every run; cheap if done only as part of `extract-entities --stale-only`.

**Source:** `journal-server/journal/260411-security-ocr-context-entity-tracking.md` "Deferred to a future session".

---

### 9. OCR context priming empirical evaluation `[server]`

OCR context priming shipped 2026-04-11 but was never measured against a real baseline. Run the same handwritten sample
through the OCR provider with and without `OCR_CONTEXT_DIR` set and eyeball the proper-noun accuracy delta.

**Provider note (2026-05-09):** prod runs `OCR_DUAL_PASS=true` with the dual-pass factory
(`_build_dual_pass_provider`) wiring Anthropic Claude Opus 4.6 as primary and Google Gemini 2.5 Pro as secondary; the
runtime `ocr_provider=gemini` setting only takes effect if dual-pass is turned off. The evaluation should cover **both**
providers since the context block primes both. `OCR_CONTEXT_DIR` markdown also primes Whisper / Gemini transcription via
`services/transcription_context.py` (closed item 38), so any glossary growth has a side-effect on transcription
accuracy too.

**If no delta:** decide whether to keep the feature on (cache-ttl cost is minimal once the system text is above the cache
minimum) or rip it out.

**Source:** `server/journal/260411-security-ocr-context-entity-tracking.md` "Second-session checklist".

---

### 10. `FixedTokenChunker` sizing review `[server]`

Observed on 2026-04-11 via the webapp chunks overlay on entry 7 (277 words, 2 pages, date 2026-02-15): the current
`FixedTokenChunker(max_tokens=150, overlap_tokens=40)` produces **5 chunks** for that entry, which is over-fragmented.
Three smells from the overlay:

1. **Tail orphan** — a 12-word closing sentence ("I assume in a few years he will have different interests.") got ejected
   into its own chunk, severed from the parenting paragraph it belongs to. Orphan chunks embed as standalone thoughts
   with no context and hurt retrieval quality.
2. **Split mid-theme** — the marriage/parenting reflection gets cut across two chunks, so a search for "marriage" only
   hits half the content.
3. **Below recommended size** — `text-embedding-3-large` docs recommend 256-512 tokens per chunk for best retrieval.
   150-token chunks leave retrieval quality on the table.

**Action:** sweep `(max_tokens, overlap_tokens)` over the existing corpus using the `journal eval-chunking` CLI.
Candidate points: `(150,40)` baseline, `(200,40)`, `(250,30)`, `(300,25)`. Expect ~250/30 to produce 2-3 chunks for a
277-word entry with clean paragraph boundaries. Commit the winner to `config.py` and update the regression test
`test_ingest_multi_page_packs_efficiently` (currently locks the old 2-chunk count at `max_tokens=150`).

**Related:** #6 (semantic chunker percentile tuning). Both sweeps are worth doing in the same session — the corpus is
now at 69 entries (≥ 3× the original gating threshold), so numbers are meaningful.

**Source:** conversation with Claude, 2026-04-11, reviewing the chunks overlay on entry 7.

---

### 11. Grow OCR glossary `[server]`

`OCR_CONTEXT_DIR` glossary growth is worth doing for two independent reasons: OCR accuracy on proper nouns, and (when
running an Anthropic primary) prompt-caching economics.

**Status note (2026-05-09):** prod runs `OCR_DUAL_PASS=true`, so Anthropic Claude Opus 4.6 is the primary and Gemini
2.5 Pro is the secondary (the runtime `ocr_provider=gemini` setting is ignored under dual-pass). Gemini has its own
caching mechanics (Vertex/Gemini API context-cache vs. Anthropic's `cache_control`); the 4,096-token Anthropic
threshold called out in this item still applies because Anthropic is the dual-pass primary. The boot-time warning
quoted below fires from the Anthropic adapter (`providers/ocr.py`) when it's loaded.

```
OCR system text is N tokens (approx) — below the 4096-token
cache minimum for claude-opus-4-6. cache_control will be silently
ignored and every request will pay full input price.
```

**Action:**

1. Grow the context directory organically as you add proper nouns — drives accuracy on both providers.
2. **Since Anthropic is the dual-pass primary**, grow the composed system text past 4,096 tokens (≈ 15-20 KB of
   markdown) to clear the cache-minimum bar. Below that bar `cache_control` is silently ignored on Anthropic and every
   request pays full input price.
3. Genuine content only — both for accuracy and for caching — rather than padding with filler.

**Cost pressure is low** — at ~1 page/day the uncached system block is cents per month even on the Anthropic adapter.
This is a "do it when you have more proper nouns to add" item, not urgent. If you decide _not_ to, consider adding a
`warning_suppressed` flag to silence the Anthropic repeat warning so it doesn't numb you to other cache-related issues.

**Related:** #9 (glossary accuracy evaluation across both providers). Do both in the same session.

**Source:** conversation with Claude, 2026-04-11; provider-pivot notes 2026-05-01 (multi-provider transcription) and the
audit on 2026-05-09.

---

## Deferred / known gaps (not planned, but tracked)

### D1. Legacy multipage entries with the old `\n\n` page join `[server]`

Entries ingested before the 2026-04-11 chunking fix have `"\n\n".join(page_texts)` baked into their `final_text`. Running
`rechunk_entries` alone doesn't help — the separator is part of the chunker input, not a parameter.

Fix options:

1. Opt-in script that rebuilds `final_text` for legacy multipage entries from `entry_pages.raw_text` with the new
   separator, then rechunks. Destructive to any user edits to `final_text`, so it must be opt-in.
2. Ignore — most legacy entries are single-page text/voice; only the early multi-page photo entries are candidates and
   none have surfaced as problematic. Re-ingest from scratch if any multipage pathologies appear.

**Status:** still going with option 2 as of 2026-05-09 (prod has 69 entries — 29 photo, 28 text, 9 voice, 3 imported;
no observed legacy-multipage pathologies). Promote to Tier 2 if affected entries appear.

---

### D2. No entity chips cache invalidation on entry save `[webapp]`

When the user saves an edited entry, the entity chip strip in `EntryDetailView` may continue to
show entities extracted from the _pre-edit_ text until a full page reload.

Now that auto-reextraction-on-save shipped (Closed item 23), the stale-chips condition is more
likely to surface as a real UX bug rather than a benign display lag. Worth revisiting — promote
to Tier 2 if the user observes it.

**Source:** `journal-webapp/journal/260411-auth-header-overlay-cache-entity-views.md` "Risks and known gaps".

---

### D3. Entity list pagination state doesn't survive navigation `[webapp]`

If you click an entity, view its detail, then hit back to `/entities`, you land on page 1 not the page you came from.
Minor UX — fix is to persist `currentParams.offset` across mount cycles or via route query params.

**Source:** `journal-webapp/journal/260411-auth-header-overlay-cache-entity-views.md` "Risks and known gaps".

---

### D4. Provider zero-data-retention agreements (policy, not code) `[ops]`

Anthropic and OpenAI data retention is a policy discussion, not a code change. Signing a ZDR addendum removes
provider-side retention as a privacy concern for journal content.

**Source:** `docs/security.md`; `journal-server/journal/260411-security-ocr-context-entity-tracking.md`.

---

### D5. TLS / reverse proxy `[ops]`

The server currently binds to `127.0.0.1:8400` per the 2026-04-11 security hardening. A reverse proxy (caddy or nginx) on
the VM terminates TLS and fronts the server when exposing it beyond loopback. Out of scope for the codebase; a deployment
concern.

**Source:** `journal-server/journal/260411-security-ocr-context-entity-tracking.md` "Deferred to a future session".

---

### D6. Encrypted backup for journal data `[ops]`

`/srv/media/config/journal` (SQLite DB, ChromaDB data, any source images/audio) should be backed up with encryption at
rest to protect against disk loss. Out of scope for the codebase.

**Source:** `journal-server/journal/260411-security-ocr-context-entity-tracking.md` "Deferred to a future session".

---

### D7. ~~Webapp "Phase 2: Authentication" from `future-features.md`~~ `[webapp]` — ✅ resolved 2026-04-15

What was deferred here is now done. The bearer-token shim from the 2026-04-11 security session
was followed by the **multi-user auth + tier-1 data isolation** workstream that landed
2026-04-14 → 2026-04-15 (Closed item 25). The webapp now has registration, email-verification,
hashed sessions, per-user data isolation, an Admin/Settings split, and an API-keys view at
`/api-keys`. See [`security-roadmap.md`](./security-roadmap.md) (Tier 1 closed; later tiers
remain).

---

## Closed — shipped between 2026-03-22 and 2026-04-12 (recap)

Included so we don't accidentally re-surface these as TODOs.

1. Initial implementation (CLI, MCP server, SQLite + ChromaDB, ingestion, query routing)
2. Multi-page OCR ingestion (server-side)
3. REST API mode (SSE + JSON endpoints)
4. Semantic chunker with sentence splitting, adaptive overlap, and `eval-chunking` CLI
5. Rechunk CLI and backfill script
6. Chunk/token overlay in webapp (with cache invalidation on save)
7. Live diff editor in webapp
8. Delete entry endpoint and UI
9. Entity tracking backend (extraction service, dedup pipeline, Protocol-based storage) and webapp read-only list/detail
   views
10. OCR context priming (`OCR_CONTEXT_DIR`, prompt caching, anti-hallucination instructions)
11. Security hardening (bearer auth, fail-closed startup, DNS rebinding protection always on, loopback-only bind, SSRF
    guard, `chmod 600`, `docs/security.md`)
12. Multi-page chunking page-join fix (277→5 chunks mystery closed)
13. Search UI (Tier 1 item 4) — `GET /api/search` backend with semantic + keyword modes, FTS5 `snippet()` highlights,
    chunk offsets on `ChunkMatch`, and the webapp `/search` view with `?chunk=N` deep-link scroll-into-view
14. `/health` endpoint (Tier 1 item 2) — `GET /health` with ingestion stats, in-process query latency histogram, and
    per-component liveness checks; bearer-auth-exempt on loopback; `journal health` CLI prints the same payload
15. Dashboard 3a (Tier 1 item 3a) — `GET /api/dashboard/writing-stats` and webapp DashboardView at `/` (Option B routing
    — entries list moved to `/entries`). Chart.js 4 line charts for writing frequency and word count per
    week/month/quarter/year
16. Dashboard 3b (Tier 1 item 3b) — per-entry mood scoring via `config/mood-dimensions.toml` (bipolar + unipolar facets),
    `MoodScorer` Protocol + Anthropic Sonnet 4.5 adapter via tool use, `replace_mood_scores` with sparse storage, opt-in
    `JOURNAL_ENABLE_MOOD_SCORING` config flag, `journal backfill-mood` CLI with stale-only / force / prune-retired /
    dry-run modes, `GET /api/dashboard/mood-dimensions` and `GET /api/dashboard/mood-trends` endpoints, and a mood chart
    in the dashboard. See `docs/mood-scoring.md`
17. Entry creation from webapp (2026-04-12) — three new REST endpoints: `POST /api/entries/ingest/text` (sync JSON),
    `POST /api/entries/ingest/file` (sync multipart .md/.txt), `POST /api/entries/ingest/images` (async multipart,
    job-based OCR). `IngestionService.ingest_text()` for text/file entries. `JobRunner` extended with `ingest_images` and
    `mood_score_entry` job types. Migration 0007 relaxes `source_type` CHECK. Webapp: `/entries/new` with Write Entry,
    Import File, Upload Images tabs. Supersedes the previously-planned multi-page ingestion UI.
18. Image upload bug fixes (2026-04-12) — nginx `client_max_body_size 20m` for the `/api/` proxy, `apiFetch` Content-Type
    fix for `FormData` uploads, error message extraction (`body.error` fallback), duplicate error display removed,
    dismiss button + clear-on-tab-switch for error banner.
19. OCR date extraction + date editing (2026-04-12) — new `date_extraction` module parses dates from OCR text (named
    months, ISO, DD/MM/YYYY). `PATCH /api/entries/{id}` extended to accept `entry_date` alongside `final_text`. Webapp:
    clickable date heading in EntryDetailView with inline date picker.
20. OCR uncertainty highlighting (2026-04-11) — Review toggle in EntryDetailView with `⟪/⟫` sentinel
    parsing, `uncertain_spans` DB storage (migration 0005), yellow highlights on Original OCR panel.
    UX improved 2026-04-12: always clickable, info banner when no spans exist.
21. Entity management combine/rename/delete + merge review (2026-04-12) — merge, rename, delete, and merge review for entities. Migration 0008
    adds `entity_merge_history` and `entity_merge_candidates` tables. Six new REST endpoints. Webapp entity detail view
    has edit/delete, list view has multi-select merge + merge review section. Fixed two tuple-unpack bugs in entity list
    endpoints.
22. Mobile layout fix (2026-04-12) — corrected text panel was invisible on small screens due to absolute-positioned
    children in a flex-col layout. Both editor sections now have `min-h-[300px]` on mobile.

## Closed — shipped between 2026-04-13 and 2026-05-09

Grouped by workstream rather than by commit; see the linked journal entries for detail.

23. **Auto-entity-reextraction on save (2026-04-13)** — entity extraction runs automatically
    as part of the save pipeline (`server/journal/260413-auto-entity-reextraction-on-save.md`).
    Supersedes the originally-planned manual "extraction trigger UI" (no longer needed).
24. **Mood-scoring deployment fix + default reversal (2026-04-13)** — frontend mood chart
    confirmed live (was already shipped on 2026-04-11), and `JOURNAL_ENABLE_MOOD_SCORING`
    flipped to default `true` (`config.py:262`). Now toggleable at runtime from Settings.
25. **Multi-user auth + tier-1 data isolation (2026-04-14 → 2026-04-15)** — per-user data
    isolation throughout the schema, hashed sessions, follow-up bugfixes for verification
    spinner flicker and stale view-mode state. Origin doc:
    [`security-roadmap.md`](./security-roadmap.md) (Tier 1 closed 2026-04-15). Webapp side:
    `webapp/journal/260415-multi-tenant-bugfixes.md`.
26. **Source-type taxonomy rename (2026-04-15)** — `webapp/journal/260415-rename-source-type-taxonomy.md`.
27. **Search UX improvements (2026-04-15, 2026-05-01)** — quick-pick presets, chronological
    sort, spinner-on-search, "All time" preset normalisation.
28. **Unified Dashboard expansion (2026-04-20 → 2026-04-21)** — Insights page merged into
    `/`. Five new charts beyond 3a/3b: entity-trends multi-line, entity-distribution
    doughnut, calendar heatmap (CSS grid), mood-entity correlation, word-count distribution.
    See `webapp/journal/260421-unified-dashboard-and-new-charts.md`. Sub-epic 3c of the
    original Tier 1 dashboard item.
29. **Bell rehydration fix + dashboard chart improvements (2026-04-21)** — webapp polish.
30. **Sticky filters + dashboard drilldown (2026-04-21)** — webapp.
31. **Dynamic dashboard descriptions + heatmap fill (2026-04-22)** — webapp.
32. **Wake lock + voice confidence scoring (2026-04-22)** — `useWakeLock` composable using
    Screen Wake Lock API for long voice recordings; transcription confidence scoring on
    server side. `webapp/journal/260422-wake-lock-and-voice-confidence.md` and
    `server/journal/260422-transcription-confidence-scoring.md`.
33. **Cost estimates + editable API pricing (2026-04-23)** — editable pricing table
    (`webapp/journal/260423-cost-estimates-and-pricing.md`); ingestion job results enriched
    with token/cost data. Plus Dockerfile `uv run` removed from boot path.
34. **Job History improvements (2026-04-23 → 2026-05-03)** — color-coded type badges, raw
    params popovers, polish, tweaks at `JobHistoryView.vue`.
35. **Pushover notification stack (2026-04-23 → 2026-04-30)** — `PushoverNotificationService`
    with per-user creds, six notification topics, and a `health_poll.py` daemon thread
    pinging SQLite/Chroma/disk every 5 min. Webapp Pushover settings UI on Admin/Settings.
    Series: `server/journal/260423-pushover-notifications.md`,
    `webapp/journal/260423-pushover-notifications-ui.md`,
    `server/journal/260430-pushover-bullet-format.md`.
36. **Compress / individual-toasts pipeline notifications (2026-04-25 → 2026-04-27)** —
    pipeline-stage toasts collapsed into one Pushover bullet message;
    individual stage toasts in the webapp.
37. **Canonical-name validator + possessive false-positive fix (2026-04-27)** — entity
    extraction quality work on the real corpus.
38. **Context-driven Whisper priming + date-heading detection (2026-04-28)** —
    `OCR_CONTEXT_DIR` markdown also drives Whisper; Haiku-based heading detector lifts
    leading dates into `# ` markdown headings on `final_text`.
    `server/journal/260428-context-transcription-and-date-headings.md`.
39. **Responsive entry-footer spacing (2026-04-28)** — webapp.
40. **OCR/voice date-extraction fixes (2026-05-04)** — preserve dictated leading dates as
    `entry_date`. `server/journal/260504-fix-voice-date-extraction.md`.
41. **Live reload for file-backed config (2026-05-01)** — three admin-only endpoints
    `POST /api/admin/reload/{ocr-context,transcription-context,mood-dimensions}` to re-read
    configs without server restart. Webapp Admin Server UI surfaces it.
42. **Hybrid search (2026-05-01)** — replaced `mode=keyword|semantic` with a hybrid pipeline
    (BM25 + dense in parallel, RRF k=60 fusion, Claude Haiku listwise rerank of top-30). Mode
    toggle removed from SearchView. Reference: [`search.md`](./search.md);
    `server/journal/260501-hybrid-search.md`.
43. **Multi-provider transcription (2026-05-01)** — `build_transcription_provider()` factory
    composing primary + retry/fallback + shadow wrappers. Gemini 2.5 Pro as alternative
    primary, whisper-1 as fallback, parallel shadow adapter for offline diff evaluation.
    Reference: [`transcription-providers.md`](./transcription-providers.md).
44. **Save-entry Pushover toggles (2026-05-01)** — per-user notification opt-outs.
45. **Strip leading date from body (2026-05-01)** — body cleanup after date promotion.
46. **Settings vs Admin rationalization (2026-05-01)** — split per-user `/settings` from
    system-wide `/admin/*` behind `requiresAdmin`.
47. **Local dev auth runbook (2026-05-03)** — `server/journal/260503-local-dev-auth-runbook.md`.
48. **Mood dimensions overhaul + admin Moods tab (2026-05-05)** — grouped mood toggles,
    admin Moods tab, mood-trend tooltip group chips, `frustration` rendered as inverted
    "calm". Series: `260505-mood-dimension-tweaks.md`, `260505-mood-dimensions-meta-block.md`,
    `webapp/journal/260505-mood-group-tooltips-and-admin-moods-tab.md`.
49. **Entity quality program (2026-05-06 → 2026-05-08)** — large workstream covering:
    aliases CRUD + async re-embed-on-description-edit, casing single source of truth on the
    server (`services/entity_naming.py:smart_title_case`, client title-caser removed),
    soft quarantine + merge-candidate detection, persistent dedup rejection memory,
    past-dismissals audit/undo panel. Series: `260506-entity-aliases-and-reembed-job-slice-a.md`,
    `260506-entity-casing-quarantine-and-merge-candidates.md`,
    `260507-known-entity-context-stage-0.md`,
    `260508-entity-casing-single-source-of-truth.md`,
    `260508-entity-dedup-rejection-memory.md`,
    `webapp/journal/260507-entity-aliases-ui-and-recognition-toasts.md`,
    `webapp/journal/260508-past-dismissals-panel.md`.
50. **Refactor round 3 — module splits (2026-05-07 → 2026-05-08)** — `api.py` →
    domain modules; `db/repository.py` → package; `mcp_server.py` → `mcp_server/` package
    (closed sub-plan); `auth_api` → 6-cluster split; `services/ingestion.py` → per-media
    package; `entitystore/store.py` → mixins + protocol; `cli.py` → command-group package.
    Plus item-6 exceptions batch 1 and shared-connection race fix. See
    [`refactor-round-3.md`](./refactor-round-3.md) for the live tracker.
51. **Entity dedup persistent rejections + per-pair candidates + signature tightening (2026-05-08)** —
    `2a5990c` "Entity dedup: persistent rejections, per-pair candidates, signature tightening (#12)".
52. **Migration 0022 idempotency fix (2026-05-08)** — orphan-tolerant + idempotent on
    partial-failure retry. Validates the migration-testing principle now captured in memory.
53. **Dependabot config (2026-05-08)** — grouped security + minor/patch PR config.
54. **Entry-edit panel swap (2026-05-08)** — Corrected Text on the left in `EntryDetailView`.
55. **Slice C follow-ups (recent webapp)** — docs, Docker healthcheck fix, prettier sweep.
56. **Doc cleanup + plan-hygiene conventions applied retroactively (2026-05-08)** —
    `server/journal/260508-doc-cleanup-and-plan-hygiene.md`.

---

## How to use this doc

1. **Starting a work session?** Read Tier 1 and pick the highest item you have appetite for.
2. **Finished an item?** Move it from Tier 1/2/3 to "Closed" with a one-line summary, plus a journal entry covering the
   details.
3. **Discovered a new item?** Add it to the right tier with a source reference so future-you can find why it matters.
4. **Deferring an item?** Move it to "Deferred / known gaps" with a reason. Items should only sit in Tier 1/2/3 if
   there's intent to ship them.

The task list (TaskCreate/TaskList) and this roadmap are complementary, not redundant:

- **Roadmap:** long-lived, survives sessions, cross-cutting.
- **Task list:** per-session scratch for active work.

When you promote a roadmap item to active work, create a task for it and link back here.
