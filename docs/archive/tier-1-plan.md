# Tier 1 Implementation Plan

**Status:** closed 2026-05-09. All four Tier 1 items are now done. Kept as a record of the
planning round and the open-question decisions. New work moves directly through
[`roadmap.md`](../roadmap.md); promote Tier 2 items into a successor `tier-2-plan.md` when one
is needed.

**Closeout summary (2026-05-09 audit):**

1. **Item 1 (entity-extraction first run)** — de facto complete. The entity tables have been
   populated and actively maintained in prod since mid-April; downstream features built on
   them include auto-reextraction-on-save (2026-04-13), `/api/dashboard/entity-distribution`
   + `/entity-trends` (2026-04-21), the entity casing / aliases / quarantine / merge-candidate
   / dedup-rejection workstream (2026-05-06 → 2026-05-08), and the past-dismissals audit panel
   (2026-05-08). T1.1.b (dedup-threshold tuning) was never executed — `0.88` remains the
   default in `config.py:363` — but no entry suggests this blocked anyone, so the implicit
   decision is "default holds."
2. **Item 2 (`/health` endpoint)** — shipped 2026-04-11 (unchanged from the original entry).
3. **Item 3a (basic dashboard)** — shipped 2026-04-11.
4. **Item 3b (mood scoring)** — backend shipped 2026-04-11. T1.3b.vii (frontend mood chart)
   was also shipped 2026-04-11; the original plan entry was stale on the day it was written
   and was corrected on 2026-04-13 (`journal/260413-mood-scoring-deployment-fix.md`). Open
   question 2 was later reversed: `JOURNAL_ENABLE_MOOD_SCORING` now **defaults to True**
   (`src/journal/config.py:263`), opt-out instead of opt-in. See `docs/mood-scoring.md`.
5. **Item 3c (people + topic charts)** — shipped 2026-04-21 with different endpoint names and
   chart shapes than this plan specified:
   - Endpoints: `/api/dashboard/entity-distribution`, `/api/dashboard/entity-trends`,
     `/api/dashboard/calendar-heatmap`, plus `/mood-entity-correlation` and
     `/word-count-distribution` as bonus charts.
   - Open question 6 (heatmap library) decided in favour of CSS grid + multi-line, not
     `chartjs-chart-matrix` — no new npm dep.
   - All shipped as part of the unified DashboardView, not as a separate Insights view.
6. **Item 4 (Search UI)** — shipped 2026-04-11. Subsequently overhauled 2026-05-01 to a
   hybrid (BM25 + dense + RRF + Haiku rerank) pipeline that drops the `mode=` toggle entirely;
   see `docs/search.md` and `journal/260501-hybrid-search.md`.

The original plan body, work-unit breakdown, build order, and open-question rationale are
preserved below as reference.

---

## Current state of the art (verified 2026-04-11)

Before planning new work, here's what already exists that the Tier 1 items can build on top of. Checked by reading the
current code, not from memory.

**Search**

1. `QueryService.search_entries()` — semantic, paginated, date-filtered, groups chunk matches by entry, sorts by top
   chunk score (`src/journal/services/query.py:39`). Returns `SearchResult` with `matching_chunks: list[ChunkMatch]`
   where each chunk carries text and score but **not** char offsets into the parent entry.
2. `SQLiteEntryRepository.search_text()` — FTS5 keyword search over `entries_fts` (indexes `final_text`), date-filtered
   (`repository.py:150`). **Not yet exposed via QueryService** — Tier 1 item 4 needs a thin service wrapper.

**Stats / mood / topics**

1. `QueryService.get_mood_trends(start, end, granularity='week')` exists (`query.py:131`) and delegates to the
   repository. The aggregation itself works — **but the `mood_scores` table is empty** because nothing in the current
   ingestion pipeline writes to it. Tier 1 item 3b needs to populate it.
2. `QueryService.get_topic_frequency(topic, ...)` exists (`query.py:139`) but takes a single topic parameter and is
   implemented over FTS5, not over the entity tables. For the dashboard "top topics" chart we'll want a different
   aggregation over `entity_mentions` filtered to `entity_type='topic'`.
3. `get_statistics(start, end)` exists (`query.py:126`) for total-counts-style stats.
4. **No `get_writing_frequency` exists.** Needs to be written.

**mood_scores schema** (from migration 0001, currently unused):

```sql
mood_scores(id, entry_id, dimension TEXT DEFAULT 'overall',
            score REAL CHECK(-1.0 <= score <= 1.0),
            confidence REAL, created_at)
```

Dimensions are free text, scores are in `[-1, +1]`. This is the shape we need to write to; no migration needed.

**Webapp**

1. Chart.js 4 is already wired via `src/utils/chartjs-config.ts`.
2. Mosaic shell (Sidebar + Header + DefaultLayout) is in place.
3. The overlay highlight mechanism in `EntryDetailView` and `useOverlayHighlight` can be reused by Search UI for chunk
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

Item 1 runs in parallel with any coding — it's not a coding task. Items 2 and 4 are the only parallel coding tracks.
Everything else is strictly sequential within Item 3.

---

## Work unit breakdown

Sizes use **S / M / L**:

- **S** — one sitting; single file or small multi-file change.
- **M** — half a day to a day of focused work; multi-file change, moderate tests.
- **L** — more than a day; new subsystem, migration, or cross-repo coordination.

### Item 1 — First real entity-extraction run `[server, ops]` — ✅ de facto shipped

See closeout summary above. The work units below were never ticked off as a discrete checklist
session, but the entity tables have been populated and worked against in prod since mid-April.
T1.1.b (dedup tuning) was not executed; `ENTITY_DEDUP_SIMILARITY_THRESHOLD` remains at the
default `0.88`. Treat the rest as historical.

- **T1.1.a** `[S]` — Pick one entry you know well. Run `journal extract-entities --entry-id N`. Eyeball the output:
  canonical names, relationships, predicates, confidence scores. Deliverable: gut-check result ("looks right" / "list of
  problems").
- **T1.1.b** `[S]` — Only if T1.1.a flagged noisy dedup merges: tune `ENTITY_DEDUP_SIMILARITY_THRESHOLD` in `.env` and
  re-run. Default is `0.88`. Deliverable: committed config change, or a note saying defaults are fine.
- **T1.1.c** `[S]` — Batch run: `journal extract-entities --stale-only`. Deliverable: populated `entities`,
  `entity_aliases`, `entity_mentions`, `entity_relationships`.
- **T1.1.d** `[S]` — Verify via `GET /api/entities` and the webapp `EntityListView`. Confirm the list, detail, mentions,
  and relationships pages render without errors on real data.
- **T1.1.e** `[S]` — Write a short journal entry capturing any dedup tuning, surprising LLM outputs, or predicate
  patterns noticed. Feeds Tier 3 item 14 (predicate normalisation) with actual data.

**Parallelism note:** these units run on your laptop independently of any coding work below. Kick T1.1.a off in the first
session and pick up the coding tracks while Anthropic is processing.

---

### Item 2 — `/health` endpoint `[server]` — ✅ shipped 2026-04-11

Backend-only. See `journal/260411-health-endpoint.md` for the session notes and `docs/api.md` for the endpoint contract.

- **T1.2.a** `[M]` ✅ **In-process stats collector.** Shipped as `src/journal/services/stats.py`.
  `InMemoryStatsCollector` has bounded per-type `deque(maxlen=1000)` of recent latency samples, exact counters,
  `threading.Lock`-protected record + snapshot, nearest-rank p50/p95/p99 percentiles computed on snapshot. Wired into
  `QueryService` behind an optional `stats: StatsCollector | None = None` dependency with a `_timed` helper that is a
  pure passthrough when `stats=None`.
- **T1.2.b** `[S]` ✅ **Ingestion stats aggregator.** Shipped as `SQLiteEntryRepository.get_ingestion_stats(now)`
  returning an `IngestionStats` dataclass. Window cutoffs (`last_7d`, `last_30d`) are computed in Python from the
  injected `now` parameter so the tests can drive the clock deterministically. Row counts surface from a hardcoded
  `_HEALTH_ROW_COUNT_TABLES` tuple rather than dynamic enumeration, so the `/health` contract is stable across schema
  additions.
- **T1.2.c** `[S]` ✅ **Provider liveness checks.** Shipped as `src/journal/services/liveness.py` with `check_sqlite`,
  `check_chromadb`, `check_api_key`, and `overall_status`. API key checks never burn tokens — they only verify presence
  and plausible length and return `degraded` (not `error`) for missing keys, because the _server_ is still up.
- **T1.2.d** `[S]` ✅ **Route.** `GET /health` in `api.py`, registered via `mcp.custom_route`. **Not** bearer-authed —
  `BearerTokenMiddleware` gained an `exempt_paths` kwarg and `main()` passes `{"/health"}`. Decision rationale: the
  server binds to loopback only, so any caller that can reach `/health` already has a shell on the box, and the payload
  is scrubbed of anything that would leak query content.
- **T1.2.e** `[S]` ✅ **Dev CLI surface.** `journal health [--compact]` subcommand shipped. Builds services locally and
  prints the same JSON payload as the HTTP endpoint. Exits non-zero when the rolled-up status is `error`.
- **T1.2.f** `[M]` ✅ **Tests.** 44 new tests across stats (10), liveness (12), ingestion stats (4), query service stats
  integration (4), auth exempt paths (5), `/health` route (6), and CLI (3). All pass. Ruff clean.

**Open questions — resolved:**

1. Should `/health` be bearer-authed? **No.** See rationale in T1.2.d and the privacy guardrails in the payload (no
   search terms, counts-only query stats).
2. Prometheus text format as an alternate content-type? **Not shipped.** JSON only. Revisit if a real monitoring consumer
   appears.
3. Most-frequent search terms? **Not shipped** — privacy concern.

---

### Item 3 — Dashboard `[both]`

Three sub-epics in order: 3a (basic) → 3b (mood) → 3c (entities).

#### Item 3a — Basic dashboard (no LLM cost) — ✅ shipped 2026-04-11

Backend in `journal-server@HEAD`, webapp in `journal-webapp@HEAD`. See `journal/260411-dashboard-3a-backend.md` (this
repo) and `journal-webapp/journal/260411-dashboard-3a.md` for the session notes.

- **T1.3a.i** `[S]` ✅ **Backend: writing-frequency repository method.** Shipped as
  `SQLiteEntryRepository.get_writing_frequency`. Supports `week`, `month`, `quarter`, and `year` granularities.
  `bin_start` is computed in SQL as the canonical bucket-start date (Monday for weeks, first of month/quarter/year for
  the others) so the frontend never has to parse `%Y-W%W`-style strings. Empty buckets are omitted.
- **T1.3a.ii** `[S]` ✅ **Backend: combined REST endpoint.** `GET /api/dashboard/writing-stats?bin=&from=&to=` returns a
  single envelope with both `entry_count` and `total_words` per bin. One method, one response shape, matches the
  underlying SQL. Open question #5 resolved in favour of the combined endpoint.
- **T1.3a.iii** `[M]` ✅ **Webapp: dashboard shell.** Shipped as `/` route (Option B) → `DashboardView.vue` with
  `useDashboardStore` (Pinia) holding date-range + bin state. Entries list demoted to `/entries`.
- **T1.3a.iv** `[S]` ✅ **Chart: writing frequency.** Chart.js 4 line chart styled via `src/utils/chartjs-config.ts`.
  Friendly empty-state message when `entry_count < 5` per the "explicit > implicit" decision on open question #9.
- **T1.3a.v** `[S]` ✅ **Chart: word-count trend.** Second series on the same data, rendered alongside.
- **T1.3a.vi** `[M]` ✅ **Tests.** 10 repo unit tests + 8 API integration tests (server), Vitest tests for store + view +
  sidebar default-expanded behaviour (webapp), Playwright verification at 375×812 / 768×1024 / 1920×1080.

**Open questions resolved:**

1. #5 (combined endpoint vs two) — **combined**, as above.
2. #9 (empty-state threshold) — friendly message when `entry_count < 5`, not hidden.
3. Additional granularities (`quarter`, `year`) — shipped alongside the original `week`/`month`; `day` dropped as too
   noisy for the target corpus.

#### Item 3b — Mood scoring + mood chart — ✅ backend shipped 2026-04-11

Backend in `journal-server@HEAD`. Webapp mood chart is the last remaining piece and ships as a sibling commit in
`journal-webapp`. Session notes: `journal/260411-mood-scoring-backend.md`. Full rationale: `docs/mood-scoring.md`.

**Design refinements vs the original plan:**

1. The fixed 5-facet set (`overall, energy, anxiety, gratitude, productivity`) was replaced with a **7-facet
   user-editable config** in `config/mood-dimensions.toml`. Mixed bipolar / unipolar scale types per facet — some axes
   (joy vs sadness) are genuinely bipolar, others (agency vs apathy) are unipolar because the "negative pole" reads as
   absence. Old schema forced everything to bipolar which was wrong.
2. **Config as data**, not code. Python loader parses TOML at startup; editing a facet is a one-file edit + restart.
3. **Sparse storage by default.** Adding a facet doesn't require a backfill run — new entries pick it up, old ones return
   `null` for the new facet until an explicit `--stale-only` backfill is run. Regeneration is cheap.
4. **Sonnet 4.5** instead of Haiku (user preference — noticeably better at subjective calibration on short texts, still
   ~$0.006/entry).

**Work units:**

- **T1.3b.i** `[S]` ✅ **Dimensions config + loader.** Shipped as `config/mood-dimensions.toml` +
  `src/journal/services/mood_dimensions.py` with a `MoodDimension` dataclass (`name`, `positive_pole`, `negative_pole`,
  `scale_type`, `notes`) and a validated `load_mood_dimensions(path)` loader using stdlib `tomllib` (no new deps). 17
  unit tests including a smoke test of the shipped config file.
- **T1.3b.ii** `[M]` ✅ **MoodScorer Protocol + Anthropic adapter.** `src/journal/providers/mood_scorer.py`. Uses tool
  use via the Messages API. `build_tool_schema(dimensions)` builds the input schema at call time with per-facet min/max
  bounds based on scale type, so unipolar facets fail schema validation if the model tries to return a negative score.
  Fallback parses the first JSON object from text blocks if the tool call is missing. 22 unit tests.
- **T1.3b.iii** `[S]` ✅ **Config flag.** `JOURNAL_ENABLE_MOOD_SCORING` (default False). Also `MOOD_SCORER_MODEL`
  (default `claude-sonnet-4-5`), `MOOD_SCORER_MAX_TOKENS`, and `MOOD_DIMENSIONS_PATH`.
- **T1.3b.iv** `[S]` ✅ **Wire into ingestion.** `MoodScoringService` bridges scorer + repo + `replace_mood_scores`. Hook
  in `IngestionService._process_text` via a new `mood_scoring` optional constructor param. Scoring failures are logged
  but never propagate back — an entry is always saved even if scoring fails. 6 service tests.
- **T1.3b.v** `[S]` ✅ **Backfill CLI.**
  `journal backfill-mood [--force] [--prune-retired] [--dry-run] [--start-date] [--end-date]`. `--stale-only` is the
  default (mode string, not a flag). Dry-run prints a cost estimate based on Sonnet 4.5 pricing. 9 tests across the
  service + 2 CLI tests.
- **T1.3b.vi** `[S]` ✅ **Dashboard endpoints.** Two new routes in `api.py`: `GET /api/dashboard/mood-dimensions`
  surfaces the live facet set for the frontend; `GET /api/dashboard/mood-trends` wraps `QueryService.get_mood_trends`
  with a `dimension` filter. Both bearer-authenticated. 7 integration tests.
- **T1.3b.vii** `[M]` ✅ **Frontend mood chart.** Was actually shipped in the webapp on
  2026-04-11 alongside the backend; the original entry here was stale on the day it was
  written. Corrected 2026-04-13 in `journal/260413-mood-scoring-deployment-fix.md`. Now
  rendered as a multi-line Chart.js chart with variance bands, grouped/ungrouped dimension
  toggles, and a sibling mood-correlation chart (added 2026-04-21). See
  `webapp/src/views/DashboardView.vue:381` (`renderMoodChart`).
- **T1.3b.viii** `[M]` ✅ **Backend tests.** 80 new tests across the six backend files (repository CRUD, trends canonical
  dates, scoring service, backfill service, mood scorer adapter, dimensions loader, API endpoints, CLI). Frontend tests
  ship with the webapp commit.

**Refactor bonus:** `get_mood_trends` and `get_writing_frequency` now share a `_bin_start_sql` helper and both return
canonical ISO dates instead of `%Y-W%W`-style format strings. The LLM-facing `journal_get_mood_trends` MCP tool still
accepts `day / week / month / quarter / year` for backward compatibility — only the supported-granularity set expanded;
nothing was removed.

#### Item 3c — People + topic charts — ✅ shipped 2026-04-21 (different shape)

What actually shipped (see `webapp/journal/260421-unified-dashboard-and-new-charts.md`):

- **Backend** — `src/journal/api/dashboard.py`:
  - `GET /api/dashboard/entity-distribution` (line 310) — entity mention counts grouped by
    name, filtered by type and date. Covers T1.3c.i + T1.3c.ii intent.
  - `GET /api/dashboard/entity-trends` (line 418) — top-N entities binned over time. Covers
    T1.3c.iv intent.
  - `GET /api/dashboard/calendar-heatmap` (line 373) — calendar heatmap, rendered as a CSS
    grid in the frontend (no `chartjs-chart-matrix` dep). Open question 6 resolved in favour
    of CSS grid + multi-line.
  - Bonus: `GET /api/dashboard/mood-entity-correlation` (line 494),
    `GET /api/dashboard/word-count-distribution` (line 560).
- **Frontend** — `webapp/src/views/DashboardView.vue`: entity-trends multi-line chart, entity
  distribution doughnut with expand/collapse legend, calendar heatmap CSS grid. All on the
  unified `/` dashboard, not a separate Insights view.

The original work-unit table is preserved below as a historical reference.

- **T1.3c.i** `[S]` — Backend: mentions aggregation. *(replaced by `entity-distribution` +
  `entity-trends`.)*
- **T1.3c.ii** `[S]` — Backend: entity-type frequency. *(folded into the above.)*
- **T1.3c.iii** `[S]` — Endpoints. *(shipped under different names — see above.)*
- **T1.3c.iv** `[S]` — Chart: people mentions. *(shipped as `renderEntityTrendsChart`.)*
- **T1.3c.v** `[M]` — Chart: topic heatmap. *(shipped as a CSS-grid calendar heatmap; matrix
  plugin not adopted.)*
- **T1.3c.vi** `[M]` — Tests. *(shipped alongside.)*

---

### Item 4 — Search UI `[both]` — ✅ shipped 2026-04-11

**Backend shipped 2026-04-11** — see `journal/260411-search-backend.md`. **Frontend shipped 2026-04-11** — see
`journal-webapp/journal/260411-search-ui.md`. All T1.4 work units are done.

- **T1.4.a** `[S]` ✅ **Backend: expose FTS5 search via service.** `QueryService.keyword_search()` delegates to a new
  `EntryRepository.search_text_with_snippets()` method. Results carry a `snippet` string (FTS5 `snippet()` output with
  `\x02`/`\x03` marker chars wrapping matched terms) and leave `matching_chunks=[]`. Decided to add the snippet generator
  per open question 8 — response shape is independent of frontend markup choice.
- **T1.4.b** `[M]` ✅ **Backend: extend `SearchResult` with chunk offsets.** `ChunkMatch` now carries optional
  `chunk_index`, `char_start`, `char_end` fields (all `None` for legacy entries without persisted chunks).
  `QueryService.search_entries()` enriches each match by JOINing `entry_chunks` on `chunk_index` from Chroma metadata.
- **T1.4.c** `[S]` ✅ **Backend: REST endpoint.** `GET /api/search` shipped with params `q`, `mode` (default `semantic`
  per open question 7), `start_date`, `end_date`, `limit` (clamped `[1, 50]`), `offset`. Bearer-authenticated via the
  existing middleware. Full contract in `docs/api.md`.
- **T1.4.d** `[M]` ✅ **Webapp: `/search` route and SearchView shell.** Shipped as `src/views/SearchView.vue` with query
  input, semantic/keyword mode toggle, and date range inputs. `src/stores/search.ts` (`useSearchStore`) holds
  query/mode/date state so back-navigation preserves the query.
- **T1.4.e** `[M]` ✅ **Webapp: results list with highlights.** Each result shows the entry date, relevance score, and a
  snippet rendered through `src/utils/searchSnippet.ts` which converts the server's `\x02`/`\x03` marker chars to
  `<mark>` tags with HTML escaping. Click-through links include `?chunk=N` for semantic hits; `EntryDetailView` reads the
  param on mount, flips the overlay to chunks mode, waits for chunks to load, and `scrollIntoView` on the matching
  `[aria-label="chunk N start"]` badge element. Keyword hits omit `chunk` since FTS5 doesn't produce per-chunk scores.
- **T1.4.f** `[M]` ✅ **Tests.** 23 new backend tests (repo FTS5 snippets, query service enrichment + keyword_search, API
  endpoint happy paths + error cases) and 34 new webapp tests (api client, snippet renderer, store, SearchView, chunk
  scroll-into-view in EntryDetailView). All pass; coverage held above the gates in both repos.

**Open question:** for keyword mode, do we want to run FTS5's snippet generator for each result so we can show the
matching context rather than the whole entry? FTS5 supports `snippet()` and `highlight()` aux functions — recommended
yes, it's a small backend change and much better UX.

---

## Recommended build order

Critical path is annotated with `🎯`. Items off the critical path can run in parallel with the item immediately above
them.

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

**Why Search UI first:** independent, backend already mostly exists, high immediate value at any corpus size, gives the
user something usable even while they're growing the corpus.

**Why `/health` before the dashboard:** the StatsCollector infrastructure built in T1.2.a is genuinely reused by T1.3a —
query latency is the kind of thing the dashboard can surface too, and building it once in one place keeps the measurement
path consistent.

**Why mood scoring (3b) before entity charts (3c):** 3b is independent; 3c is hard-blocked on item 1 having produced real
data. Even if item 1 is kicked off in parallel, the batch run takes time. Work on mood scoring while that's happening.

---

## Open questions (need decisions before coding)

> **Historical note (2026-05-09):** these were the planning-time questions and recommendations.
> Outcomes recorded in the work-unit sections above and the closeout summary at the top of
> this doc. Notably, Q2 was reversed — `JOURNAL_ENABLE_MOOD_SCORING` defaults to **True**, not
> False; Q6 was decided in favour of CSS-grid heatmap rather than `chartjs-chart-matrix`.

Numbered so you can answer by number.

1. **Mood scoring model:** Claude Haiku 4.5 (recommended — cheap, fast, good enough for `[-1,+1]` scoring) or reuse the
   Opus model already configured for OCR? Recommendation: Haiku.
2. **Mood scoring on or off by default:** `JOURNAL_ENABLE_MOOD_SCORING` defaults to `False` — meaning no-op unless the
   user explicitly opts in. Alternative: default `True` in dev, `False` in prod. Recommendation: default `False`,
   explicitly opt in via `.env`.
3. **Mood dimensions:** `overall`, `energy`, `anxiety`, `gratitude`, `productivity`. Add / remove any?
4. **`/health` authentication:** bearer-authenticated (recommended, matches the rest of `/api/*`) or unauthenticated for
   liveness probes? Liveness probes can be satisfied with a simpler unauthenticated `GET /live` if you ever need one.
5. **Dashboard combined endpoint vs two separate:** single `GET /api/dashboard/writing-stats` returning count and word
   count in one response (recommended) or two endpoints?
6. **Topic heatmap library choice:** `chartjs-chart-matrix` plugin (true heatmap, one extra npm dep) or fall back to a
   grouped bar chart using the base Chart.js (no new dep, less informative visual)? Recommendation: matrix plugin if the
   bundle-size delta is under ~15 KB, otherwise bar chart.
7. **Search mode default:** semantic (recommended — matches how you'd use the tool day-to-day) or keyword?
8. **Search snippet generator:** use FTS5 `snippet()` / `highlight()` auxiliary functions for keyword mode?
   Recommendation: yes.
9. **Dashboard minimum corpus guard:** hide charts entirely below N entries, or show an empty-state message? If the
   latter, what's N? Recommendation: show empty-state message when `entry_count < 5`.

---

## Risks and known unknowns

1. **Small corpus makes dashboards ugly.** At 5 entries the charts are toys. Empty-state handling (open question 9)
   mitigates this, but the mood and people charts are intrinsically meaningless at small N. Ship them anyway — the corpus
   grows.
2. **Mood scoring accuracy at small N.** Can't validate the scorer's judgement against ground truth without comparing
   dozens of entries. Plan to spot-check the first ~10 scored entries manually and adjust the system prompt if
   mis-scored.
3. **Search highlight offsets on legacy multipage entries.** Entries ingested before the 2026-04-11 page-join fix have
   the old `"\n\n"` join in their `final_text`, which means their existing `entry_chunks.char_start`/`char_end` already
   account for the old separator. Highlights should still render correctly because offsets and text agree — but verify
   during T1.4.e with one legacy multipage entry in the corpus.
4. **StatsCollector thread safety.** FastMCP's request handling model needs a quick check before picking a locking
   strategy — if it's asyncio single-threaded per loop, no lock needed; if it dispatches to a thread pool, the histogram
   needs a lock. Check `src/journal/mcp_server.py` before building T1.2.a.
5. **FTS5 `final_text` vs vector store content mismatch.** The FTS index is built on `final_text` but the vector store
   embeds chunks from `final_text` as well, so keyword and semantic modes should agree on what they're searching. Verify
   by mixing a few keyword and semantic queries on the same term during T1.4.f.

---

## How to use this plan

1. Pick the top unticked work unit on the critical path.
2. Open a task (`TaskCreate`) with the unit ID (e.g. `T1.4.c`) as the subject.
3. Answer any open questions that block the unit before coding.
4. Build → test → commit → tick off here and in `roadmap.md`.
5. Write a journal entry at the end of each session covering the units shipped.

When Tier 1 is complete, promote Tier 2 items to active planning by expanding them into a new `tier-2-plan.md` following
the same structure.
