# Journal Tool — Consolidated Roadmap

**Status:** written 2026-04-11. Supersedes `docs/phase-2-brief.md`
(2026-03-23) and `journal-webapp/docs/future-features.md`. Pulls in
all outstanding TODOs from the task list, memory files, and recent
journal entries.

This is the single source of truth for "what do we work on next".
When you finish an item, cross it out here; when you defer one,
move it to the "Deferred / known gaps" section with a reason.

Scope is cross-cutting: some items are pure backend (`journal-server`),
some pure frontend (`journal-webapp`), many touch both. Each item is
tagged with `[server]`, `[webapp]`, or `[both]`.

---

## Ordering rationale

Items are grouped by **readiness**, not by a linear "Phase 2 / Phase
3 / Phase 4" numbering, because the earlier phase-based docs kept
drifting out of sync with actual sequencing. The three tiers below
reflect the real blockers:

1. **Tier 1 — Ready to start now.** No upstream dependency. Pick any.
2. **Tier 2 — Blocked on real data or on a Tier 1 item.** Can't
   meaningfully start until the corpus grows or until the dependency
   ships.
3. **Tier 3 — Polish and research.** Valuable but not urgent.

Inside each tier, items are loosely ordered by "what the user would
get most use out of on a single-user personal journal with a small
corpus".

---

## Tier 1 — Ready to start now

### 1. First real entity-extraction run `[server]`

Entity extraction plumbing ships end-to-end in tests but has never
been run against real entries. Before building any entity UI on top
of it, we need to know the output quality is acceptable.

Steps:
1. Pick a single known entry: `journal extract-entities --entry-id N`.
2. Spot-check the extracted entities and relationships against what
   you'd expect for that entry.
3. Tune `ENTITY_DEDUP_SIMILARITY_THRESHOLD` (default `0.88`) if
   stage-c merges are noisy.
4. Once one entry looks good, do a batch run:
   `journal extract-entities --stale-only`.
5. Spot-check the entity list and relationship graph via the
   existing REST endpoints.

**Why this is first:** Every downstream item in Tier 2 (entity
graph view, LadybugDB experiment, dashboard people-mentions chart)
depends on having extracted data to look at. Graph viz against an
empty or 5-entity corpus is a toy.

**Source:** `journal-server/journal/260411-security-ocr-context-entity-tracking.md`
"Context for the next session".

---

### 2. `/health` endpoint `[server]` — ✅ shipped 2026-04-11

A single `GET /health` endpoint on the MCP server exposing
operational stats. **Unauthenticated** (decided against bearer
auth — loopback bind means anyone who can reach it already has
a shell on the box, and we scrub any field that would leak
query content). See `journal/260411-health-endpoint.md` for the
full session notes and `docs/api.md` for the endpoint contract.

**Shipped:**

1. ✅ `InMemoryStatsCollector` — `src/journal/services/stats.py`,
   bounded per-type histogram with `record_query` + `snapshot`,
   wired into `QueryService` behind an optional dependency.
2. ✅ `get_ingestion_stats(now)` — repository method aggregating
   total/last-7d/last-30d counts, by-source-type, avg words/chunks,
   last-ingestion timestamp, per-table row counts.
3. ✅ Provider liveness — `src/journal/services/liveness.py`
   pings SQLite and ChromaDB and sanity-checks Anthropic/OpenAI
   API keys without burning tokens.
4. ✅ `GET /health` route in `api.py`, exempt from the bearer
   middleware via a new `exempt_paths` kwarg on
   `BearerTokenMiddleware`.
5. ✅ `journal health [--compact]` CLI subcommand emitting the
   same payload as JSON for scripted use.

**Deliberately NOT shipped** (plan items that were cut on
review):

1. **Most frequent search terms.** The plan listed this as an
   optional "Query & usage stat". It would leak what the user
   was searching for from an unauthenticated endpoint, and
   adding it would push us toward tracking raw query strings
   in memory. Omitted — the query stats block carries
   counts-by-type only.
2. **ChromaDB last-update timestamp** and **SQLite database
   size (bytes)**. Both are operationally interesting but
   Chroma does not expose a cheap "last write" timestamp and
   DB bytes requires a separate stat() call. The `row_counts`
   block is the closest proxy and good enough.
3. **Dashboard consumer.** The plan noted that the dashboard
   (Tier 1 item 3) could reuse the health payload as a data
   source. Not pursued yet — when 3a lands it will add its
   own dashboard-specific endpoint, and the shared-source
   idea can be revisited if it turns out to duplicate work.

**Source:** `docs/phase-2-brief.md` "Health & Stats Endpoint".

---

### 3. Dashboard view `[both]` — 3a + 3b shipped 2026-04-11, 3c outstanding

Scoped webapp view at `/` (Option B — Dashboard is now the
home route; entries list moved to `/entries`). Uses Chart.js 4
(already in `journal-webapp` via `src/utils/chartjs-config.ts`)
styled to match the Mosaic aesthetic.

**Charts**

1. ✅ **Writing frequency** — entries per bin (week / month /
   quarter / year) over a selectable date range. Pure SQL
   aggregation, no LLM. Shipped 2026-04-11 as sub-epic 3a.
2. ✅ **Word count trend** — total words per bin, rendered
   alongside writing frequency. Same endpoint, second series.
   Shipped 2026-04-11 as sub-epic 3a.
3. ⏳ **People mentions over time** — stacked area / multi-line,
   top-N people. Depends on Tier 1 item 1 (real entity
   extraction) before it's meaningful. **3c, not yet started.**
4. ✅ **Mood dimensions** — per-entry scoring via Claude Sonnet
   4.5 (env-overridable), opt-in via `JOURNAL_ENABLE_MOOD_SCORING`.
   Facet set is user-editable via `config/mood-dimensions.toml`
   with mixed bipolar (`-1..+1`) / unipolar (`0..+1`) scale
   types per facet. Regeneration via `journal backfill-mood
   [--force | --stale-only] [--prune-retired] [--dry-run]`.
   See `docs/mood-scoring.md`.
5. ⏳ **Topic frequency heatmap or bar chart** — most-mentioned
   entities of type `topic` over time. Feeds off entity
   extraction. **3c, blocked on item 1.**

**Dashboard features**
1. Date range selector (last month / 3 months / 6 months / 1 year / all)
2. Bin width selector (day / week / month)
3. Responsive layout

**Backend endpoints** needed:
1. `GET /api/dashboard/writing-frequency?from=...&to=...&bin=week`
2. `GET /api/dashboard/word-count-trend?from=...&to=...&bin=week`
3. `GET /api/dashboard/mentions?from=...&to=...&top_n=10`
   (wraps entity-mention aggregation)
4. `GET /api/dashboard/mood-trends?from=...&to=...&bin=week`
   (wraps `QueryService.get_mood_trends()` — already exists)
5. `GET /api/dashboard/topic-frequency?from=...&to=...` (wraps
   `QueryService.get_topic_frequency()` — already exists)

**New dependency — ingestion-time scoring:** Charts 4 and 5 need
per-entry mood/topic scores stored in SQLite so the dashboard can
aggregate without re-running an LLM on every load. Two options:

1. **At ingestion:** during `_process_text` (or immediately after),
   fire a single scoring LLM call per entry, store results in a
   new `mood_scores` row (table already exists from migration 0001
   but is currently unused). This is the preferred path per the
   phase-2-brief — pay once at ingest, query cheaply forever.
2. **On demand:** score an entry the first time the dashboard asks
   for it, cache the result. Lazy but adds latency spikes on
   first dashboard load after batch ingestion.

Go with option 1. Keep the scorer behind a Protocol so it's
swappable. Make it opt-in via `JOURNAL_ENABLE_MOOD_SCORING` so it
doesn't silently burn tokens on users who don't want it.

**Ordering within the item:**
1. Ship writing-frequency + word-count charts first (no LLM cost,
   immediate value).
2. Then mood scoring + mood chart.
3. Then people/topic charts, which depend on Tier 1 item 1 having
   run against real entries.

**Source:** `docs/phase-2-brief.md` "Web Dashboard",
`journal-webapp/docs/future-features.md` "Phase 2: Dashboards".

---

### 4. Search UI `[both]` — ✅ shipped 2026-04-11

Dedicated webapp `/search` view.

**Backend — shipped 2026-04-11** (see
`journal/260411-search-backend.md`):
1. ✅ `GET /api/search?q=...&mode=semantic|keyword&start_date=...&end_date=...&limit=...&offset=...`
2. ✅ `ChunkMatch` now carries `chunk_index`, `char_start`,
   `char_end` for semantic hits so the frontend can render chunk
   highlights without a second round-trip.
3. ✅ Keyword mode returns FTS5 `snippet()` output with `\x02`/`\x03`
   marker chars wrapping matched terms.

**Frontend — shipped 2026-04-11** (see
`journal-webapp/journal/260411-search-ui.md`):
1. ✅ `/search` route and `SearchView.vue` with query input, mode
   toggle (semantic default), and date range filter.
2. ✅ Pinia `useSearchStore` preserves query/mode/dates across
   navigation and surfaces `ApiRequestError` messages verbatim.
3. ✅ Results list with FTS5 snippet highlights rendered via
   `src/utils/searchSnippet.ts` (converts `\x02`/`\x03` marker
   chars to `<mark>` tags with HTML escaping).
4. ✅ Click-through to `EntryDetailView` with `?chunk=N` on
   semantic hits; `EntryDetailView` reads the param, flips the
   overlay to chunks mode, and `scrollIntoView` on the matching
   chunk badge.

**Source:** `journal-webapp/docs/future-features.md` "Phase 3:
Search UI" (now obsolete — this roadmap entry is the record of
what actually shipped).

---

## Tier 2 — Blocked on data, Tier 1, or both

### 5. Entity graph visualization view `[webapp]`

New `/graph` route using **Cytoscape.js** (library bake-off already
happened — see `journal-webapp/journal/260411-auth-header-overlay-cache-entity-views.md`).
Renders the entity-and-relationship graph as an interactive force-
directed layout.

**Features**
1. Node colours by entity type (person / place / activity /
   organization / topic / other)
2. Click a node → side panel showing that entity's canonical name,
   aliases, mentions, incoming/outgoing relationships, and the list
   of entries it appears in (reuse `EntityDetailView` data)
3. Edge labels showing predicates
4. Filter bar: entity-type checkboxes, date-range slider, min
   confidence threshold
5. Search box to focus on a specific named entity

**Blocker:** Per the entity-tracking session notes, this waits
until there are "at least 30–50 entities and a handful of
relationships — anything smaller is a toy, not a useful knowledge
graph." So this is blocked on Tier 1 item 1 AND on the user
actually building up a corpus.

**Tasks tracked:** this is task #8 in the task list.

**Source:** `journal-webapp/journal/260411-auth-header-overlay-cache-entity-views.md`
"Deferred to Phase 2"; `journal-server/docs/entity-tracking.md`.

---

### 6. LadybugDB graph-backend experiment `[server]`

Swap in a second `EntityStore` implementation backed by LadybugDB
(Kuzu's successor) while keeping SQLite as the fallback. The
`EntityStore` Protocol in `src/journal/entitystore/store.py`
already exists specifically to make this pluggable — the
experiment is meant to be a zero-architectural-risk bet.

**Goals**
1. Validate that the Protocol abstraction actually holds up when a
   second backend is plugged in — any leakage of SQLite assumptions
   is a design bug to fix.
2. Benchmark: how much faster is a multi-hop relationship query
   (e.g. "who does Atlas know, and where have they been together?")
   against a native graph backend vs SQLite JOINs?
3. Evaluate operational cost — LadybugDB adds another moving piece.
   Is the query speedup worth the ops overhead on a single-user
   tool?

**Decision point:** once the benchmark is run, either commit to
graph DB as the default (feature-flagged, config-driven) or stay on
SQLite and delete the experimental branch. Do not ship two backends
as permanent production paths.

**Blocker:** needs real entity data (Tier 1 item 1) to benchmark
against. A toy dataset doesn't exercise graph traversal in any
meaningful way.

**Tasks tracked:** this is task #7 in the task list.

**Source:** `docs/entity-tracking.md` "Storage-agnostic Protocol",
`journal-server/journal/260411-security-ocr-context-entity-tracking.md`
"Deferred to a future session".

---

### 7. Entity extraction trigger UI `[webapp]`

An "Extract entities" button next to Save/Delete in `EntryDetailView`
that calls the existing `triggerEntityExtraction()` API client
function.

**Why deferred:** the 2026-04-11 entity session chose to keep the
initial population on the CLI so the user could spot-check results
without a button accidentally triggering extraction on every page
view. Once the user has done the first real run (Tier 1 item 1) and
is comfortable with the output, this becomes worth building.

**Blocker:** Tier 1 item 1.

**Source:** `journal-webapp/journal/260411-auth-header-overlay-cache-entity-views.md`
"Deferred to Phase 2".

---

### 8. Entity merge review UI `[webapp]`

The extraction service emits warnings whenever stage-c (embedding
similarity) merges two entities that weren't exact-name or alias
matches. There's currently no surface in the webapp to review or
overturn these merges.

**Design sketch:**
1. New "Merge review" badge in the sidebar that shows a count of
   pending warnings
2. Review page lists warnings with entity A, entity B, their
   canonical names, sample mentions, and an "Accept merge" /
   "Split back apart" action
3. Splitting an accepted merge needs a backend surface — this is a
   **backend design question** before it's a UI task.

**Blocker:** Tier 1 item 1 (no merges exist yet), plus needs a
design pass on how "undo a merge" works at the storage layer.

**Source:** `journal-webapp/journal/260411-auth-header-overlay-cache-entity-views.md`
"Deferred to Phase 2".

---

## Tier 3 — Polish and research

### 9. ~~Multi-page ingestion UI `[webapp]`~~ — ✅ shipped 2026-04-12

**Superseded** by the entry creation feature (Closed item 17). The
webapp now has a full `/entries/new` view with drag-drop multi-image
upload, reorder, thumbnails, async OCR job with progress bar, plus
text entry and file import tabs.

---

### 10. Voice note playback `[both]`

Audio player alongside transcript in `EntryDetailView` for voice
entries. Needs:

1. A `GET /api/entries/{id}/audio` endpoint that serves the original
   audio file. `source_files` already stores the path.
2. Frontend `<audio>` element with transcript scrubbing (timestamp
   markers if Whisper gave us word-level timestamps, otherwise
   simple playback).

**Source:** `journal-webapp/docs/future-features.md` "Phase 4".

---

### 11. Low-confidence OCR highlighting `[both]`

Ask the OCR provider to return per-region confidence metadata,
store it alongside `raw_text`, render it in the original panel as
dashed-amber underlines (alongside the existing diff highlights).

**Open questions:**
1. Does Anthropic's vision API even return per-region confidence?
   Need to check the SDK before sizing this.
2. What happens to the confidence spans after the user edits
   `final_text`? Character offsets into `raw_text` don't translate
   cleanly once the text is edited.

**Source:** `journal-webapp/docs/future-features.md` "Phase 3".

---

### 12. Export `[both]`

Export entries (or a filtered subset) to Markdown, PDF, or JSON.
`GET /api/export?format=markdown&from=...&to=...` with server-side
rendering. Button on `EntryListView` above the filtered list.

**Source:** `journal-webapp/docs/future-features.md` "Phase 5".

---

### 13. Semantic-chunker percentile tuning `[server]`

`SemanticChunker` ships with `boundary_percentile=25` and
`decisive_percentile=10` as defaults. These were picked by gut feel
because the user had 2 real entries at the time — meaningless stats.
Once the corpus is ~20 entries, sweep values with the existing
`journal eval-chunking` CLI and commit the winners to `config.py`.

Open questions to answer during tuning:
1. Does raising boundary_percentile to 30/35 produce more coherent
   chunks or just fewer chunks?
2. How do ratios compare between `fixed` (150/40) and `semantic`
   (25/10)? The user flipped the default to `semantic` in commit
   `d1343ac` — verify that decision holds at ~20 entries.
3. Consider building a golden-query retrieval set at ~20 entries.

**Source:** `journal-server/journal/260410-semantic-chunking.md`
"What's deferred to the next session".

---

### 14. Predicate normalisation for the entity graph `[server]`

Relationship predicates (`met`, `saw`, `caught up with`, `had lunch
with`) are free-text. Over time they drift and a single underlying
relationship gets expressed as N different predicates. A normalisation
pass — small clustering LLM call that maps free-text predicates to a
canonical set — keeps the graph queryable.

**Blocker:** needs real data to see the drift. Don't preempt the
drift with a hand-crafted mapping; let it accumulate, then cluster.

**Source:** `docs/entity-tracking.md` "Known risks",
`journal-server/journal/260411-security-ocr-context-entity-tracking.md`
"Deferred to a future session".

---

### 15. Coreference resolution `[server]`

Currently only first-person (`I`, `me`, `my`) is resolved, via the
`JOURNAL_AUTHOR_NAME` config. Pronouns like `we`, `she`, `him`,
`they` are not resolved — the extractor sees them as strings with no
entity link, so "she told me..." contributes nothing to the graph.

**Approach:** most likely a second LLM pass over the entry that's
given the already-extracted entity list and asked to fill in pronoun
references. Expensive if done every run; cheap if done only as part
of `extract-entities --stale-only`.

**Source:** `journal-server/journal/260411-security-ocr-context-entity-tracking.md`
"Deferred to a future session".

---

### 16. OCR context priming empirical evaluation `[server]`

OCR context priming shipped 2026-04-11 but was never measured
against a real baseline. Run the same handwritten sample through
the OCR provider with and without `OCR_CONTEXT_DIR` set and eyeball
the proper-noun accuracy delta.

**If no delta:** decide whether to keep the feature on (cache-ttl
cost is minimal once the system text is above the cache minimum)
or rip it out.

**Source:** `journal-server/journal/260411-security-ocr-context-entity-tracking.md`
"Second-session checklist".

---

### 17. `FixedTokenChunker` sizing review `[server]`

Observed on 2026-04-11 via the webapp chunks overlay on entry 7
(277 words, 2 pages, date 2026-02-15): the current
`FixedTokenChunker(max_tokens=150, overlap_tokens=40)` produces
**5 chunks** for that entry, which is over-fragmented. Three
smells from the overlay:

1. **Tail orphan** — a 12-word closing sentence ("I assume in a
   few years he will have different interests.") got ejected into
   its own chunk, severed from the parenting paragraph it belongs
   to. Orphan chunks embed as standalone thoughts with no context
   and hurt retrieval quality.
2. **Split mid-theme** — the marriage/parenting reflection gets
   cut across two chunks, so a search for "marriage" only hits
   half the content.
3. **Below recommended size** — `text-embedding-3-large` docs
   recommend 256-512 tokens per chunk for best retrieval.
   150-token chunks leave retrieval quality on the table.

**Action:** sweep `(max_tokens, overlap_tokens)` over the existing
corpus using the `journal eval-chunking` CLI. Candidate points:
`(150,40)` baseline, `(200,40)`, `(250,30)`, `(300,25)`. Expect
~250/30 to produce 2-3 chunks for a 277-word entry with clean
paragraph boundaries. Commit the winner to `config.py` and update
the regression test
`test_ingest_multi_page_packs_efficiently` (currently locks the
old 2-chunk count at `max_tokens=150`).

**Related:** #13 (semantic chunker percentile tuning). Both sweeps
are worth doing in the same session once the corpus reaches ~20
entries so the numbers are meaningful.

**Source:** conversation with Claude, 2026-04-11, reviewing the
chunks overlay on entry 7.

---

### 18. Grow OCR glossary to unlock prompt caching `[server]`

As of 2026-04-11 the deployed `OCR_CONTEXT_DIR` loads ~333 chars
of glossary content. Combined with `SYSTEM_PROMPT` +
`CONTEXT_USAGE_INSTRUCTIONS` the composed system block is ~328
tokens — well below Anthropic's 4,096-token `cache_control`
minimum, so the system block is re-sent uncached on every OCR
call. The boot-time warning fires on every restart:

```
OCR system text is 328 tokens (approx) — below the 4096-token
cache minimum for claude-opus-4-6. cache_control will be silently
ignored and every request will pay full input price.
```

**Action:** grow the context directory organically until the
composed system text crosses 4,096 tokens (≈ 15-20 KB of markdown
across `people.md`, `places.md`, `topics.md`, and any other
categories that make sense). Genuine content is preferred — both
for OCR accuracy and for caching — rather than padding with
filler. Once over the threshold every OCR call becomes ~12.5×
cheaper on the system block.

**Cost pressure is low** — at ~1 page/day the uncached system
block is cents per month, so this is a "do it when you have more
proper nouns to add" item, not urgent. But it should be done at
some point, and if you decide *not* to, consider adding a
`warning_suppressed` flag to silence the repeat warning so it
doesn't numb you to other cache-related issues.

**Related:** #16 (glossary accuracy evaluation). Do both in the
same session — grow the glossary, measure the accuracy delta on a
held-out sample, check the warning is gone.

**Source:** conversation with Claude, 2026-04-11. Server logs
after the `OCR_CONTEXT_DIR` path fix at 23:46:31 show the
glossary loading correctly but still below the cache minimum.

---

## Deferred / known gaps (not planned, but tracked)

### D1. Legacy multipage entries with the old `\n\n` page join `[server]`

Entries ingested before the 2026-04-11 chunking fix have
`"\n\n".join(page_texts)` baked into their `final_text`. Running
`rechunk_entries` alone doesn't help — the separator is part of the
chunker input, not a parameter.

Fix options:
1. Opt-in script that rebuilds `final_text` for legacy multipage
   entries from `entry_pages.raw_text` with the new separator, then
   rechunks. Destructive to any user edits to `final_text`, so it
   must be opt-in.
2. Ignore — the user's current DB has 5 small single-page entries,
   none affected. Re-ingest from scratch if any multipage
   pathologies actually surface.

**Status:** currently going with option 2. Promote to Tier 2 if
affected entries appear.

---

### D2. No entity chips cache invalidation on entry save `[webapp]`

When the user saves an edited entry, the entity chip strip in
`EntryDetailView` continues to show entities extracted from the
*pre-edit* text until a full page reload.

Not a bug exactly: chips show historical extraction state, and the
user needs to explicitly re-run extraction anyway before the entity
graph is updated. Worth revisiting if and when Tier 2 item 7
(in-webapp extraction trigger) ships — at that point the workflow
becomes "edit, save, re-extract from within the webapp" and the
stale chips become a real UX bug.

**Source:** `journal-webapp/journal/260411-auth-header-overlay-cache-entity-views.md`
"Risks and known gaps".

---

### D3. Entity list pagination state doesn't survive navigation `[webapp]`

If you click an entity, view its detail, then hit back to
`/entities`, you land on page 1 not the page you came from. Minor
UX — fix is to persist `currentParams.offset` across mount cycles
or via route query params.

**Source:** `journal-webapp/journal/260411-auth-header-overlay-cache-entity-views.md`
"Risks and known gaps".

---

### D4. Provider zero-data-retention agreements (policy, not code) `[ops]`

Anthropic and OpenAI data retention is a policy discussion, not a
code change. Signing a ZDR addendum removes provider-side retention
as a privacy concern for journal content.

**Source:** `docs/security.md`;
`journal-server/journal/260411-security-ocr-context-entity-tracking.md`.

---

### D5. TLS / reverse proxy `[ops]`

The server currently binds to `127.0.0.1:8400` per the 2026-04-11
security hardening. A reverse proxy (caddy or nginx) on the VM
terminates TLS and fronts the server when exposing it beyond
loopback. Out of scope for the codebase; a deployment concern.

**Source:** `journal-server/journal/260411-security-ocr-context-entity-tracking.md`
"Deferred to a future session".

---

### D6. Encrypted backup for journal data `[ops]`

`/srv/media/config/journal` (SQLite DB, ChromaDB data, any source
images/audio) should be backed up with encryption at rest to protect
against disk loss. Out of scope for the codebase.

**Source:** `journal-server/journal/260411-security-ocr-context-entity-tracking.md`
"Deferred to a future session".

---

### D7. Webapp "Phase 2: Authentication" from `future-features.md` `[webapp]`

The old `future-features.md` listed "Phase 2: Authentication" —
login page, JWT, user table. **This is obsolete** as written. The
backend shipped bearer-token auth in the 2026-04-11 security
session; the webapp sends `Authorization: Bearer <token>` from
`JOURNAL_API_TOKEN` in its env.

What *might* still be worth doing:
1. A lightweight settings page where the user can set/change the
   API token without editing env files (nice to have, not
   important on a single-user tool).
2. If the tool ever goes multi-user, that's a full-blown rewrite of
   the auth model — not a "Phase 2" item, a new project.

Leaving this in the deferred list only as a marker so the old
doc's item doesn't get quietly forgotten.

---

## Closed — shipped between 2026-03-22 and 2026-04-11 (recap)

Included so we don't accidentally re-surface these as TODOs.

1. Initial implementation (CLI, MCP server, SQLite + ChromaDB,
   ingestion, query routing)
2. Multi-page OCR ingestion (server-side)
3. REST API mode (SSE + JSON endpoints)
4. Semantic chunker with sentence splitting, adaptive overlap,
   and `eval-chunking` CLI
5. Rechunk CLI and backfill script
6. Chunk/token overlay in webapp (with cache invalidation on save)
7. Live diff editor in webapp
8. Delete entry endpoint and UI
9. Entity tracking backend (extraction service, dedup pipeline,
   Protocol-based storage) and webapp read-only list/detail views
10. OCR context priming (`OCR_CONTEXT_DIR`, prompt caching,
    anti-hallucination instructions)
11. Security hardening (bearer auth, fail-closed startup, DNS
    rebinding protection always on, loopback-only bind, SSRF guard,
    `chmod 600`, `docs/security.md`)
12. Multi-page chunking page-join fix (277→5 chunks mystery closed)
13. Search UI (Tier 1 item 4) — `GET /api/search` backend with
    semantic + keyword modes, FTS5 `snippet()` highlights, chunk
    offsets on `ChunkMatch`, and the webapp `/search` view with
    `?chunk=N` deep-link scroll-into-view
14. `/health` endpoint (Tier 1 item 2) — `GET /health` with
    ingestion stats, in-process query latency histogram, and
    per-component liveness checks; bearer-auth-exempt on loopback;
    `journal health` CLI prints the same payload
15. Dashboard 3a (Tier 1 item 3a) — `GET /api/dashboard/writing-stats`
    and webapp DashboardView at `/` (Option B routing — entries
    list moved to `/entries`). Chart.js 4 line charts for writing
    frequency and word count per week/month/quarter/year
16. Dashboard 3b (Tier 1 item 3b) — per-entry mood scoring via
    `config/mood-dimensions.toml` (bipolar + unipolar facets),
    `MoodScorer` Protocol + Anthropic Sonnet 4.5 adapter via
    tool use, `replace_mood_scores` with sparse storage,
    opt-in `JOURNAL_ENABLE_MOOD_SCORING` config flag,
    `journal backfill-mood` CLI with stale-only / force /
    prune-retired / dry-run modes, `GET /api/dashboard/mood-dimensions`
    and `GET /api/dashboard/mood-trends` endpoints, and a mood
    chart in the dashboard. See `docs/mood-scoring.md`
17. Entry creation from webapp (2026-04-12) — three new REST
    endpoints: `POST /api/entries/ingest/text` (sync JSON),
    `POST /api/entries/ingest/file` (sync multipart .md/.txt),
    `POST /api/entries/ingest/images` (async multipart, job-based
    OCR). `IngestionService.ingest_text()` for text/file entries.
    `JobRunner` extended with `ingest_images` and `mood_score_entry`
    job types. Migration 0007 relaxes `source_type` CHECK. Webapp:
    `/entries/new` with Write Entry, Import File, Upload Images tabs.
    Supersedes Tier 3 item 9 (multi-page ingestion UI).

---

## How to use this doc

1. **Starting a work session?** Read Tier 1 and pick the highest
   item you have appetite for.
2. **Finished an item?** Move it from Tier 1/2/3 to "Closed" with a
   one-line summary, plus a journal entry covering the details.
3. **Discovered a new item?** Add it to the right tier with a
   source reference so future-you can find why it matters.
4. **Deferring an item?** Move it to "Deferred / known gaps" with
   a reason. Items should only sit in Tier 1/2/3 if there's intent
   to ship them.

The task list (TaskCreate/TaskList) and this roadmap are
complementary, not redundant:
- **Roadmap:** long-lived, survives sessions, cross-cutting.
- **Task list:** per-session scratch for active work.

When you promote a roadmap item to active work, create a task for
it and link back here.
