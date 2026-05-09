# 2026-05-09 — Fitness W12: health endpoint extension

W12 from `docs/fitness-tier-plan.md`. Surfaces per-source fitness state on
`/api/health` so an operator inspecting health sees "Strava broke at 04:01
yesterday" without hitting `/api/fitness/sync/status` separately. Adds a 48h
broken-auth threshold that drops `overall_status` to `degraded`.

## What shipped

1. **`src/journal/db/fitness_repository.py`** — new
   `get_health_summary(user_id)` returning a list of dicts, one per source
   the user has configured. Single-query: UNION subquery enumerates sources
   from `fitness_auth_state` ∪ `fitness_sync_runs`, LEFT JOIN pulls auth_state
   columns, correlated subquery pulls the latest successful run's
   `started_at`. Empty list when neither table has rows for the user (do not
   emit a stub of nulls). Sources are returned ordered alphabetically.

2. **`src/journal/services/liveness.py`** — new `check_fitness_freshness`
   helper that consumes the summary list and returns a `ComponentCheck`.
   Returns `degraded` when any source has `auth_status='broken'` with
   `auth_broken_since` more than `threshold_hours` ago; otherwise `ok`.
   Strict greater-than at the boundary (exactly 48h is still `ok`). Treats
   missing/malformed `auth_broken_since` as `ok` rather than crashing —
   shouldn't happen per W6's transition_auth contract but the rollup must
   not be load-bearing on data hygiene. Status is never `error`.

3. **`src/journal/api/health.py`** — refactored to share a
   `_build_health_payload(services, *, fitness_user_id)` builder between
   `/health` and `/api/health`. The fitness block and the freshness check
   are added only when `fitness_user_id` is non-None (i.e. only on
   `/api/health`). The unauthenticated `/health` is otherwise unchanged.

4. **`src/journal/config.py`** — `FITNESS_HEALTH_BROKEN_DEGRADED_HOURS`
   (default 48) flows through `Config.fitness_health_broken_degraded_hours`,
   validated `>= 1` in `__post_init__`.

5. **Tests** — 7 new in `tests/test_db/test_fitness_repository.py` for
   `get_health_summary`; 8 new in `tests/test_services/test_liveness.py`
   for `check_fitness_freshness`; 6 new `TestApiHealthFitness` class in
   `tests/test_api.py` covering both endpoints' behaviour. Total: 2119
   passing (was 2090 — net +29 includes a few skip-count adjustments).
   Lint clean.

## Decisions worth recording

1. **`/health` (unauth) gets *no* fitness block at all.** The original W12
   plan said "both endpoints surface the new payload." Deviation rationale:
   `auth_broken_since` is per-user state; surfacing it on the unauth probe
   would let any anonymous caller enumerate which users have configured
   Strava/Garmin and *when* their auth broke. The Docker healthcheck and any
   external uptime probe only need "is the server up?" — they don't need
   per-user integration state. The 48h-broken downgrade does *not*
   propagate to the public probe; the operator's webapp `/api/health` view
   does, and that's where the operator looks. Plan doc updated to reflect
   the deviation and pin the rationale before W13/W14 reach for it. Trade:
   if the public probe is the only thing watching, a long-broken
   integration won't surface there. Acceptable — the operator-facing path
   (the webapp) is the actual notification surface.

2. **`degraded`, not a new `warning` tier.** Existing `liveness.py`
   `StatusLevel` literal is `ok | degraded | error`. The plan's wording
   matched the existing taxonomy; the open question in the W12 brief
   resolved trivially after reading the literal.

3. **Single repo method, not per-source loops.** `get_health_summary` does
   one SQL query per `/api/health` hit. The naive `for source in
   ('strava','garmin')` would hit auth_state and sync_runs twice each =
   four queries per request. The endpoint is low-traffic so the perf
   delta is negligible, but keeping the omit-if-not-configured logic in
   SQL means callers can't accidentally emit a stub-of-nulls. (The
   webapp's W15 sync/status view already calls a separate per-source
   helper; no need to share — the shapes are different.)

4. **Clock = `datetime.now(UTC)`.** Matches `_now_iso` in the fitness
   repository and the W6 fetch service. No clock shim — the W6 service
   uses `datetime.now(UTC)` directly, so the freshness check follows the
   same convention. Tests pass an explicit `now` arg.

5. **Strict `>`, not `>=`, on the threshold boundary.** A breakage exactly
   48h old is still `ok`; one second past is `degraded`. Pinning this with
   a test so a future tweak doesn't silently flip flap behaviour. The
   alternative (`>=`) means the rollup flips at the moment the threshold
   is hit, which would race against the polling cadence.

6. **`check_fitness_freshness` never returns `error`.** A broken
   integration is operator information, not a server outage. Reserving
   `error` for components that are actually unreachable (sqlite,
   chromadb).

7. **Refactor pulled the payload-builder out of the route handler.** The
   original `get_health` was a single async function and `/api/health`
   delegated to it directly. With per-user fitness, the two endpoints
   diverge — extracting `_build_health_payload(*, fitness_user_id)` keeps
   the divergence in one place and avoids duplicating the ingestion +
   queries + checks setup. `/health` and `/api/health` now both call the
   builder with different `fitness_user_id` values.

## Plan corrections (W8/W9/W10/W11/W12 — five-in-a-row)

1. **Test path: `tests/test_api/test_health.py` → `tests/test_api.py::TestHealth`.**
   The repo has a flat `tests/test_api.py` with topical classes; there is
   no `tests/test_api/` directory. The new `TestApiHealthFitness` class
   lives next to `TestHealth` in the same flat file. (W11 caught the
   `tests/test_cli/` variant of this drift; W12 caught the `tests/test_api/`
   variant. Both directories don't exist; the convention is flat
   `test_<area>.py` plus topical `test_api_<area>.py` siblings.)

2. **`FitnessRepository` was already wired into `services["fitness_repo"]`
   in W6/W9.** Plan said "register it in `_init_services` alongside
   `entry_repo` etc." That work was already done — confirmed by reading
   `src/journal/mcp_server/bootstrap.py:634` — so W12 only had to read
   from the existing key, not register a new one.

3. **The `Status` literal is `ok | degraded | error` — no `warning` tier.**
   The W12 brief's open question #3 ("should overall_status go straight
   from ok to degraded, or through warning?") resolved by reading
   `services/liveness.py` line 33. Plan said `degraded` directly, taxonomy
   confirms.

## What's not done yet

1. **No new ingestion/normalize queue-depth or worker-activity surfacing on
   `/health`.** Out of scope per the plan; that's a separate concern.
2. **No webapp surface.** That's W15 — webapp will consume the new
   `/api/health` `fitness` block.
3. **No live smoke.** That's W13 — first time any of this hits real
   Strava/Garmin tokens.

## Files

- `docs/fitness-tier-plan.md` — modified (W12 section rewritten with
  decisions and corrected paths)
- `src/journal/api/health.py` — modified (extracted `_build_health_payload`,
  added fitness block + check on authed route only)
- `src/journal/db/fitness_repository.py` — modified (`get_health_summary`)
- `src/journal/services/liveness.py` — modified (`check_fitness_freshness`)
- `src/journal/config.py` — modified
  (`fitness_health_broken_degraded_hours`)
- `tests/test_db/test_fitness_repository.py` — modified (+7 tests)
- `tests/test_services/test_liveness.py` — modified (+8 tests)
- `tests/test_api.py` — modified (+1 import, +6 tests as
  `TestApiHealthFitness`, dropped string forward-refs)

## Tests

- 2119 passed, 0 failed.
- Lint clean (ruff). No new noqa annotations.
- The "I/O operation on closed file" log noise on test teardown comes from
  the `health_poll.py` background thread on shutdown — preexisting,
  unrelated to W12.

## Next

W13 — first live smoke test against real Strava/Garmin tokens. Different
character from W4–W12 (needs creds, observable side effects, doc journal
of what the providers actually returned). Recommend a fresh session for
that — different shape than the in-memory unit-test cadence.
