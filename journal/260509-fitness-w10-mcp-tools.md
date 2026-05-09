# 2026-05-09 — Fitness W10: MCP tools

W10 from `docs/fitness-tier-plan.md`. Adds the eight `@mcp.tool()` registrations
in `mcp_server/tools/fitness.py`, the MCP twin of the W9 REST surface plus three
correlation queries that are MCP-only.

Master plan D6: every meaningful query and operational lever is also an MCP
tool. With W9 (REST) + W10 (MCP) shipped, an external agent can both refresh
fitness data and query it without going through the REST API. W11 (CLI re-auth)
is the next blocker for the first real sync.

## What shipped

Eight `@mcp.tool()` functions, all returning JSON-serialisable dicts:

- **`fitness_list_activities(start, end, activity_type=None)`** — windowed
  activities. Same shape as `GET /api/fitness/activities`.
- **`fitness_list_daily(start, end)`** — windowed daily rollups. Same shape
  as `GET /api/fitness/daily`.
- **`fitness_sync_status()`** — per-source `null`-or-status snapshot. Same
  shape as `GET /api/fitness/sync/status`.
- **`fitness_integrity_check()`** — soft-pointer orphan report. Same shape as
  `GET /api/fitness/integrity`.
- **`fitness_trigger_sync(source)`** — enqueue a `fitness_sync_<source>` job
  with the same dedup posture as `POST /api/fitness/sync/{source}` (returns
  the existing in-flight `job_id` with `already_running: true`). Errors
  (unknown source, source not configured) are returned as structured dicts
  with `job_id: None`, not raised — matches the `journal_get_job_status` /
  batch-job-tool convention so the LLM can read and respond.
- **`fitness_correlate_sleep_mood(start, end)`** — Q1 from
  `fitness-schema.md` §8 (sleep score × energy & joy, daily grain).
- **`fitness_correlate_weekly_runs_stress(start, end)`** — Q2 (Mon-of-week
  bucketed running distance × frustration mood as stress proxy).
- **`fitness_correlate_hrv_mood(start, end, window=7)`** — Q3 (rolling
  calendar-day HRV × joy & energy).

The three correlation queries are reproduced **verbatim** from
`fitness-schema.md` §8 — that doc is the source of truth (the queries were
the schema's acceptance test). Tests pin the year-boundary case (Q2) and
the missing-day case (Q3), which were the schema's two specific gotchas.

`tools/_ctx.py` gained `_get_fitness_repo` and `_get_db_conn` helpers.
`mcp_server/__init__.py` re-exports the eight new tool symbols.

## Plan drift caught

Two pieces of W10 plan drift, both of the same flavour as the W8/W9 ones —
file-path assumptions that didn't survive contact with the actual layout:

1. **Test directory.** Plan said
   `tests/test_mcp_server/test_tools/test_fitness.py`. There is no
   `tests/test_mcp_server/` directory at all in this repo — the existing
   `tests/test_mcp_server.py` is a flat module that exercises tool *handlers*
   via `QueryService` rather than going through MCP. Used the flat
   convention: `tests/test_mcp_tools_fitness.py`. The W9 plan made the
   identical mistake about `tests/test_api/test_fitness.py`; this is now
   the third occurrence.

2. **Tool registration target file.** Plan said modify
   `mcp_server/__init__.py` and explicitly noted "NOT via `tools/__init__.py`
   (which is currently a docstring-only file with no imports). The earlier
   draft pointed at the wrong file." The plan's correction was correct —
   `mcp_server/__init__.py` is the package facade that re-exports tool
   symbols. Side-effect imports there register the tools.

## Decisions worth recording

1. **Tools return dicts, not formatted strings.** The existing `tools/queries.py`
   tools return human-readable strings (search results with bullet points etc.);
   `tools/jobs.py` returns dicts. For fitness data — tabular activity lists,
   daily metrics, correlation outputs — structured dicts are the right call.
   The plan said "All return JSON-serialisable dicts/lists" and a meta-test
   pins this.

2. **Correlation SQL is duplicated verbatim from the schema doc.** The
   alternative was to factor the queries into a `services/fitness/queries.py`
   module that both MCP tools and a future REST endpoint could share. Decided
   against: there is currently exactly one caller (the MCP tool), the queries
   are stable (they're the schema's acceptance test), and the doc — not code —
   is the source of truth. Module-level docstring tells future readers to
   change the schema doc first and copy here. If a second caller appears
   (W12 health endpoint? a CLI command?) this is a one-shot extract.

3. **`fitness_trigger_sync` re-implements the W9 dedup posture at the tool
   layer instead of factoring it into a service.** Looked at extracting a
   `services/fitness/dedup.py` shared between the REST route and the MCP
   tool. Decided against: the dedup is six lines of `job_repository.list_jobs`
   calls and an early-return; both call sites are short; sharing through a
   service introduces an indirection without buying much. If W14 (docs)
   surfaces a third call site, factor it then.

4. **Errors return structured dicts, never raise.** Matches
   `tools/jobs.py`'s convention so the LLM can read and respond. Unknown
   source → `{"error": "...", "job_id": None}`. Source not configured →
   same shape, with the underlying `RuntimeError` message preserved
   (`"Strava fitness sync is not configured on this server (...)"` —
   verbatim from `JobRunner.submit_fitness_sync_strava`).

5. **`_get_db_conn` is an explicit helper, not just a `_get_query` cousin.**
   The correlation queries run hand-written SQL across `fitness_*` tables,
   `entries`, and `mood_scores` — wrapping them as repository methods would
   add ceremony for queries that are stable, schema-doc-anchored, and used
   in exactly one place. The helper makes it explicit when a tool reaches
   for the raw connection rather than going through a typed repository.

6. **Reuse `api/fitness.py` serializers from the MCP tools.** Both layers
   produce the same JSON shapes for activities and daily rows. Importing
   `_activity_to_dict` / `_daily_to_dict` / `_per_source_status` from the
   API module keeps the shapes locked together; if either layer drifts,
   the other follows automatically. The leading underscore is more
   "package-private" than "API-only" in this codebase (cf. `_ctx.py`).

7. **Window-bounds check on Q3 returns a structured error, not a raise.**
   Same convention as the operational tools — `window < 1` →
   `{"error": "...", "rows": []}`. The LLM gets a clean explanation
   without an exception trace.

## What's not done yet

1. **W11 — CLI re-auth + first-run flow.** This is the actual blocker
   for the first real sync. Until `fitness-reauth-strava` /
   `fitness-reauth-garmin` ship, `fitness_auth_state` rows can only be
   inserted via tests or direct SQL — so `fitness_trigger_sync` at runtime
   will hit the W6 fetch service's "MissingAuthState" silent-auth-broken
   path on every call (W6 decision #3).

2. **W12 — Health endpoint.** The `auth_status` field surfaced by
   `fitness_sync_status` is tomorrow's `/api/health` payload. When W12
   lands, it'll consume the same `_per_source_status` helper.

3. **W13 — First live smoke test.** Needs W11 first; the entire fitness
   pipeline has been exercised against fixtures only.

4. **W14 — Docs.** `docs/api.md` doesn't yet describe the W9 endpoints,
   and an MCP tool reference in `docs/external-services.md` (or a fitness-
   specific MCP doc) is still missing. The MCP tool descriptions in
   `tools/fitness.py` are the inline contract for now.

## Tests

- 2084 passed (2064 prior baseline + 20 new in
  `tests/test_mcp_tools_fitness.py`). 0 failed.
- Lint clean (ruff). No new noqa annotations.
- Coverage notes:
  - **Q2 year-boundary case** is pinned by
    `test_correlate_weekly_runs_stress_handles_year_boundary` — the
    schema doc's specific warning that `strftime('%Y-%W')` would silently
    halve a Dec/Jan-spanning week.
  - **Q3 missing-day case** is pinned by
    `test_correlate_hrv_mood_rolling_window_handles_missing_days` — the
    schema doc's specific warning about `ROWS BETWEEN N PRECEDING`
    silently widening the window when sync gaps exist.
  - **Tool registry** is verified by `test_all_eight_tools_registered`
    against the FastMCP `_tool_manager._tools` registry.
  - **JSON-serialisability** is verified by
    `test_tools_return_json_serialisable_dicts` (round-trips every tool's
    payload through `json.dumps`).

## Pinned

- No new dependencies. Pure orchestration on top of W2 (`FitnessRepository`),
  W6/W7/W8 (`submit_fitness_sync_*`), W9 (API serializers + per-source
  status helper), and `db/fitness_integrity.py`.
