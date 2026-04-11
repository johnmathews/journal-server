# Tier 1 Implementation Plan

**Status:** written 2026-04-11. Expands the 4 Tier 1 items from
`roadmap.md` into concrete work units, a recommended build order,
and the open questions that need a decision before coding starts.

This doc sits between `roadmap.md` (long-lived intent) and the
task list (per-session scratch). When a work unit starts, create a
task and link back here; when it ships, cross it out in both this
doc and the roadmap.

---

## Current state of the art (verified 2026-04-11)

Before planning new work, here's what already exists that the
Tier 1 items can build on top of. Checked by reading the current
code, not from memory.

**Search**
1. `QueryService.search_entries()` — semantic, paginated, date-filtered,
   groups chunk matches by entry, sorts by top chunk score
   (`src/journal/services/query.py:39`). Returns `SearchResult` with
   `matching_chunks: list[ChunkMatch]` where each chunk carries text
   and score but **not** char offsets into the parent entry.
2. `SQLiteEntryRepository.search_text()` — FTS5 keyword search over
   `entries_fts` (indexes `final_text`), date-filtered
   (`repository.py:150`). **Not yet exposed via QueryService** —
   Tier 1 item 4 needs a thin service wrapper.

**Stats / mood / topics**
1. `QueryService.get_mood_trends(start, end, granularity='week')`
   exists (`query.py:131`) and delegates to the repository. The
   aggregation itself works — **but the `mood_scores` table is
   empty** because nothing in the current ingestion pipeline
   writes to it. Tier 1 item 3b needs to populate it.
2. `QueryService.get_topic_frequency(topic, ...)` exists
   (`query.py:139`) but takes a single topic parameter and is
   implemented over FTS5, not over the entity tables. For the
   dashboard "top topics" chart we'll want a different aggregation
   over `entity_mentions` filtered to `entity_type='topic'`.
3. `get_statistics(start, end)` exists (`query.py:126`) for
   total-counts-style stats.
4. **No `get_writing_frequency` exists.** Needs to be written.

**mood_scores schema** (from migration 0001, currently unused):
```sql
mood_scores(id, entry_id, dimension TEXT DEFAULT 'overall',
            score REAL CHECK(-1.0 <= score <= 1.0),
            confidence REAL, created_at)
```
Dimensions are free text, scores are in `[-1, +1]`. This is the
shape we need to write to; no migration needed.

**Webapp**
1. Chart.js 4 is already wired via `src/utils/chartjs-config.ts`.
2. Mosaic shell (Sidebar + Header + DefaultLayout) is in place.
3. The overlay highlight mechanism in `EntryDetailView` and
   `useOverlayHighlight` can be reused by Search UI for chunk
   highlights — it already consumes `(char_start, char_end)` spans.

---

## Dependency graph

```
 ┌────────────────────────────────────┐
 │  Item 1: entity-extraction run     │  (ops, no code)
 │  (unblocks 3c, and all of Tier 2)  │
 └───────────┬────────────────────────┘
             │
             │            ┌────────────────────────┐
             │            │  Item 4: Search UI     │
             │            │  (independent)         │
             │            └────────────────────────┘
             │
             │            ┌────────────────────────┐
             │            │  Item 2: /health       │
             │            │  (independent, builds  │
             │            │   stats infra reused   │
             │            │   by 3a)               │
             │            └──────────┬─────────────┘
             │                       │
             │                       ▼
             │            ┌────────────────────────┐
             │            │  Item 3a: basic        │
             │            │  dashboard (writing-   │
             │            │  frequency, word-count)│
             │            └──────────┬─────────────┘
             │                       │
             │                       ▼
             │            ┌────────────────────────┐
             │            │  Item 3b: mood scoring │
             │            │  + mood chart          │
             │            └──────────┬─────────────┘
             │                       │
             └───────────────────────┤
                                     ▼
                          ┌────────────────────────┐
                          │  Item 3c: people +     │
                          │  topic charts          │
                          │  (needs real entities) │
                          └────────────────────────┘
```

Item 1 runs in parallel with any coding — it's not a coding task.
Items 2 and 4 are the only parallel coding tracks. Everything else
is strictly sequential within Item 3.

---

## Work unit breakdown

Sizes use **S / M / L**:
- **S** — one sitting; single file or small multi-file change.
- **M** — half a day to a day of focused work; multi-file change,
  moderate tests.
- **L** — more than a day; new subsystem, migration, or
  cross-repo coordination.

### Item 1 — First real entity-extraction run `[server, ops]`

No code. All work units are operational.

- **T1.1.a** `[S]` — Pick one entry you know well. Run
  `journal extract-entities --entry-id N`. Eyeball the output:
  canonical names, relationships, predicates, confidence scores.
  Deliverable: gut-check result ("looks right" / "list of
  problems").
- **T1.1.b** `[S]` — Only if T1.1.a flagged noisy dedup merges:
  tune `ENTITY_DEDUP_SIMILARITY_THRESHOLD` in `.env` and re-run.
  Default is `0.88`. Deliverable: committed config change, or a
  note saying defaults are fine.
- **T1.1.c** `[S]` — Batch run: `journal extract-entities
  --stale-only`. Deliverable: populated `entities`,
  `entity_aliases`, `entity_mentions`, `entity_relationships`.
- **T1.1.d** `[S]` — Verify via `GET /api/entities` and the
  webapp `EntityListView`. Confirm the list, detail, mentions,
  and relationships pages render without errors on real data.
- **T1.1.e** `[S]` — Write a short journal entry capturing any
  dedup tuning, surprising LLM outputs, or predicate patterns
  noticed. Feeds Tier 3 item 14 (predicate normalisation) with
  actual data.

**Parallelism note:** these units run on your laptop independently
of any coding work below. Kick T1.1.a off in the first session and
pick up the coding tracks while Anthropic is processing.

---

### Item 2 — `/health` endpoint `[server]`

Backend-only. Builds two new things plus a route.

- **T1.2.a** `[M]` — **In-process stats collector.** New module,
  probably `src/journal/services/stats.py`. Public surface:
  ```python
  class StatsCollector(Protocol):
      def record_query(self, query_type: str, latency_ms: float) -> None: ...
      def snapshot(self) -> StatsSnapshot: ...
  ```
  Concrete implementation keeps per-query-type counts and a
  lightweight latency histogram (HDR histogram would be overkill;
  just a sorted bounded buffer of the last N=1000 durations per
  type so p50/p95/p99 computation is O(log n) on snapshot).
  Wrapped transparently around `QueryService` methods — the
  service accepts an optional `stats` dependency and records on
  every method call. Zero-overhead when `stats=None`.
  Tests: unit tests for histogram correctness, percentile edge
  cases, thread safety of the counter.
- **T1.2.b** `[S]` — **Ingestion stats aggregator.** New repository
  method `get_ingestion_stats(now: datetime) -> IngestionStats`
  that returns a dataclass with total / last-7d / last-30d counts
  by source type, average word count, average chunk count,
  average tokens-per-chunk, last ingestion timestamp, table row
  counts. Pure SQL aggregation; tests mock the clock.
- **T1.2.c** `[S]` — **Provider liveness checks.** New module
  `src/journal/services/liveness.py` with per-provider ping
  functions:
  1. SQLite: `SELECT 1` through the connection.
  2. ChromaDB: `collection.count()`.
  3. Anthropic: **do not** burn a real token — just check
     `api_key` is set, valid length, and optionally verify the
     client constructs without error.
  4. OpenAI: same non-burning check for the embeddings provider.
  Each returns `(name, status, detail)`. Overall status is the
  worst of the components.
- **T1.2.d** `[S]` — **Route.** `GET /health` registered via
  `mcp.custom_route` on the MCP server, bearer-authenticated (the
  existing middleware already covers `/health` if we add it
  under the same mount — verify, don't assume). Returns a JSON
  envelope combining snapshots from T1.2.a, T1.2.b, and T1.2.c.
- **T1.2.e** `[S]` — **Dev CLI surface.** `journal health` as a
  thin CLI wrapper that prints the same payload, for scripted
  use and for sanity checks during development.
- **T1.2.f** `[M]` — **Tests for the route.** Bearer auth
  positive/negative, payload schema, status=degraded when a
  component ping fails (use a fake provider that raises).

**Open questions** (see bottom of doc for the full list):
- Should `/health` be bearer-authed? Recommendation: yes.
- Do we want Prometheus text format as an alternate content-type?
  Recommendation: no, not yet. Ship JSON only; add Prometheus if
  real monitoring consumers ever appear.

---

### Item 3 — Dashboard `[both]`

Three sub-epics in order: 3a (basic) → 3b (mood) → 3c (entities).

#### Item 3a — Basic dashboard (no LLM cost)

- **T1.3a.i** `[S]` — **Backend: writing-frequency repository
  method.** `get_writing_frequency(start, end, granularity)
  -> list[WritingFrequencyBin]`. Pure SQL `GROUP BY` on
  `strftime('%Y-%W', entry_date)` (or day/month). Returns
  `(bin_start, entry_count, total_words)` tuples.
- **T1.3a.ii** `[S]` — **Backend: REST endpoints.**
  1. `GET /api/dashboard/writing-frequency?from=&to=&bin=week`
  2. `GET /api/dashboard/word-count-trend?from=&to=&bin=week`
  The word-count endpoint reuses the writing-frequency result
  and computes average per bin — one method, two response
  shapes (or one combined response, see open question below).
- **T1.3a.iii** `[M]` — **Webapp: dashboard shell.** New route
  `/dashboard`, new `DashboardView.vue`. Page layout with a
  header containing a date-range picker (reuse the Mosaic
  components if present; otherwise plain `<input type="date">`)
  and a bin-width picker (day/week/month radio). Pinia store
  for dashboard state so picker changes trigger refetches
  without re-creating the charts.
- **T1.3a.iv** `[S]` — **Chart: writing frequency.** Line or bar
  chart via Chart.js. Uses the existing `chartjs-config.ts`
  styling. Empty-state handling: if the corpus has < N entries,
  show a friendly "not enough data yet" message instead of an
  embarrassing 1-point chart.
- **T1.3a.v** `[S]` — **Chart: word-count trend.** Same shape,
  different data source.
- **T1.3a.vi** `[M]` — **Tests.** Backend: endpoint integration
  tests + repository unit tests. Frontend: Vitest tests for the
  store's refetch-on-picker-change, and a component smoke test
  for DashboardView.

**Open question:** one combined `/api/dashboard/writing-stats`
endpoint returning count and word count in a single response, or
two endpoints? Two endpoints is more RESTful but the fetch-twice
cost is real. Recommendation: single combined endpoint —
frontend always wants both charts together on the same time
range, and it matches the actual DB query.

#### Item 3b — Mood scoring + mood chart

This is the biggest unit in Tier 1 because it introduces a new
LLM call in the ingestion path.

- **T1.3b.i** `[S]` — **Design decision: mood dimensions.** The
  existing `mood_scores` schema lets dimensions be free text,
  but we need a committed canonical set so the chart knows what
  to plot. Proposed initial set: `overall`, `energy`, `anxiety`,
  `gratitude`, `productivity`. All scored in `[-1, +1]` (schema
  already constrains this). Document in `docs/mood-scoring.md`.
- **T1.3b.ii** `[M]` — **Protocol + adapter.** New
  `src/journal/providers/mood_scorer.py`:
  ```python
  @runtime_checkable
  class MoodScorer(Protocol):
      def score(self, text: str) -> list[MoodScore]: ...
  ```
  Concrete `AnthropicMoodScorer` uses Claude Haiku (cheaper than
  Opus, fast, fine for this task). System prompt describes the
  dimension set and output JSON schema. Parses JSON, validates
  scores are in range, returns `list[MoodScore]`. Cost per
  entry: a few cents at most.
- **T1.3b.iii** `[S]` — **Config flag.** New setting
  `JOURNAL_ENABLE_MOOD_SCORING` (default `False`). When false,
  the scorer Protocol is not instantiated and ingestion skips
  the scoring step entirely.
- **T1.3b.iv** `[S]` — **Wire into ingestion.** In `_process_text`
  or right after `create_entry`, call `mood_scorer.score(text)`
  if configured, then write results via
  `repository.replace_mood_scores(entry_id, scores)` (new method
  following the `replace_chunks` precedent — delete-then-insert,
  idempotent on re-run).
- **T1.3b.v** `[S]` — **Rechunk-style backfill CLI.**
  `journal backfill-mood` that walks existing entries missing
  scores and runs the scorer. Same guard rails as
  `rechunk_entries` — dry-run flag, date-range filter.
- **T1.3b.vi** `[S]` — **Backend: mood-trends endpoint.**
  `GET /api/dashboard/mood-trends?from=&to=&bin=week&dims=...`.
  Wraps existing `QueryService.get_mood_trends`.
- **T1.3b.vii** `[M]` — **Frontend: mood chart.** Multi-line
  Chart.js chart with a dimension toggle (checkbox per dimension,
  default all on). Ties into the same date picker as 3a.
- **T1.3b.viii** `[M]` — **Tests.** Scorer adapter tests (mock
  the Anthropic client, assert schema validation). Ingestion
  test that the flag gates the call. Backfill CLI test. Endpoint
  test. Frontend test.

#### Item 3c — People + topic charts

- **T1.3c.i** `[S]` — **Backend: mentions aggregation.** New
  repository method
  `get_top_mentions(entity_type, start, end, bin, top_n)` that
  returns per-bin counts for the top-N entities of a given type.
  Pure SQL over `entity_mentions` JOIN `entries`.
- **T1.3c.ii** `[S]` — **Backend: entity-type frequency.** New
  method for the topic heatmap — counts of `entity_type='topic'`
  mentions binned the same way.
- **T1.3c.iii** `[S]` — **Endpoints.**
  1. `GET /api/dashboard/mentions?entity_type=person&top_n=10&from=&to=&bin=week`
  2. `GET /api/dashboard/topic-frequency?from=&to=&bin=week`
- **T1.3c.iv** `[S]` — **Chart: people mentions.** Stacked area
  or multi-line.
- **T1.3c.v** `[M]` — **Chart: topic heatmap.** Chart.js doesn't
  natively do heatmaps well — options are the matrix plugin
  (`chartjs-chart-matrix`) or falling back to a grouped bar
  chart. **Open question** (see bottom).
- **T1.3c.vi** `[M]` — **Tests.**

**Blocker:** all of 3c depends on Item 1 having populated the
entity tables with real data. Don't start coding 3c until T1.1.c
is complete.

---

### Item 4 — Search UI `[both]` — ✅ shipped 2026-04-11

**Backend shipped 2026-04-11** — see
`journal/260411-search-backend.md`. **Frontend shipped 2026-04-11** —
see `journal-webapp/journal/260411-search-ui.md`. All T1.4 work
units are done.

- **T1.4.a** `[S]` ✅ **Backend: expose FTS5 search via service.**
  `QueryService.keyword_search()` delegates to a new
  `EntryRepository.search_text_with_snippets()` method. Results
  carry a `snippet` string (FTS5 `snippet()` output with
  `\x02`/`\x03` marker chars wrapping matched terms) and leave
  `matching_chunks=[]`. Decided to add the snippet generator per
  open question 8 — response shape is independent of frontend
  markup choice.
- **T1.4.b** `[M]` ✅ **Backend: extend `SearchResult` with chunk
  offsets.** `ChunkMatch` now carries optional `chunk_index`,
  `char_start`, `char_end` fields (all `None` for legacy entries
  without persisted chunks). `QueryService.search_entries()`
  enriches each match by JOINing `entry_chunks` on `chunk_index`
  from Chroma metadata.
- **T1.4.c** `[S]` ✅ **Backend: REST endpoint.** `GET /api/search`
  shipped with params `q`, `mode` (default `semantic` per open
  question 7), `start_date`, `end_date`, `limit` (clamped
  `[1, 50]`), `offset`. Bearer-authenticated via the existing
  middleware. Full contract in `docs/api.md`.
- **T1.4.d** `[M]` ✅ **Webapp: `/search` route and SearchView
  shell.** Shipped as `src/views/SearchView.vue` with query input,
  semantic/keyword mode toggle, and date range inputs.
  `src/stores/search.ts` (`useSearchStore`) holds query/mode/date
  state so back-navigation preserves the query.
- **T1.4.e** `[M]` ✅ **Webapp: results list with highlights.**
  Each result shows the entry date, relevance score, and a
  snippet rendered through `src/utils/searchSnippet.ts` which
  converts the server's `\x02`/`\x03` marker chars to `<mark>`
  tags with HTML escaping. Click-through links include
  `?chunk=N` for semantic hits; `EntryDetailView` reads the
  param on mount, flips the overlay to chunks mode, waits for
  chunks to load, and `scrollIntoView` on the matching
  `[aria-label="chunk N start"]` badge element. Keyword hits
  omit `chunk` since FTS5 doesn't produce per-chunk scores.
- **T1.4.f** `[M]` ✅ **Tests.** 23 new backend tests (repo FTS5
  snippets, query service enrichment + keyword_search, API
  endpoint happy paths + error cases) and 34 new webapp tests
  (api client, snippet renderer, store, SearchView, chunk
  scroll-into-view in EntryDetailView). All pass; coverage held
  above the gates in both repos.

**Open question:** for keyword mode, do we want to run FTS5's
snippet generator for each result so we can show the matching
context rather than the whole entry? FTS5 supports `snippet()` and
`highlight()` aux functions — recommended yes, it's a small
backend change and much better UX.

---

## Recommended build order

Critical path is annotated with `🎯`. Items off the critical path
can run in parallel with the item immediately above them.

```
Session 1:
  🎯 T1.1.a  Run extract-entities on one entry        [in parallel with all below]
  🎯 T1.4.a  Keyword search service wrapper
  🎯 T1.4.b  SearchResult chunk offsets
  🎯 T1.4.c  /api/search endpoint + tests

Session 2:
  🎯 T1.4.d  SearchView shell + store
  🎯 T1.4.e  Results list with highlights
  🎯 T1.4.f  Tests
     T1.1.b  Tune dedup threshold if needed
     T1.1.c  Batch extract --stale-only
     T1.1.d  Verify entity views
     T1.1.e  Journal entry about entity extraction

Session 3:
  🎯 T1.2.a  StatsCollector + histogram
  🎯 T1.2.b  IngestionStats aggregator
  🎯 T1.2.c  Liveness checks
  🎯 T1.2.d  /health route
  🎯 T1.2.e  journal health CLI
  🎯 T1.2.f  Tests

Session 4:
  🎯 T1.3a.i  Writing frequency repository method
  🎯 T1.3a.ii /api/dashboard/writing-stats endpoint
  🎯 T1.3a.iii DashboardView shell + pickers
  🎯 T1.3a.iv  Writing frequency chart
  🎯 T1.3a.v   Word count chart
  🎯 T1.3a.vi  Tests

Session 5:
  🎯 T1.3b.i   Mood-scoring design decision + docs
  🎯 T1.3b.ii  MoodScorer Protocol + Haiku adapter
  🎯 T1.3b.iii Config flag
  🎯 T1.3b.iv  Wire into ingestion
  🎯 T1.3b.v   Backfill CLI
  🎯 T1.3b.vi  Mood-trends endpoint
  🎯 T1.3b.vii Mood chart
  🎯 T1.3b.viii Tests

Session 6 (only after T1.1.c is done):
  🎯 T1.3c.i   Mentions aggregation
  🎯 T1.3c.ii  Topic frequency aggregation
  🎯 T1.3c.iii Endpoints
  🎯 T1.3c.iv  People mentions chart
  🎯 T1.3c.v   Topic heatmap / bar chart
  🎯 T1.3c.vi  Tests
```

**Why Search UI first:** independent, backend already mostly
exists, high immediate value at any corpus size, gives the user
something usable even while they're growing the corpus.

**Why `/health` before the dashboard:** the StatsCollector
infrastructure built in T1.2.a is genuinely reused by T1.3a —
query latency is the kind of thing the dashboard can surface too,
and building it once in one place keeps the measurement path
consistent.

**Why mood scoring (3b) before entity charts (3c):** 3b is
independent; 3c is hard-blocked on item 1 having produced real
data. Even if item 1 is kicked off in parallel, the batch run
takes time. Work on mood scoring while that's happening.

---

## Open questions (need decisions before coding)

Numbered so you can answer by number.

1. **Mood scoring model:** Claude Haiku 4.5 (recommended —
   cheap, fast, good enough for `[-1,+1]` scoring) or reuse the
   Opus model already configured for OCR? Recommendation: Haiku.
2. **Mood scoring on or off by default:** `JOURNAL_ENABLE_MOOD_SCORING`
   defaults to `False` — meaning no-op unless the user explicitly
   opts in. Alternative: default `True` in dev, `False` in prod.
   Recommendation: default `False`, explicitly opt in via `.env`.
3. **Mood dimensions:** `overall`, `energy`, `anxiety`,
   `gratitude`, `productivity`. Add / remove any?
4. **`/health` authentication:** bearer-authenticated (recommended,
   matches the rest of `/api/*`) or unauthenticated for liveness
   probes? Liveness probes can be satisfied with a simpler
   unauthenticated `GET /live` if you ever need one.
5. **Dashboard combined endpoint vs two separate:** single
   `GET /api/dashboard/writing-stats` returning count and word
   count in one response (recommended) or two endpoints?
6. **Topic heatmap library choice:** `chartjs-chart-matrix` plugin
   (true heatmap, one extra npm dep) or fall back to a grouped
   bar chart using the base Chart.js (no new dep, less
   informative visual)? Recommendation: matrix plugin if the
   bundle-size delta is under ~15 KB, otherwise bar chart.
7. **Search mode default:** semantic (recommended — matches how
   you'd use the tool day-to-day) or keyword?
8. **Search snippet generator:** use FTS5 `snippet()` / `highlight()`
   auxiliary functions for keyword mode? Recommendation: yes.
9. **Dashboard minimum corpus guard:** hide charts entirely below
   N entries, or show an empty-state message? If the latter,
   what's N? Recommendation: show empty-state message when
   `entry_count < 5`.

---

## Risks and known unknowns

1. **Small corpus makes dashboards ugly.** At 5 entries the
   charts are toys. Empty-state handling (open question 9)
   mitigates this, but the mood and people charts are intrinsically
   meaningless at small N. Ship them anyway — the corpus grows.
2. **Mood scoring accuracy at small N.** Can't validate the
   scorer's judgement against ground truth without comparing
   dozens of entries. Plan to spot-check the first ~10 scored
   entries manually and adjust the system prompt if mis-scored.
3. **Search highlight offsets on legacy multipage entries.**
   Entries ingested before the 2026-04-11 page-join fix have the
   old `"\n\n"` join in their `final_text`, which means their
   existing `entry_chunks.char_start`/`char_end` already account
   for the old separator. Highlights should still render
   correctly because offsets and text agree — but verify during
   T1.4.e with one legacy multipage entry in the corpus.
4. **StatsCollector thread safety.** FastMCP's request handling
   model needs a quick check before picking a locking strategy —
   if it's asyncio single-threaded per loop, no lock needed; if
   it dispatches to a thread pool, the histogram needs a lock.
   Check `src/journal/mcp_server.py` before building T1.2.a.
5. **FTS5 `final_text` vs vector store content mismatch.** The
   FTS index is built on `final_text` but the vector store
   embeds chunks from `final_text` as well, so keyword and
   semantic modes should agree on what they're searching. Verify
   by mixing a few keyword and semantic queries on the same term
   during T1.4.f.

---

## How to use this plan

1. Pick the top unticked work unit on the critical path.
2. Open a task (`TaskCreate`) with the unit ID (e.g. `T1.4.c`)
   as the subject.
3. Answer any open questions that block the unit before coding.
4. Build → test → commit → tick off here and in `roadmap.md`.
5. Write a journal entry at the end of each session covering the
   units shipped.

When Tier 1 is complete, promote Tier 2 items to active planning
by expanding them into a new `tier-2-plan.md` following the same
structure.
