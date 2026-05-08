# Journal Tool — Consolidated Roadmap

**Status:** active. **Last updated:** 2026-05-09. **Supersedes:**
[`phase-2-brief.md`](./phase-2-brief.md) (2026-03-23) and `journal-webapp/docs/future-features.md`.
Pulls in all outstanding TODOs from the task list, memory files, and recent journal entries.

This is the single source of truth for "what do we work on next". When you finish an item, cross it out here; when you
defer one, move it to the "Deferred / known gaps" section with a reason.

## Active planning docs

Live plans linked here so they don't become shadow inventory. For each, the `Status:` header at
the top of the linked doc tells you whether it's active, closed, or superseded.

- [`tier-1-plan.md`](./tier-1-plan.md) — **closed 2026-05-09**, all four Tier 1 items done
  (Items 2/3a/3b/4 shipped 2026-04-11; Item 3c shipped 2026-04-21 with renamed endpoints;
  Item 1 de facto complete via the entity-quality workstream). Kept as a record of decisions.
- [`refactor-round-3.md`](./refactor-round-3.md) — current entry point for refactor work.
  Supersedes [`code-quality-refactor-plan.md`](./code-quality-refactor-plan.md) (v2, closed)
  and [`refactor-follow-ups.md`](./refactor-follow-ups.md) (closed). Most recent shipped
  units: api.py / repository / mcp_server / auth_api / ingestion / cli splits and
  item-6 exceptions batch 1 (all by 2026-05-08).
  - [`refactor-repository-plan.md`](./refactor-repository-plan.md) — child plan, Recommendation 3 (active).
  - [`refactor-item-6-exceptions-plan.md`](./refactor-item-6-exceptions-plan.md) — child plan, § B (active; batch 1 landed 2026-05-08).
  - [`refactor-mcp-server-plan.md`](./refactor-mcp-server-plan.md) — child plan, Recommendation 2 (closed; split landed 2026-05-07).
- [`security-roadmap.md`](./security-roadmap.md) — multi-tier security hardening. Tier 1
  completed 2026-04-15; later tiers remain.
- [`fitness-integration-plan.md`](./fitness-integration-plan.md) — fitness-tracker
  ingestion design (open questions resolved 2026-05-08). See also
  [`fitness-schema.md`](./fitness-schema.md). Promoted to Tier 1 below.
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

> All four original Tier 1 items (entity-extraction first run, `/health`, dashboard, search UI)
> are now done — see [`tier-1-plan.md`](./tier-1-plan.md) (closed) and the Closed list below.
> The next active item that meets the Tier 1 criterion (no upstream dependency, ready to
> start) is **fitness integration** — see the linked plan for scope.

### 1. ~~First real entity-extraction run~~ `[server]` — ✅ de facto shipped

Entity tables are populated and actively maintained in prod. Downstream features built on
them (auto-reextraction-on-save, entity-distribution / entity-trends charts, the entity
casing / aliases / quarantine / merge-candidate / dedup-rejection workstream, past-dismissals
panel) have all shipped. T1.1.b dedup-threshold tuning (`0.88`) was never executed but no
work was blocked. See `tier-1-plan.md` closeout summary for detail.

---

### 1b. Fitness integration `[server]` — active, planning open questions resolved 2026-05-08

Ingestion pipeline for fitness-tracker data (Garmin / Apple Health). Schema and design
decisions captured in [`fitness-integration-plan.md`](./fitness-integration-plan.md) and
[`fitness-schema.md`](./fitness-schema.md). Sibling journal entry:
`journal/260508-fitness-integration-planning.md`. Implementation has not yet started.

**Why this is now Tier 1:** independent of the journal-text pipeline, opens a new analytical
surface (mood vs activity correlation), and the planning doc is the most recently added
active workstream.

---

### 2. `/health` endpoint `[server]` — ✅ shipped 2026-04-11

A single `GET /health` endpoint on the MCP server exposing operational stats. **Unauthenticated** (decided against bearer
auth — loopback bind means anyone who can reach it already has a shell on the box, and we scrub any field that would leak
query content). See `journal/260411-health-endpoint.md` for the full session notes and `docs/api.md` for the endpoint
contract.

**Shipped:**

1. ✅ `InMemoryStatsCollector` — `src/journal/services/stats.py`, bounded per-type histogram with `record_query` +
   `snapshot`, wired into `QueryService` behind an optional dependency.
2. ✅ `get_ingestion_stats(now)` — repository method aggregating total/last-7d/last-30d counts, by-source-type, avg
   words/chunks, last-ingestion timestamp, per-table row counts.
3. ✅ Provider liveness — `src/journal/services/liveness.py` pings SQLite and ChromaDB and sanity-checks Anthropic/OpenAI
   API keys without burning tokens.
4. ✅ `GET /health` route in `api.py`, exempt from the bearer middleware via a new `exempt_paths` kwarg on
   `BearerTokenMiddleware`.
5. ✅ `journal health [--compact]` CLI subcommand emitting the same payload as JSON for scripted use.

**Deliberately NOT shipped** (plan items that were cut on review):

1. **Most frequent search terms.** The plan listed this as an optional "Query & usage stat". It would leak what the user
   was searching for from an unauthenticated endpoint, and adding it would push us toward tracking raw query strings in
   memory. Omitted — the query stats block carries counts-by-type only.
2. **ChromaDB last-update timestamp** and **SQLite database size (bytes)**. Both are operationally interesting but Chroma
   does not expose a cheap "last write" timestamp and DB bytes requires a separate stat() call. The `row_counts` block is
   the closest proxy and good enough.
3. **Dashboard consumer.** The plan noted that the dashboard (Tier 1 item 3) could reuse the health payload as a data
   source. Not pursued yet — when 3a lands it will add its own dashboard-specific endpoint, and the shared-source idea
   can be revisited if it turns out to duplicate work.

**Source:** `docs/phase-2-brief.md` "Health & Stats Endpoint".

---

### 3. ~~Dashboard view~~ `[both]` — ✅ entirely shipped (3a + 3b on 2026-04-11; 3c on 2026-04-21)

Unified DashboardView at `/` (entries list at `/entries`). Chart.js 4 throughout. Sub-epic 3c
shipped under different endpoint names than originally planned and as a CSS-grid heatmap
rather than `chartjs-chart-matrix` (open question 6 resolved against the plugin).

**Live charts** (all in `webapp/src/views/DashboardView.vue`):

1. ✅ Writing frequency (sub-epic 3a, 2026-04-11)
2. ✅ Word count trend (sub-epic 3a, 2026-04-11)
3. ✅ Mood dimensions with variance bands and grouped/ungrouped toggles
   (sub-epic 3b, backend + frontend 2026-04-11; grouped toggles + admin Moods tab 2026-05-05)
4. ✅ Entity-trends multi-line chart (sub-epic 3c, 2026-04-21)
5. ✅ Entity-distribution doughnut with expand/collapse legend (3c, 2026-04-21)
6. ✅ Calendar heatmap (CSS grid, 3c, 2026-04-21)
7. ✅ Mood-entity correlation chart (bonus, 2026-04-21)
8. ✅ Word-count distribution chart (bonus, 2026-04-21)

**Live backend endpoints** in `src/journal/api/dashboard.py`:

1. ✅ `GET /api/dashboard/writing-stats` (combined entry-count + word-count)
2. ✅ `GET /api/dashboard/mood-dimensions`, `GET /api/dashboard/mood-trends`
3. ✅ `GET /api/dashboard/entity-distribution`, `GET /api/dashboard/entity-trends`
4. ✅ `GET /api/dashboard/calendar-heatmap`
5. ✅ `GET /api/dashboard/mood-entity-correlation`, `GET /api/dashboard/word-count-distribution`

**Mood scoring decision changed:** `JOURNAL_ENABLE_MOOD_SCORING` now **defaults to `true`**
(opt-out via `=false`). Reversal of open question 2 in the original tier-1-plan; happened
during the deployment fix on 2026-04-13 and was made user-toggleable from the Settings page.

**Source:** [`tier-1-plan.md`](./tier-1-plan.md) (closed),
`webapp/journal/260421-unified-dashboard-and-new-charts.md`,
`webapp/journal/260413-mood-scoring-deployment-fix.md`.

---

### 4. Search UI `[both]` — ✅ shipped 2026-04-11

Dedicated webapp `/search` view.

**Backend — shipped 2026-04-11** (see `journal/260411-search-backend.md`):

1. ✅ `GET /api/search?q=...&mode=semantic|keyword&start_date=...&end_date=...&limit=...&offset=...`
2. ✅ `ChunkMatch` now carries `chunk_index`, `char_start`, `char_end` for semantic hits so the frontend can render chunk
   highlights without a second round-trip.
3. ✅ Keyword mode returns FTS5 `snippet()` output with `\x02`/`\x03` marker chars wrapping matched terms.

**Frontend — shipped 2026-04-11** (see `journal-webapp/journal/260411-search-ui.md`):

1. ✅ `/search` route and `SearchView.vue` with query input, mode toggle (semantic default), and date range filter.
2. ✅ Pinia `useSearchStore` preserves query/mode/dates across navigation and surfaces `ApiRequestError` messages
   verbatim.
3. ✅ Results list with FTS5 snippet highlights rendered via `src/utils/searchSnippet.ts` (converts `\x02`/`\x03` marker
   chars to `<mark>` tags with HTML escaping).
4. ✅ Click-through to `EntryDetailView` with `?chunk=N` on semantic hits; `EntryDetailView` reads the param, flips the
   overlay to chunks mode, and `scrollIntoView` on the matching chunk badge.

**Subsequent overhaul (2026-05-01):** the `mode=keyword|semantic` toggle was removed and the
backend replaced with a hybrid pipeline (BM25 + dense + RRF + Haiku rerank). See
[`search.md`](./search.md) and Closed item 42.

**Source:** `journal-webapp/docs/future-features.md` "Phase 3: Search UI" (now obsolete — this roadmap entry is the
record of what actually shipped).

---

## Tier 2 — Blocked on data, Tier 1, or both

### 5. Entity graph visualization view `[webapp]`

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

**Blocker:** Per the entity-tracking session notes, this waits until there are "at least 30–50 entities and a handful of
relationships — anything smaller is a toy, not a useful knowledge graph." So this is blocked on Tier 1 item 1 AND on the
user actually building up a corpus.

**Tasks tracked:** this is task #8 in the task list.

**Source:** `journal-webapp/journal/260411-auth-header-overlay-cache-entity-views.md` "Deferred to Phase 2";
`journal-server/docs/entity-tracking.md`.

---

### 6. LadybugDB graph-backend experiment `[server]`

Swap in a second `EntityStore` implementation backed by LadybugDB (Kuzu's successor) while keeping SQLite as the
fallback. The `EntityStore` Protocol in `src/journal/entitystore/store.py` already exists specifically to make this
pluggable — the experiment is meant to be a zero-architectural-risk bet.

**Goals**

1. Validate that the Protocol abstraction actually holds up when a second backend is plugged in — any leakage of SQLite
   assumptions is a design bug to fix.
2. Benchmark: how much faster is a multi-hop relationship query (e.g. "who does Atlas know, and where have they been
   together?") against a native graph backend vs SQLite JOINs?
3. Evaluate operational cost — LadybugDB adds another moving piece. Is the query speedup worth the ops overhead on a
   single-user tool?

**Decision point:** once the benchmark is run, either commit to graph DB as the default (feature-flagged, config-driven)
or stay on SQLite and delete the experimental branch. Do not ship two backends as permanent production paths.

**Blocker:** needs real entity data (Tier 1 item 1) to benchmark against. A toy dataset doesn't exercise graph traversal
in any meaningful way.

**Tasks tracked:** this is task #7 in the task list.

**Source:** `docs/entity-tracking.md` "Storage-agnostic Protocol",
`journal-server/journal/260411-security-ocr-context-entity-tracking.md` "Deferred to a future session".

---

### 7. ~~Entity extraction trigger UI~~ `[webapp]` — ✅ superseded 2026-04-13

Resolved by **auto-reextraction on save** (`server/journal/260413-auto-entity-reextraction-on-save.md`):
extraction now runs automatically as part of the save pipeline, so a manual trigger button is
no longer needed. Manual extraction is still available via CLI for backfills.

---

### 8. Entity management: combine, rename, delete `[both]` — ✅ shipped 2026-04-12

All four entity management features shipped:

1. ✅ **Combine / merge** — select 2+ entities in the list view via checkboxes, click "Merge selected", pick the survivor
   in a modal. All mentions, relationships, and aliases from absorbed entities are reassigned to the survivor. Merge
   history is recorded in `entity_merge_history` for audit/undo.
2. ✅ **Rename / edit** — edit button on entity detail view opens an inline form for canonical name, entity type, and
   description. `PATCH /api/entities/{id}`.
3. ✅ **Delete** — delete button on entity detail view with `window.confirm()` dialog. `DELETE /api/entities/{id}`
   cascades to mentions, relationships, and aliases.
4. ✅ **Merge review** — extraction service now persists near-miss similarity matches to `entity_merge_candidates` table.
   The entity list view shows a "Possible duplicates to review" banner with accept/dismiss actions per candidate.

**Backend:** Migration 0008 (`entity_merge_history` + `entity_merge_candidates` tables). `EntityStore` Protocol extended
with `update_entity`, `delete_entity`, `merge_entities`, `create_merge_candidate`, `list_merge_candidates`,
`resolve_merge_candidate`, `get_merge_history`. Six new REST endpoints. Extraction service updated to persist near-miss
candidates.

**Frontend:** New types, API functions, and store actions. `EntityDetailView` has edit form + delete button.
`EntityListView` has row checkboxes, merge modal, and merge review section.

**Also fixed:** Two tuple-unpack bugs in `GET /api/entities?search=` and MCP `journal_list_entities` that crashed on
non-empty results.

**Source:** user feedback 2026-04-12.

---

## Tier 3 — Polish and research

### 9. ~~Multi-page ingestion UI `[webapp]`~~ — ✅ shipped 2026-04-12

**Superseded** by the entry creation feature (Closed item 17). The webapp now has a full `/entries/new` view with
drag-drop multi-image upload, reorder, thumbnails, async OCR job with progress bar, plus text entry and file import tabs.

---

### 10. Voice note playback `[both]`

Audio player alongside transcript in `EntryDetailView` for voice entries. Needs:

1. A `GET /api/entries/{id}/audio` endpoint that serves the original audio file. `source_files` already stores the path.
2. Frontend `<audio>` element with transcript scrubbing (timestamp markers if Whisper gave us word-level timestamps,
   otherwise simple playback).

**Source:** `journal-webapp/docs/future-features.md` "Phase 4".

---

### 11. ~~Low-confidence OCR highlighting `[both]`~~ — ✅ shipped 2026-04-11

Shipped as the "Review" toggle in `EntryDetailView`. The OCR provider wraps uncertain words/phrases in `⟪/⟫` sentinels,
the parser extracts them as `uncertain_spans` (stored in DB via migration 0005), and the Review toggle renders them with
yellow highlights in the Original OCR panel. Spans are anchored to `raw_text` and immune to `final_text` edits. UX
improved 2026-04-12: toggle is always clickable, shows info banner when no uncertain spans exist.

**Original question answered:** Anthropic's vision API does not return per-region confidence natively, but prompting the
model to wrap uncertain spans in sentinels achieves the same result.

**Source:** `journal-webapp/journal/260411-review-toggle.md`.

---

### 12. Export `[both]`

Export entries (or a filtered subset) to Markdown, PDF, or JSON. `GET /api/export?format=markdown&from=...&to=...` with
server-side rendering. Button on `EntryListView` above the filtered list.

**Source:** `journal-webapp/docs/future-features.md` "Phase 5".

---

### 13. Semantic-chunker percentile tuning `[server]`

`SemanticChunker` ships with `boundary_percentile=25` and `decisive_percentile=10` as defaults. These were picked by gut
feel because the user had 2 real entries at the time — meaningless stats. Once the corpus is ~20 entries, sweep values
with the existing `journal eval-chunking` CLI and commit the winners to `config.py`.

Open questions to answer during tuning:

1. Does raising boundary_percentile to 30/35 produce more coherent chunks or just fewer chunks?
2. How do ratios compare between `fixed` (150/40) and `semantic` (25/10)? The user flipped the default to `semantic` in
   commit `d1343ac` — verify that decision holds at ~20 entries.
3. Consider building a golden-query retrieval set at ~20 entries.

**Source:** `journal-server/journal/260410-semantic-chunking.md` "What's deferred to the next session".

---

### 14. Predicate normalisation for the entity graph `[server]`

Relationship predicates (`met`, `saw`, `caught up with`, `had lunch with`) are free-text. Over time they drift and a
single underlying relationship gets expressed as N different predicates. A normalisation pass — small clustering LLM call
that maps free-text predicates to a canonical set — keeps the graph queryable.

**Blocker:** needs real data to see the drift. Don't preempt the drift with a hand-crafted mapping; let it accumulate,
then cluster.

**Source:** `docs/entity-tracking.md` "Known risks",
`journal-server/journal/260411-security-ocr-context-entity-tracking.md` "Deferred to a future session".

---

### 15. Coreference resolution `[server]`

Currently only first-person (`I`, `me`, `my`) is resolved, via the `JOURNAL_AUTHOR_NAME` config. Pronouns like `we`,
`she`, `him`, `they` are not resolved — the extractor sees them as strings with no entity link, so "she told me..."
contributes nothing to the graph.

**Approach:** most likely a second LLM pass over the entry that's given the already-extracted entity list and asked to
fill in pronoun references. Expensive if done every run; cheap if done only as part of `extract-entities --stale-only`.

**Source:** `journal-server/journal/260411-security-ocr-context-entity-tracking.md` "Deferred to a future session".

---

### 16. OCR context priming empirical evaluation `[server]`

OCR context priming shipped 2026-04-11 but was never measured against a real baseline. Run the same handwritten sample
through the OCR provider with and without `OCR_CONTEXT_DIR` set and eyeball the proper-noun accuracy delta.

**If no delta:** decide whether to keep the feature on (cache-ttl cost is minimal once the system text is above the cache
minimum) or rip it out.

**Source:** `journal-server/journal/260411-security-ocr-context-entity-tracking.md` "Second-session checklist".

---

### 17. `FixedTokenChunker` sizing review `[server]`

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

**Related:** #13 (semantic chunker percentile tuning). Both sweeps are worth doing in the same session once the corpus
reaches ~20 entries so the numbers are meaningful.

**Source:** conversation with Claude, 2026-04-11, reviewing the chunks overlay on entry 7.

---

### 18. Grow OCR glossary to unlock prompt caching `[server]`

As of 2026-04-11 the deployed `OCR_CONTEXT_DIR` loads ~333 chars of glossary content. Combined with `SYSTEM_PROMPT` +
`CONTEXT_USAGE_INSTRUCTIONS` the composed system block is ~328 tokens — well below Anthropic's 4,096-token
`cache_control` minimum, so the system block is re-sent uncached on every OCR call. The boot-time warning fires on every
restart:

```
OCR system text is 328 tokens (approx) — below the 4096-token
cache minimum for claude-opus-4-6. cache_control will be silently
ignored and every request will pay full input price.
```

**Action:** grow the context directory organically until the composed system text crosses 4,096 tokens (≈ 15-20 KB of
markdown across `people.md`, `places.md`, `topics.md`, and any other categories that make sense). Genuine content is
preferred — both for OCR accuracy and for caching — rather than padding with filler. Once over the threshold every OCR
call becomes ~12.5× cheaper on the system block.

**Cost pressure is low** — at ~1 page/day the uncached system block is cents per month, so this is a "do it when you have
more proper nouns to add" item, not urgent. But it should be done at some point, and if you decide _not_ to, consider
adding a `warning_suppressed` flag to silence the repeat warning so it doesn't numb you to other cache-related issues.

**Related:** #16 (glossary accuracy evaluation). Do both in the same session — grow the glossary, measure the accuracy
delta on a held-out sample, check the warning is gone.

**Source:** conversation with Claude, 2026-04-11. Server logs after the `OCR_CONTEXT_DIR` path fix at 23:46:31 show the
glossary loading correctly but still below the cache minimum.

---

## Deferred / known gaps (not planned, but tracked)

### D1. Legacy multipage entries with the old `\n\n` page join `[server]`

Entries ingested before the 2026-04-11 chunking fix have `"\n\n".join(page_texts)` baked into their `final_text`. Running
`rechunk_entries` alone doesn't help — the separator is part of the chunker input, not a parameter.

Fix options:

1. Opt-in script that rebuilds `final_text` for legacy multipage entries from `entry_pages.raw_text` with the new
   separator, then rechunks. Destructive to any user edits to `final_text`, so it must be opt-in.
2. Ignore — the user's current DB has 5 small single-page entries, none affected. Re-ingest from scratch if any multipage
   pathologies actually surface.

**Status:** currently going with option 2. Promote to Tier 2 if affected entries appear.

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
    Import File, Upload Images tabs. Supersedes Tier 3 item 9 (multi-page ingestion UI).
18. Image upload bug fixes (2026-04-12) — nginx `client_max_body_size 20m` for the `/api/` proxy, `apiFetch` Content-Type
    fix for `FormData` uploads, error message extraction (`body.error` fallback), duplicate error display removed,
    dismiss button + clear-on-tab-switch for error banner.
19. OCR date extraction + date editing (2026-04-12) — new `date_extraction` module parses dates from OCR text (named
    months, ISO, DD/MM/YYYY). `PATCH /api/entries/{id}` extended to accept `entry_date` alongside `final_text`. Webapp:
    clickable date heading in EntryDetailView with inline date picker.
20. OCR uncertainty highlighting (Tier 3 item 11, 2026-04-11) — Review toggle in EntryDetailView with `⟪/⟫` sentinel
    parsing, `uncertain_spans` DB storage, yellow highlights on Original OCR panel. UX improved 2026-04-12: always
    clickable, info banner when no spans exist.
21. Entity management (Tier 2 item 8, 2026-04-12) — merge, rename, delete, and merge review for entities. Migration 0008
    adds `entity_merge_history` and `entity_merge_candidates` tables. Six new REST endpoints. Webapp entity detail view
    has edit/delete, list view has multi-select merge + merge review section. Fixed two tuple-unpack bugs in entity list
    endpoints.
22. Mobile layout fix (2026-04-12) — corrected text panel was invisible on small screens due to absolute-positioned
    children in a flex-col layout. Both editor sections now have `min-h-[300px]` on mobile.

## Closed — shipped between 2026-04-13 and 2026-05-09

Grouped by workstream rather than by commit; see the linked journal entries for detail.

23. **Auto-entity-reextraction on save (2026-04-13)** — entity extraction runs automatically
    as part of the save pipeline (`server/journal/260413-auto-entity-reextraction-on-save.md`).
    Supersedes Tier 2 item 7.
24. **Mood-scoring deployment fix + default reversal (2026-04-13)** — frontend mood chart
    confirmed live (was already shipped on 2026-04-11), and `JOURNAL_ENABLE_MOOD_SCORING`
    flipped to default `true` (`config.py:263`). Now toggleable at runtime from Settings.
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
    Series: `260423-pushover-notifications.md`, `260423-pushover-notifications-ui.md`,
    `260430-pushover-bullet-format.md`.
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
