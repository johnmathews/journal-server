# 2026-04-11 — Mood scoring backend (Tier 1 item 3b)

Session 5 of the Tier 1 plan. Backend half of sub-epic 3b
(mood scoring + mood chart). The webapp mood chart ships as a
sibling commit in `journal-webapp`. Closes work units
**T1.3b.i** through **T1.3b.vi** and **T1.3b.viii** (backend
tests); **T1.3b.vii** (frontend chart) is in the webapp commit.

## What shipped

### 1. Dimensions as data

`config/mood-dimensions.toml` is the source of truth for the
facet set. Each `[[dimension]]` block is a record with:

- `name` — snake_case key stored in `mood_scores.dimension`
- `positive_pole` / `negative_pole` — human-readable labels
- `scale_type` — `"bipolar"` (`-1..+1`) or `"unipolar"` (`0..+1`)
- `notes` — scoring criteria inlined into the LLM prompt

The 7-facet starting set covers joy↔sadness, anxiety↔eagerness,
agency (unipolar), comfort↔discomfort, energy↔fatigue,
fulfillment (unipolar), proactive↔reactive.

Mixed bipolar / unipolar scale types was the key design
refinement vs the original plan. Some axes are genuinely
bipolar (the opposite of joy is a felt feeling — sadness);
others are unipolar because the "negative pole" reads as
absence (the opposite of agency is felt as *lack of agency*,
not a different active feeling). Forcing everything to bipolar
would be wrong in both directions — the LLM would try to
manufacture "anti-agency" scores for unipolar facets, and the
dashboard would plot them on the wrong axis half.

The loader (`src/journal/services/mood_dimensions.py`) uses
stdlib `tomllib` — no new dependency. Validates the config
aggressively: missing required fields, duplicate names,
invalid scale types, and non-snake_case names all raise
`MoodDimensionConfigError` at startup rather than silently
degrading. A misconfigured file with
`JOURNAL_ENABLE_MOOD_SCORING=true` refuses to start the server.
17 unit tests including a smoke test of the shipped config so
future edits can't break it in CI.

### 2. `AnthropicMoodScorer` via tool use

`src/journal/providers/mood_scorer.py` implements the
`MoodScorer` Protocol with an Anthropic Messages tool-use
adapter.

The key design trick is `build_tool_schema(dimensions)` — it
builds the `record_mood_scores` tool's input schema *at call
time* from the currently-loaded facets. Each facet becomes a
required sub-object with its own `minimum` / `maximum` based on
its `scale_type`. So if you have a bipolar joy_sadness and a
unipolar agency facet, the tool schema has:

```json
{
  "joy_sadness": {
    "type": "object",
    "properties": {
      "value": {"type": "number", "minimum": -1.0, "maximum": 1.0}
    }
  },
  "agency": {
    "type": "object",
    "properties": {
      "value": {"type": "number", "minimum": 0.0, "maximum": 1.0}
    }
  }
}
```

This lets Anthropic's schema validator catch a wrong-sign
unipolar score on the wire instead of clamping it client-side.
Editing the TOML file and restarting the server changes the
tool schema on the next call — no code edit needed.

The adapter defaults to **Claude Sonnet 4.5** (`claude-sonnet-4-5`)
via `MOOD_SCORER_MODEL` env var. User preference was Sonnet
over Haiku for better subjective calibration on short morning-
pages-style entries. Cost is still ~$0.006/entry, ~$0.18/month
at one entry per day.

Fallback parsing: if the response is missing a tool_use block
(rare), walk the text blocks looking for the first JSON object
and parse it as the tool payload. If that also fails, return
empty and let the service log + skip. The service layer swallows
all scorer exceptions so ingestion is never broken by a scoring
hiccup. 22 unit tests.

### 3. Repository `mood_scores` CRUD

Four new methods on `SQLiteEntryRepository`:

- `replace_mood_scores(entry_id, scores)` — idempotent
  delete-then-insert for a subset of dimensions. Only rewrites
  the dimensions named in `scores`; dimensions present in the
  DB but absent from the list are preserved. This lets the
  ingestion hook rewrite only "currently configured" dimensions
  while keeping historical scores for retired facets untouched.
- `get_mood_scores(entry_id)` — fetch all scores for an entry
  as `MoodScore` dataclasses.
- `get_entries_missing_mood_scores(dimension_names)` — drives
  backfill `--stale-only`. Returns entry ids that are missing
  at least one of the named dimensions. Preserves retired-dim
  scores (they don't count toward "complete").
- `prune_retired_mood_scores(current_names)` — deletes rows
  whose dimension is NOT in the current set. Used by
  `backfill-mood --prune-retired`. Passing an empty list wipes
  the whole `mood_scores` table (interpreted as "no dimensions
  are current").

11 repository tests covering the CRUD semantics, sparse
storage, and idempotency.

### 4. `get_mood_trends` refactor to canonical dates

`get_writing_frequency` and `get_mood_trends` now share a
`_bin_start_sql(granularity, column)` helper, and both return
`period` as a canonical ISO date (Monday for weeks, first of
month/quarter/year for the others) instead of
`%Y-W%W`-style format strings. The dashboard chart can now
plot mood trends on the same x-axis as writing-frequency
trends without any client-side date parsing.

The LLM-facing `journal_get_mood_trends` MCP tool still
accepts `day / week / month / quarter / year` for backward
compatibility — only the supported-granularity set was
*expanded*; nothing was removed. Existing LLM consumers that
display the `period` field as-is get a human-readable ISO date
instead of a week-number string, which is arguably an
improvement. 6 new tests for canonical dates + invalid
granularity + day backcompat.

### 5. `MoodScoringService` + ingestion hook

`src/journal/services/mood_scoring.py` is a thin bridge between
the scorer provider and the repository. One method:
`score_entry(entry_id, text) -> int`. Returns the number of
scores written; returns 0 (never raises) on empty text, empty
dimensions, scorer exception, or empty scorer response. The
"never raises" contract is load-bearing — ingestion must never
fail because mood scoring had a bad day.

`IngestionService.__init__` gained an optional `mood_scoring`
parameter. When set, `_process_text` calls `score_entry` at the
tail (after embeddings are persisted — so a scoring failure
cannot roll back the expensive embedding step). All ingestion
paths (image, voice, URL, multi-page, `update_entry_text`) flow
through `_process_text`, so every path is scored when the flag
is on. 6 service tests.

### 6. `backfill_mood_scores` + `journal backfill-mood` CLI

`src/journal/services/backfill.py` gained `backfill_mood_scores`
with these modes:

- `stale-only` (default) — score only entries missing a current
  dimension. Idempotent and cheap. Repeatedly running walks
  toward completeness.
- `force` — rescore every entry in the window, regardless of
  existing state. Used after editing a dimension's notes or
  labels.
- `prune_retired=True` — delete `mood_scores` rows whose
  dimension isn't in the current config. Off by default so
  historical scores survive config edits.
- `dry_run=True` — count what would change, with no LLM calls
  or DB writes. Returns a `MoodBackfillResult` with
  `dry_run=True` set.
- `start_date` / `end_date` — inclusive ISO-8601 window.

Per-entry errors are captured in `result.errors` and don't
abort the batch. 9 service tests cover every mode combination.

The CLI surface is `journal backfill-mood [--force]
[--prune-retired] [--dry-run] [--start-date] [--end-date]`.
Dry-run prints an estimated cost using Sonnet 4.5 pricing so
the user can decide before paying. 2 CLI tests (help output,
dry-run happy path).

### 7. Dashboard endpoints

Two new routes in `api.py`, both bearer-authenticated via the
app-wide middleware:

- `GET /api/dashboard/mood-dimensions` — returns the current
  facet set with scale types and score ranges. The webapp
  mood chart calls this on page load so adding a facet in the
  TOML file flows through to the UI without a frontend
  rebuild. Returns `{"dimensions": []}` when scoring is
  disabled — callers treat that as "nothing to display" rather
  than an error.
- `GET /api/dashboard/mood-trends?bin=&from=&to=&dimension=`
  — wraps `QueryService.get_mood_trends` with the new
  canonical-date format. Optional `dimension` filter. Same
  `bin` vocabulary as `/api/dashboard/writing-stats`. 400
  `invalid_bin` on unsupported granularity, 503 when services
  aren't initialised.

7 integration tests.

### 8. Config + server wiring

`src/journal/config.py` gains four new fields:

- `enable_mood_scoring: bool` — default `False`. Opt in via
  `JOURNAL_ENABLE_MOOD_SCORING=true`.
- `mood_scorer_model: str` — default `claude-sonnet-4-5`.
  Overridable via `MOOD_SCORER_MODEL`.
- `mood_scorer_max_tokens: int` — default 1024.
- `mood_dimensions_path: Path` — default
  `config/mood-dimensions.toml`.

`src/journal/mcp_server.py` conditionally loads dimensions and
instantiates `AnthropicMoodScorer` + `MoodScoringService` when
the flag is on, passes them into `IngestionService`, and adds
`mood_dimensions` and `mood_scoring` to the services dict so
the API routes can surface them. When the flag is off, both
keys are present but empty/None — the routes handle that
cleanly.

## Deliberate non-goals for this session

1. **PCA / factor analysis / correlation matrix** across
   facets. Noted in `docs/mood-scoring.md` as a Tier 3
   follow-up after ~60-100 entries of real data. No point
   building the analysis before there's data to analyze.
2. **Dashboard mood chart.** Ships in the sibling webapp commit.
3. **Dark mode for the mood chart.** Same rationale as 3a —
   low-effort iteration once there's real data to look at.
4. **Automatic re-scoring on facet edits.** Editing the TOML
   file is a config change, not a database migration. The user
   runs `journal backfill-mood --force` explicitly when they
   want to reinterpret historical entries against new criteria.
   Automatic reinterpretation on startup would burn tokens
   silently.
5. **A webapp surface for editing facets.** Out of scope.
   Facets live in a file the operator edits directly. If a
   multi-user version ever exists, facets would need to move
   into a DB table with per-user config — but that's a
   different architecture.

## Tests and quality gates

- **Before:** 454 tests passing.
- **After:** 534 tests passing. +80 new tests across 6 files:
  1. `tests/test_services/test_mood_dimensions.py` — 17
  2. `tests/test_providers/test_mood_scorer.py` — 22
  3. `tests/test_db/test_repository.py::TestMoodScoresCRUD` — 11
  4. `tests/test_db/test_repository.py::TestMoodTrendsCanonicalDates` — 6
  5. `tests/test_services/test_mood_scoring.py` — 6
  6. `tests/test_services/test_backfill.py::TestBackfillMoodScores` — 9
  7. `tests/test_api.py::TestDashboardMoodDimensions` + `TestDashboardMoodTrends` — 7
  8. `tests/test_cli.py` — 2 new CLI tests
- **`uv run ruff check`:** clean.
- **Coverage:** held.

## Follow-ups

1. **Webapp sibling commit.** Frontend mood chart (T1.3b.vii)
   ships in `journal-webapp` right after this commit lands.
2. **Rebuild and redeploy the running backend** so the new
   endpoints are reachable and (optionally) enable scoring.
   Opt in by setting `JOURNAL_ENABLE_MOOD_SCORING=true` and
   `ANTHROPIC_API_KEY` in the env, then
   `journal backfill-mood --stale-only` to score historical
   entries. The `--dry-run` flag prints the cost first so you
   can decide.
3. **Tier 3 analysis** — correlation matrix, PCA/factor
   analysis, day-of-week patterns. Needs ~60-100 scored
   entries to be meaningful. Revisit after a few weeks of real
   data.
