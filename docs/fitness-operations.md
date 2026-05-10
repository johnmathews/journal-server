# Fitness Operations

**Status:** active. **Last updated:** 2026-05-10.

Operator-facing runbook for the fitness pipeline. Covers initial setup, re-auth,
historical backfill, sync monitoring, and troubleshooting the rough edges that
surfaced during the W13 first live fetch (2026-05-10).

For the architectural picture and data flow, see
[`fitness-pipeline.md`](fitness-pipeline.md). For schema and migration details,
see [`fitness-schema.md`](fitness-schema.md). The decisions and constraints
behind every piece below are in
[`fitness-integration-plan.md`](fitness-integration-plan.md). The original
W1–W15 execution sequencing is archived at
[`archive/fitness-tier-plan.md`](archive/fitness-tier-plan.md) (closed
2026-05-10).

---

## Contents

1. [Configuration prerequisites](#1-configuration-prerequisites)
2. [Initial re-auth](#2-initial-re-auth)
3. [Historical backfill](#3-historical-backfill)
4. [Routine sync](#4-routine-sync)
5. [Status, health, and integrity](#5-status-health-and-integrity)
6. [Troubleshooting](#6-troubleshooting)
7. [Known limitations](#7-known-limitations)

---

## 1. Configuration prerequisites

Both sources need credentials in the server environment before re-auth can run.
Strava also needs a registered API application.

| Variable | Source | Required for |
|---|---|---|
| `STRAVA_CLIENT_ID` | Strava | Strava re-auth + sync |
| `STRAVA_CLIENT_SECRET` | Strava | Strava re-auth + sync |
| `STRAVA_REDIRECT_URI` | Strava (default `http://localhost:8400/strava/callback`) | Strava re-auth listener bind + authorize URL |
| `GARMIN_USERNAME` | Garmin | Garmin re-auth + sync |
| `GARMIN_PASSWORD` | Garmin | Garmin re-auth + sync |
| `FITNESS_BACKFILL_START` | both (default `2026-01-01`) | `fitness-backfill` default `--start` |
| `FITNESS_TRANSIENT_FAILURE_THRESHOLD` | both (default `3`) | W6 fetch service auth-broken trip + backfill streak abort |
| `FITNESS_HEALTH_BROKEN_DEGRADED_HOURS` | both (default `48`) | `/api/health` downgrade when a source has been broken longer than this |

See [`configuration.md`](configuration.md#optional--fitness-integration) for
the canonical reference.

For Strava-app registration steps, see
[`archive/fitness-tier-plan.md` §1](archive/fitness-tier-plan.md#1-preparation--credential-acquisition-blocking-p0).

---

## 2. Initial re-auth

Each source is bootstrapped once via a CLI subcommand. Re-auth is also the
recovery path when a source transitions to `auth_status="broken"` (see
[§5](#5-status-health-and-integrity)).

### 2a. Strava — laptop / dev box

When the journal-server is **not** running on the same host (or is not bound to
the redirect URI's port), re-auth is straightforward:

```bash
uv run journal fitness-reauth-strava --user-id 1
```

The command prints an authorize URL and a status line, then blocks on a
one-shot HTTP listener at the host/port from `STRAVA_REDIRECT_URI`. Open the
URL in any browser, approve the prompt, Strava redirects to the listener, and
tokens persist into `fitness_auth_state` with `auth_status="ok"`.

### 2b. Strava — headless deployment (server already on `:8400`)

The default `STRAVA_REDIRECT_URI` reuses port `8400`, which the long-running
journal-server is already bound to. Two workarounds; the inline-python recipe
below is the recommended path.

**Recommended — skip the listener, exchange the code inline.** Build the
authorize URL by hand, paste it into a browser, copy the `code` param out of
the redirect URL bar (the browser tab will fail to load — that's fine), then
exchange the code in a one-off Python invocation against the live container:

```bash
# 1. Build the authorize URL (replace <CLIENT_ID> with the value from .env).
echo "https://www.strava.com/oauth/authorize?client_id=<CLIENT_ID>&response_type=code&redirect_uri=http://localhost:8400/strava/callback&approval_prompt=auto&scope=read,activity:read_all"

# 2. Open it in a browser, approve, then copy the `code=...` value
#    out of the URL bar after the redirect-load fails.

# 3. Exchange the code inside the running container.
docker exec -it journal-server uv run python -c "
import os
from journal.config import Config
from journal.providers.strava import exchange_code
from journal.db.connection import get_connection
from journal.db.migrations import run_migrations
from journal.db.fitness_repository import FitnessRepository
from journal.models import FitnessAuthState
from datetime import UTC, datetime

cfg = Config()
tokens = exchange_code(
    client_id=cfg.strava_client_id,
    client_secret=cfg.strava_client_secret,
    code='<PASTE_CODE_HERE>',
)
conn = get_connection(cfg.db_path)
run_migrations(conn)
repo = FitnessRepository(conn)
existing = repo.get_auth_state(user_id=1, source='strava')
now = datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
repo.upsert_auth_state(FitnessAuthState(
    user_id=1, source='strava',
    access_token=tokens['access_token'],
    refresh_token=tokens['refresh_token'],
    token_expires_at=tokens['token_expires_at'],
    extra_state=dict(existing.extra_state) if existing else {},
    last_successful_login_at=now,
    last_refresh_at=existing.last_refresh_at if existing else None,
    auth_status='ok',
    auth_broken_since=None,
    created_at=existing.created_at if existing else '',
))
print('Strava re-auth complete — tokens persisted.')
"
```

This bypasses the W11 listener entirely. Same DB shape, same audit trail. No
container restart, no port juggling.

**Fallback — stop-server + one-off container with `--service-ports`.** If you
want the native OAuth roundtrip (e.g. to verify the listener works), free port
`8400` and run the CLI in a fresh container that publishes it:

```bash
docker compose stop journal-server
docker compose run --rm --service-ports journal-server \
    uv run journal fitness-reauth-strava --user-id 1
docker compose up -d journal-server   # restart the long-running server
```

Brief downtime (30–60s plus however long you take to authorize). The listener
in the one-off container binds the host's `:8400`, the browser hits it, the
code is exchanged, the token is written to the same SQLite file, and the
restarted server picks it up.

**Browser on a remote laptop?** SSH-forward the listener port (`-L
8400:localhost:8400 user@vm`) **and** apply the stop-server-plus-`--service-ports`
recipe on the VM. Two SSH sessions, fragile. The inline-python recipe above
makes this go away — strongly preferred for headless deployments.

### 2c. Garmin

Garmin uses username/password auth (with optional MFA), not OAuth. No listener,
no port collision:

```bash
docker exec -it journal-server uv run journal fitness-reauth-garmin --user-id 1
```

The command logs into `connect.garmin.com` via `python-garminconnect`, prompts
for an MFA code on stdin if Garmin asks for one, and persists the resulting
token blob into `fitness_auth_state.extra_state_json["tokens_blob"]`. (Unlike
Strava, Garmin's `access_token` column stays `NULL` — the live credential lives
in `extra_state` because that's the shape `garth` produces.) Subsequent syncs
read the blob, not the password.

The `garminconnect` library tries multiple transports in sequence and may print
intermediate `429: Mobile login returned 429 — IP rate limited by Garmin` lines
before a third transport succeeds. **The `Garmin re-auth complete — token blob
persisted.` line is authoritative.** The 429 lines are not failures (see
[§6](#6-troubleshooting)).

---

## 3. Historical backfill

After both sources are authorized, populate history with the backfill orchestrator:

```bash
docker exec journal-server uv run journal fitness-backfill \
    --source both \
    --start 2026-01-01 \
    --user-id 1
```

The orchestrator walks `[start, end]` in 30-day windows, calling the W6 fetch
service once per window. Per-window progress is recorded in `fitness_sync_runs`
in real time, so `journal fitness-status` and `/api/health` reflect progress
during a long Garmin run.

Defaults: `--start` falls back to `FITNESS_BACKFILL_START` (default
`2026-01-01`), `--end` falls back to today (UTC). `--source` accepts `strava`,
`garmin`, or `both`.

**Resume.** Re-running is safe and cheap. Per-source watermarks
(`MAX(local_date)` over normalized rows, with Garmin using `min(activities,
daily)`) skip windows already fully ingested. Mid-window crashes leave clean
state because the raw layer is `INSERT OR IGNORE` on payload SHA and
normalized rows are upserts.

**After a fresh backfill, run a force-renormalize once.** During a fast
backfill the per-window normalize step undercounts — multiple raw rows can
land within the same SQLite-1-second `fetched_at` and the W7 watermark's
strict `>` filter excludes the second-and-later rows. The recovery is
idempotent:

```bash
docker exec journal-server uv run python -c "
from journal.config import Config
from journal.db.connection import get_connection
from journal.db.migrations import run_migrations
from journal.db.fitness_repository import FitnessRepository
from journal.services.fitness.normalize import normalize_strava, normalize_garmin

cfg = Config()
conn = get_connection(cfg.db_path)
run_migrations(conn)
repo = FitnessRepository(conn)
print('strava:', normalize_strava(repo, user_id=1, since='').rows_normalized)
print('garmin:', normalize_garmin(repo, user_id=1, since='').rows_normalized)
"
```

This re-projects every raw row that isn't already normalized. Idempotent — a
second invocation reports `rows_normalized=0` because everything is in sync.

See [§7 Known limitations](#7-known-limitations) for the underlying watermark
quirk and the planned fix.

**Abort modes.** `fitness-backfill` exits non-zero with an actionable
`aborted_reason` in three cases:

| `final_status` | Meaning | Operator action |
|---|---|---|
| `aborted_auth` | A window returned `auth_broken`. Auth state has already transitioned and (if configured) the once-per-transition Pushover has fired. | Run `fitness-reauth-{strava,garmin}` and re-invoke `fitness-backfill`. |
| `aborted_transient` | Three consecutive windows returned `transient_failure`. | Investigate the upstream error from `fitness_sync_runs.error_message`, then re-invoke. |
| `BackfillBlocked` exception | A routine sync was already in flight. | Wait for the in-flight run (`fitness-status`), then re-invoke. |

`final_status="complete"` is the success terminal. `final_status="no_windows"`
means the watermark is already past `--end` — nothing to do.

---

## 4. Routine sync

Once authorized, the routine path is `fitness-sync`:

```bash
# Both sources, incremental from each watermark.
docker exec journal-server uv run journal fitness-sync --user-id 1

# One source, with an explicit floor.
docker exec journal-server uv run journal fitness-sync \
    --source strava --since 2026-05-01 --user-id 1
```

The CLI runs synchronously inline (no JobRunner). The long-running server
exposes the same operation via `POST /api/fitness/sync/{source}` and the
`fitness_trigger_sync` MCP tool — both queue a `fitness_sync_strava` /
`fitness_sync_garmin` job through the JobRunner. See
[`api.md`](api.md#fitness-endpoints) and
[`jobs.md`](jobs.md#fitness_sync_strava--fitness_sync_garmin) for the
HTTP / job shapes.

There is no built-in scheduler today. Production-side scheduling is operator
choice: cron on the host, an external scheduler hitting the REST endpoint, or
a webapp-driven sync button.

---

## 5. Status, health, and integrity

Three windows into the pipeline's runtime state.

### `journal fitness-status`

Per-source snapshot: `auth_status`, `auth_broken_since`, `last_success_at`,
plus the most recent ten `fitness_sync_runs` rows. CLI output mirrors the
shape of `GET /api/fitness/sync/status` exactly. Sources that have never been
configured (no auth row, no sync runs) are omitted.

### `GET /api/health` (per-user, when authenticated)

The fitness block surfaces per-source `auth_status`, `last_success_at`, and
`auth_broken_since`. The overall server status downgrades to `degraded` when
any configured source has been `broken` longer than
`FITNESS_HEALTH_BROKEN_DEGRADED_HOURS` (default 48). See
[`api.md`](api.md#get-health) for the payload shape.

The unauthenticated `GET /health` does **not** include the fitness block —
the per-user filter only applies on the authenticated route.

### `GET /api/fitness/integrity` and the `fitness_integrity_check` MCP tool

Soft-pointer orphan report — normalized rows whose `raw_ref_id` (or any id in
`raw_ref_ids_json`) doesn't resolve into the matching per-source raw table.
Empty arrays = clean. Non-empty means a normalized row is referencing a raw
row that's been deleted or never existed — a data-shape regression worth
triaging.

---

## 6. Troubleshooting

### Strava re-auth fails with `OSError: [Errno 98] Address already in use`

The `STRAVA_REDIRECT_URI` port collides with the long-running journal-server.
Use the inline-python recipe from [§2b](#2b-strava--headless-deployment-server-already-on-8400).

### Garmin re-auth prints `429: Mobile login returned 429 — IP rate limited by Garmin`

`python-garminconnect` cycles through several login transports (mobile via
`cffi`, mobile via `requests`, then a legacy fallback). The first one or two
may 429 before a third succeeds. Treat the final
`Garmin re-auth complete — token blob persisted.` line as authoritative;
the intermediate 429 lines are noise, not failure indicators.

### After backfill, `normalized < fetched`

Expected during a dense backfill (e.g. `windows=5/5 fetched=80
normalized=50`). Run the force-renormalize one-liner from
[§3](#3-historical-backfill); a second invocation will report
`rows_normalized=0` once everything is projected. See
[§7 Known limitations](#7-known-limitations).

### Backfill aborts with `BackfillBlocked` (a routine sync is in flight)

`fitness-backfill` is fail-loud on the W6 single-run guard. Wait for the
in-flight sync to finish (`fitness-status` shows `last_runs[0].finished_at`),
then re-invoke. The resume predicate makes catching up cheap.

### `auth_status="broken"` after a routine sync

The fetch service has already fired the once-per-transition Pushover (if
configured). Run `fitness-reauth-{strava,garmin}` to refresh credentials.
Re-auth flips `auth_status` back to `ok` and clears `auth_broken_since`. The
next sync (CLI, REST, or MCP) picks up from the existing watermark.

### `POST /api/fitness/sync/{source}` returns 503

The source isn't wired on this server — `STRAVA_CLIENT_ID` /
`STRAVA_CLIENT_SECRET` (or `GARMIN_USERNAME` / `GARMIN_PASSWORD`) is unset
in the container's environment. `JobRunner.submit_fitness_sync_*` fails loud
at submit time rather than queueing a row that's guaranteed to fail. Confirm
with the inspection recipe below.

### Inspecting environment in a running container

Always use an **allowlist** for env-var inspection, not a deny-list. Secrets
captured into shell history, terminal scrollback, or assistant-conversation
context have a real cost — and a deny-list misses any var the operator
forgot to exclude.

```bash
docker exec journal-server printenv | \
    grep -E '^(DB_PATH|FITNESS_|STRAVA_REDIRECT_URI|STRAVA_CLIENT_ID|GARMIN_USERNAME)='
```

Add only the var names you actually need to verify. Never grep with a
deny-list (`grep -vE 'SECRET|PASSWORD|TOKEN'`) for this purpose — one
forgotten suffix and a credential lands in the buffer.

If you suspect a credential has been captured into a shared context or log,
rotate it: regenerate the Strava client secret + Garmin password, deauthorize
the Strava app to invalidate any leaked refresh token, then re-run the
re-auth commands with the fresh credentials in `.env`.

---

## 7. Known limitations

These are documented gaps with planned follow-ups. They are not bugs — the
pipeline works correctly given the documented operator workarounds.

### W11 OAuth listener is colocated with the server

The recipe in [§2b](#2b-strava--headless-deployment-server-already-on-8400)
is a workaround, not a permanent design. The clean fix is a `--code <code>`
flag on `fitness-reauth-strava` that bypasses the listener entirely (~10
lines of CLI + a unit test). Filed as a future small follow-up. Until that
ships, the inline-python recipe is the recommended headless path.

### W7 incremental normalize watermark loses rows under dense writes

`normalize_strava` / `normalize_garmin` use a watermark of
`MAX(fetched_at) FROM fitness_*` over already-normalized rows, then read raw
rows where `fetched_at > watermark`. SQLite's default
`strftime('%Y-%m-%dT%H:%M:%SZ', 'now')` is 1-second resolution, so multiple
raw rows landing in the same wall-clock second tie on `fetched_at`. The
strict `>` filter excludes the second-and-later rows in each tied group from
the next normalize pass. After a fast backfill this leaves a chunk of raws
unprojected until a follow-up `normalize_*(since="")` projects them.

Recovery is the force-renormalize one-liner in
[§3](#3-historical-backfill). Routine syncs rarely hit this in steady state
(a single watch upload per minute doesn't tie at second resolution), so the
limitation primarily affects backfills.

The fix is one of:

1. Composite `(fetched_at, id)` watermark — break ties by row id. Cleanest
   semantically; touches `list_raw_since` and `max_normalized_fetched_at`
   plus the normalize entry points.
2. Sub-second `fetched_at` resolution
   (`strftime('%Y-%m-%dT%H:%M:%fZ', 'now')` or microsecond storage). Schema
   change; impacts every existing raw row.
3. Tail-call `normalize_*(since="")` once at the end of every backfill.
   Trivial code change; doesn't help routine syncs.

Deferred to a future work unit — none of the options are W14 scope.

### `Rowing` Strava activities collapse to `activity_type="other"`

The `coarse_strava` map covers seven canonical types (`run`, `ride`, `swim`,
`walk`, `hike`, `strength`, `other`); `Rowing` falls through to `other`.
`source_subtype` is preserved on the row, so the data isn't lost — just
bucketed coarsely for the activity-type filter. Adding `Rowing → other`
explicitly (semantic no-op, documents intent) or introducing a `row`
canonical type are both possible future enhancements.
