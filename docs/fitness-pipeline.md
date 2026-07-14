# Fitness Pipeline

**Status:** active. **Last updated:** 2026-07-14 (Strava mothball note; content otherwise 2026-05-10).

> **Strava mothballed (2026-07-14):** the Strava half of this pipeline is
> gated behind `STRAVA_ENABLED=false` (the default) — routes 404, scheduler
> and CLI are Garmin-only. The Strava layers below are accurate but dormant;
> historical Strava rows are kept and still served. See
> [`fitness-operations.md` § Reviving Strava](fitness-operations.md#reviving-strava)
> and roadmap D8.

Engineer-facing overview of how fitness data flows from a user's Garmin watch
or Strava app into a queryable journal database. This doc walks the layers and
points at the detail docs; it does not relitigate decisions or restate schema.

For architectural decisions and constraints, read
[`fitness-integration-plan.md`](fitness-integration-plan.md).
For tables, columns, and migration sequencing, read
[`fitness-schema.md`](fitness-schema.md). For the original W1–W15 execution
sequencing (now closed), see
[`archive/fitness-tier-plan.md`](archive/fitness-tier-plan.md). For operator
runbooks (re-auth, backfill, troubleshooting), read
[`fitness-operations.md`](fitness-operations.md).

---

## Data flow

```
                    ┌──────────────────┐
   Garmin watch →   │                  │
                    │  Garmin Connect  │
   Garmin Connect → │      cloud       │
                    └────────┬─────────┘
                             │
                             ▼
                  ┌─────────────────────┐
                  │ python-garminconnect│  (W5 provider —
                  │  GarminConnectGarmin│   `providers/garmin.py`)
                  │       Provider      │
                  └────────┬────────────┘
                           │
   Strava app    ┌─────────┴───────────┐
       │         │                     │
       ▼         │    Strava OAuth     │
  Strava cloud → │      `stravalib`    │  (W4 provider —
                 │  StravalibStrava    │   `providers/strava.py`)
                 │       Provider      │
                 └─────────┬───────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │  W6 FetchService       │   `services/fitness/fetch.py`
              │  (state machine,       │   per-source: Strava | Garmin
              │   single-run guard,    │
              │   auth-broken         │
              │   classification,      │
              │   transient retry)     │
              └────────┬───────────────┘
                       │
                       ▼
          ┌──────────────────────────┐
          │  fitness_raw_strava /    │   Sacred raw archive —
          │  fitness_raw_garmin      │   payload SHA dedup,
          │                          │   never modified after insert
          └────────┬─────────────────┘
                   │
                   ▼
          ┌──────────────────────────┐
          │  W7 Normalize service    │   `services/fitness/normalize.py`
          │  (raw → activities/      │
          │   daily, idempotent      │
          │   upserts, drift         │
          │   notifications)         │
          └────────┬─────────────────┘
                   │
                   ▼
          ┌──────────────────────────┐
          │ fitness_activities       │   Per-activity rows from both
          │ fitness_daily            │   sources (run, ride, swim,
          │                          │   walk, hike, strength, other)
          │                          │   Daily wellness rollups
          │                          │   (sleep, HRV, body battery,
          │                          │   training load / readiness)
          └────────┬─────────────────┘
                   │
                   ▼
          ┌──────────────────────────┐
          │  W9 REST + W10 MCP       │   REST: `api/fitness.py`,
          │  read surface +          │         `api/ingestion.py` (POST)
          │  correlation queries     │   MCP : `mcp_server/tools/`
          │                          │         `fitness.py`
          └──────────────────────────┘
```

The four-layer split (provider → fetch → raw → normalize) is the load-bearing
shape — it lets a downstream change (e.g. a new metric in the normalized
layer) re-run against the raw archive without re-fetching, and it isolates
"what the upstream API said today" from "the canonical thing we query".

---

## Layers in detail

### Provider (W4 + W5)

Two protocols (`StravaProvider`, `GarminProvider`) with one concrete adapter
each (`StravalibStravaProvider`, `GarminConnectGarminProvider`). Provider
methods return raw upstream payloads (Strava API objects via `stravalib`,
Garmin Connect dict payloads via `garminconnect`). Token refresh and
auth-state mutation flow back through `persist_tokens` callbacks. The
provider layer is the only place that knows about upstream schemas.

Pinned dependencies: `stravalib==2.4` (semver-loose pin), `garminconnect`
exact-pinned (per W2 reliability research). `garth` (Garmin's transitive auth
library) follows `garminconnect`'s pin — never pinned separately.

### Fetch service (W6)

`StravaFetchService` and `GarminFetchService` (both in `services/fitness/
fetch.py`) drive one sync run from start to terminal status. The state
machine owns:

1. **Single-run guard.** A `running` row in `fitness_sync_runs` for the same
   `(user_id, source)` short-circuits with `status="running"`. Routine
   callers tolerate this; backfill upgrades it to a `BackfillBlocked`
   exception.
2. **Auth-broken classification.** A `FitnessAuthError` from the provider
   transitions `fitness_auth_state.auth_status` to `broken`, stamps
   `auth_broken_since`, fires the once-per-transition Pushover via the
   notifier, and returns `status="auth_broken"`.
3. **Transient classification.** Any other exception is recorded as
   `transient_failure` with the friendly error message.
4. **Audit trail.** Every code path produces a `fitness_sync_runs` row —
   the durable record the webapp reads, `fitness-status` prints, and
   `/api/health` filters on.

Per-source `_has_credentials` hooks short-circuit when no credentials are
available — Strava checks `access_token`; Garmin checks
`extra_state["tokens_blob"]` (the W11 reauth shape — Garmin's `access_token`
column stays `NULL`).

### Raw archive

Per-source SQLite tables (`fitness_raw_strava`, `fitness_raw_garmin`) hold
upstream payloads with a payload-SHA primary key. Inserts are
`INSERT OR IGNORE`, so repeating a fetch is idempotent. The raw archive is
**sacred** — nothing in the pipeline modifies a row after insert. If a
normalized projection turns out wrong, fix the normalize service and re-run;
the raw rows always represent what the upstream API actually said.

### Normalize service (W7)

`normalize_strava` / `normalize_garmin` read unprojected raw rows
(`fetched_at > MAX(fetched_at) over already-normalized rows`), translate
them into `fitness_activities` and `fitness_daily` upserts, and emit drift
notifications when a normalize pass surfaces a schema-shape change in the
upstream payload.

Idempotent: `INSERT OR REPLACE` on `(user_id, source, source_id)` for
activities and `(user_id, source, local_date)` for daily. A second pass over
the same window is harmless.

The watermark currently uses strict `>` on a 1-second-resolution
`fetched_at`, which underprojects under dense writes. See
[`fitness-operations.md` §8](fitness-operations.md#8-known-limitations) for
the operator-facing recovery and the planned fix options.

### Job workers (W8)

`run_fitness_sync_strava` / `run_fitness_sync_garmin` (in
`services/jobs/workers/`) wrap fetch + normalize behind the JobRunner. They
inspect `FitnessSyncResult.status` and translate it into
`mark_succeeded` / `mark_failed` on the `jobs` row plus a Pushover via
the notifier. The CLI (`fitness-sync`, `fitness-backfill`) bypasses
JobRunner — short-lived process, no `ThreadPoolExecutor` shutdown hazard.

### REST + MCP surface (W9 + W10)

Read endpoints (`api/fitness.py`) expose `/api/fitness/{activities,daily,
sync/status,integrity}`. Job creation lives at
`POST /api/fitness/sync/{source}` in `api/ingestion.py` per the codebase's
write-in-ingestion-router convention.

The MCP twin (`mcp_server/tools/fitness.py`) mirrors the read endpoints
plus three correlation queries (`fitness_correlate_sleep_mood`,
`fitness_correlate_weekly_runs_stress`, `fitness_correlate_hrv_mood`)
copied verbatim from
[`fitness-schema.md` §8](fitness-schema.md#8-correlation-queries-proves-schema-supports-them).

Backfill is CLI-only (`journal fitness-backfill`) — no REST surface. The
operator runs it once per source, end-to-end. See
[`fitness-operations.md` §3](fitness-operations.md#3-historical-backfill).

---

## Where to look in code

| Concern | Module |
|---|---|
| Provider protocols + adapters | `src/journal/providers/{strava,garmin}.py` |
| Fetch state machine | `src/journal/services/fitness/fetch.py` |
| Normalize | `src/journal/services/fitness/normalize.py` |
| Backfill orchestrator | `src/journal/services/fitness/backfill.py` |
| Activity-type collapse map | `src/journal/services/fitness/_activity_type_map.py` |
| Repository | `src/journal/db/fitness_repository.py` |
| Integrity check | `src/journal/db/fitness_integrity.py` |
| Schema migrations | `src/journal/db/migrations/{0023,0024,0025}_fitness_*.sql` |
| Job workers | `src/journal/services/jobs/workers/fitness_sync_{strava,garmin}.py` |
| REST routes (read) | `src/journal/api/fitness.py` |
| REST route (POST sync) | `src/journal/api/ingestion.py` (search `api_fitness_sync`) |
| Health-block integration | `src/journal/api/health.py` |
| MCP tools | `src/journal/mcp_server/tools/fitness.py` |
| CLI subcommands | `src/journal/cli/fitness.py` |
| Config fields | `src/journal/config.py` (search `# Fitness pipeline`) |
| Tests | `tests/test_services/test_fitness/`, `tests/test_db/test_fitness_*`, `tests/test_api_fitness.py`, `tests/test_cli_fitness.py`, `tests/test_mcp/test_fitness_tools.py` |

---

## Single-user posture (today)

Every fitness table carries `user_id` so multi-user is a future migration
rather than a rewrite, but the running deployment is single-user
(`user_id=1`). The CLI and the REST endpoints all default to `user_id=1`.
Cross-source dedup (a Strava run uploaded from a Garmin watch appears in
both raw tables) is an integrate-time concern, not a storage-time one —
the schema captures both, and the webapp (W15) is where the user-facing
"distinct workouts" reconciliation lives.
