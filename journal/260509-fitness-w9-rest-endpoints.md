# 2026-05-09 — Fitness W9: REST endpoints

W9 from `docs/fitness-tier-plan.md`. Adds the four read-side fitness routes
(`api/fitness.py`) plus the job-creation companion `POST /api/fitness/sync/{source}`
in `api/ingestion.py`.

The pipeline was internally end-to-end after W8 (token refresh → fetch → raw archive
→ normalize → activities/daily, dispatched by `JobRunner.submit_fitness_sync_*`).
W9 makes it operator-triggerable and webapp-readable.

## What shipped

- **`GET /api/fitness/activities?start=&end=&type=`** — windowed activities,
  optional `type` filter from the seven-value enum (`run|ride|swim|walk|hike|strength|other`).
- **`GET /api/fitness/daily?start=&end=`** — windowed daily rollups.
- **`GET /api/fitness/sync/status`** — per-source `{auth_status, auth_broken_since,
  last_success_at, last_runs}` payload, or `null` when the user has never had
  any auth state or sync runs for that source.
- **`POST /api/fitness/sync/{source}`** — creates a `fitness_sync_<source>` job,
  returns `202 {job_id, status}`. Dedupes against an existing in-flight
  (queued or running) job per user+source: a second POST while one is in flight
  returns the existing `job_id` with `already_running: true`. Returns `503`
  when the source isn't configured on this server.
- **`GET /api/fitness/integrity`** — soft-pointer orphan report from
  `check_fitness_integrity`. Auth-gated even though the report is global,
  to keep the gate consistent with every other route.

Wired into `register_api_routes` (read routes) and the existing
`register_ingestion_routes` (write/job-creation route, per the documented
routing override).

## Plan drift caught

The W9 plan in `docs/fitness-tier-plan.md` had three stale assumptions, in line
with what W8 saw:

1. **Test directory.** Plan said `tests/test_api/test_fitness.py`. The repo
   convention is the flat `tests/test_api_*.py` pattern (cf. `test_api_jobs.py`,
   `test_api_ingest.py`). Used `tests/test_api_fitness.py`.

2. **"Match the existing-job posture in `api/ingestion.py` for POST
   /entries/ingest/images".** That posture doesn't exist —
   `submit_image_ingestion` always submits a fresh job, no dedup. Implemented
   the dedup the plan *intended* via a `job_repository.list_jobs(status=...,
   job_type=..., user_id=...)` lookup at the endpoint, with the new field
   `already_running: true` on the response so callers can tell. Tests cover
   the dedup explicitly.

3. **Test #5 ("anonymous request returns 401 on every endpoint").** Auth is
   enforced at the middleware layer (`RequireAuthMiddleware`), already covered
   by `tests/test_auth.py`. The existing API test pattern (`test_api_jobs.py`,
   `test_api_ingest.py`) uses `_FakeAuthMiddleware` to inject a user and never
   asserts the 401 path. Followed that convention rather than re-litigating
   middleware coverage from each route module.

## Decisions worth recording

1. **`fitness_repo` lifted into the services dict.** Bootstrap previously
   constructed `FitnessRepository(conn)` *inside* `_build_fitness_callables`
   and discarded it after wiring the callables. The new repo construction
   happens in `_init_services`, the same instance is threaded into
   `_build_fitness_callables` and added to `services["fitness_repo"]` for
   the API layer. Keeps the read API and the worker on the same lock
   discipline (the repo's `threading.Lock`).

2. **`sync/status` returns `null` per source on first use, not a
   default-populated dict.** Per the plan's response shape. Lets the webapp
   distinguish "first-use, never connected" (show the connect CTA) from
   "configured but waiting for first successful sync". Test
   `test_sync_status_empty_db_returns_null_per_source` pins this — the
   "most-likely real first-use state" the plan flagged.

3. **Dedup at the API endpoint, not via a `submit_fitness_sync_*` change.**
   The `JobRunner.submit_fitness_sync_*` methods stay write-only. Adding a
   `find_running` check inside the JobRunner would couple the W6 fetch
   service's single-run guard semantics ("a run is already in flight, queue
   nothing") to the queue-time submit path; better to keep the operator-facing
   "you already have one going" decision at the HTTP boundary. The W6 guard
   still handles racy parallel submits as a defence-in-depth.

4. **`already_running: true` is a top-level response field, not a status
   value.** Keeps the existing `{job_id, status}` shape monomorphic for
   callers — `status` reflects the actual job state ("queued" or "running"),
   and the new boolean is the explicit "this is the existing job, not a
   newly-created one" hint. Webapp work (W15) can rely on this without
   parsing job status strings.

5. **Unconfigured-source returns 503.** `submit_fitness_sync_strava` raises
   `RuntimeError("not configured")` when `STRAVA_CLIENT_ID` /
   `STRAVA_CLIENT_SECRET` are unset (W8 decision #2). Surfacing this as 503
   keeps the wire-format consistent with the "Server not initialized" 503
   already used by every other route, and it's distinct from a 500 so an
   operator can tell "feature off" from "real bug". The error message
   ("Strava fitness sync is not configured on this server") is the same one
   `JobRunner` raises so the operator sees the underlying reason.

6. **Activity orphans use `dataclasses.asdict`.** `ActivityOrphan` and
   `DailyOrphan` from `db/fitness_integrity.py` are frozen dataclasses
   shaped exactly to the plan's response contract. Using `asdict` keeps the
   serializer one line and means future field additions to the dataclasses
   propagate without hand-editing the API.

## What's not done yet

1. **MCP tools — W10.** `mcp_server/tools/fitness.py` is the next unit;
   per master plan D6 every meaningful query and operational lever should
   also be reachable via MCP. `_VALID_SOURCES` and the per-source status
   helper in `api/fitness.py` are not yet shared with that module — when
   W10 lands, factor any duplication into `services/fitness/` rather than
   extending the API module.

2. **CLI re-auth flow — W11.** `journal fitness-auth` for first-token
   acquisition and the documented cron entry that hits `submit_fitness_sync_*`
   daily. W11 is the unblocker for W13 (first live smoke test) — none of
   the W9 routes can be exercised end-to-end against the real Strava /
   Garmin APIs until tokens are populated.

3. **Health endpoint extension — W12.** `/api/health` will surface the
   per-source auth status (the same fields as `sync/status` but condensed)
   so the existing health poller picks up `auth_broken` without needing a
   separate fitness probe.

4. **Docs — W14.** `docs/api.md` doesn't yet describe the W9 endpoints.
   The exact response shapes here become the contract for W15 (webapp).

## Pinned

- No new dependencies. Pure orchestration on top of W2 (`FitnessRepository`),
  W6/W7/W8 (`submit_fitness_sync_*`), and `db/fitness_integrity.py`.

## Tests

- 2064 passed (2046 prior baseline + 18 new in `tests/test_api_fitness.py`).
  0 failed.
- Lint clean (ruff). No new noqa annotations.
- Coverage: each W9 route has ≥1 test; the `sync_status` empty-DB and
  populated paths are both covered, the integrity check is exercised
  with both clean and dirty DB fixtures, and the dedup posture for
  `POST /sync/{source}` is pinned by an explicit test that pre-seeds a
  running job row.
