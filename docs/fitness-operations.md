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
2. [Initial connection](#2-initial-connection)
3. [Historical backfill](#3-historical-backfill)
4. [Routine sync](#4-routine-sync)
5. [Status, health, and integrity](#5-status-health-and-integrity)
6. [Troubleshooting](#6-troubleshooting)
7. [Known limitations](#7-known-limitations)

---

## 1. Configuration prerequisites

Strava needs server-level env vars (one OAuth app per server, shared across
users) plus a registered API application. Garmin needs no global env vars
from W6 of the fitness multi-user plan onwards — credentials are per-user in
`fitness_auth_state`, populated either via the webapp Settings panel
(`POST /api/fitness/garmin/connect`) or via the
`journal fitness-reauth-garmin --user-id N --username EMAIL` operator
fallback.

| Variable | Source | Required for |
|---|---|---|
| `STRAVA_CLIENT_ID` | Strava | Strava re-auth + sync |
| `STRAVA_CLIENT_SECRET` | Strava | Strava re-auth + sync |
| `STRAVA_REDIRECT_URI` | Strava (default `http://localhost:8400/strava/callback`) | Strava re-auth listener bind + authorize URL |
| `FITNESS_BACKFILL_START` | both (default `2026-01-01`) | `fitness-backfill` default `--start` |
| `FITNESS_TRANSIENT_FAILURE_THRESHOLD` | both (default `3`) | W6 fetch service auth-broken trip + backfill streak abort |
| `FITNESS_HEALTH_BROKEN_DEGRADED_HOURS` | both (default `48`) | `/api/health` downgrade when a source has been broken longer than this |

**Operator note (prod env hygiene).** The legacy `GARMIN_USERNAME` /
`GARMIN_PASSWORD` env vars are no longer read by any code path. They may
remain in the prod `.env` during the transition; remove them on the next
deploy that follows W6. A vestigial `STRAVA_REFRESH_TOKEN` env var (never
read by the current codebase — predates the `fitness_auth_state` table) is
also safe to drop at the same time.

See [`configuration.md`](configuration.md#optional--fitness-integration) for
the canonical reference.

For Strava-app registration steps, see
[`archive/fitness-tier-plan.md` §1](archive/fitness-tier-plan.md#1-preparation--credential-acquisition-blocking-p0).

---

## 2. Initial connection

Each user connects their own fitness sources. The webapp is the primary
path (§2a Garmin, §2b Strava) — it's what every user uses to bootstrap
and to recover from broken auth via the W11 banner's Reconnect button.
The CLI subcommands (§2c–§2e) remain as an operator fallback for
headless / scripted deployments where browser access is impractical.

Re-connect is also the recovery path when a source transitions to
`auth_status="broken"` (see [§5](#5-status-health-and-integrity)).

> **`--user-id` is required on every `fitness-*` CLI subcommand** (per
> `docs/fitness-multiuser-plan.md` W7). There is no implicit default —
> running `journal fitness-sync` without `--user-id N` exits non-zero
> with an argparse error. Every CLI example below names the user
> explicitly.

### 2a. Garmin — connecting via the webapp (primary)

From the webapp's Settings panel, click "Connect Garmin" on the Fitness
Connections card, enter Garmin account credentials, and (if Garmin asks)
the 6-digit MFA code. The webapp posts the form to three endpoints
shipped in W2 of the multi-user plan:

- `POST /api/fitness/garmin/connect` — body `{username, password}`.
  Authenticates against Garmin synchronously; returns `{connected: true,
  upstream_user_id}` on no-MFA success, or `{mfa_required: true,
  pending_session, expires_at}` when Garmin asks for a 6-digit code.
  The plaintext password is consumed once inside the request handler
  and never persisted.
- `POST /api/fitness/garmin/connect/mfa` — body
  `{pending_session, code}`. Completes the MFA-required flow. The
  `pending_session` is bound to the authenticated user's `user_id`; a
  token leaked to a different logged-in user is rejected with 403.
- `POST /api/fitness/garmin/disconnect` — deletes the calling user's
  `fitness_auth_state` row for `source='garmin'`.

A small in-memory pending-session store
(`services/fitness/garmin_pending.py`) holds the live `Garmin` client
between the connect and MFA endpoints. Entries expire after 10 minutes
and are user-bound — surviving server restarts is not in scope (the user
just repeats the connect form).

Successful logins record the upstream Garmin identifier (`displayName`)
into `fitness_auth_state.extra_state_json["upstream_user_id"]`. A
subsequent reconnect with a *different* Garmin account is refused with
409 — D8 of the multi-user plan: disconnect first if you genuinely want
to switch upstream accounts.

Garmin's auth rate-limiter keys on clientId + account email rather than
IP, so a user typo'ing their password a few times can lock themselves
out account-wide. The connect endpoint applies a per-email cool-down
(5 failures within ~15 minutes → 429 with `retry_after_seconds`) so a
user mistyping does not deepen an existing upstream lockout. The webapp
surfaces 429 responses as "try again in N minutes" rather than
auto-retrying.

See [`api.md`](./api.md#post-apifitnessgarminconnect) for the full
endpoint reference (including the `post_mfa_profile_fetch_failed`
branch that surfaces intermittent Garmin profile-endpoint flakiness as
a distinct retry signal) and
[`fitness-multiuser-plan.md`](./fitness-multiuser-plan.md) §5 W2 for
context.

### 2b. Strava — connecting via the webapp (primary)

From the webapp's Settings panel, click "Connect Strava" on the Fitness
Connections card. The browser navigates to Strava's OAuth approval
page; on approval Strava redirects to the webapp callback
(`/settings/fitness/strava/callback`), which POSTs the resulting `code`
and `state` to the exchange endpoint. The supporting endpoints shipped
in W3 of the multi-user plan:

- `GET /api/fitness/strava/authorize_url` — returns `{authorize_url,
  state, expires_at}`. The `state` is a 256-bit CSPRNG token bound to
  the authenticated user's `user_id` for 10 minutes; presenting it back
  from a different user fails 403.
- `POST /api/fitness/strava/exchange` — body `{code, state}`. Validates
  the state against the calling user, calls
  `providers.strava.exchange_code(..., return_athlete=True)` to capture
  the upstream `athlete.id` in the same SDK roundtrip, and persists
  tokens + upstream id. Idempotent up to state-token consumption — once
  a state is consumed, replaying it under the same code is a 410.
- `POST /api/fitness/strava/disconnect` — deletes the calling user's
  `fitness_auth_state` row for `source='strava'`.

A small in-memory pending-state store
(`services/fitness/strava_pending.py`) holds the `(user_id, expires_at)`
entry between issuing the authorize URL and exchanging the code.
Entries expire after 10 minutes; restart loses them. The store is
parallel to (not derived from) `garmin_pending.py` because the Strava
entry has no live SDK client to park — see the W3 journal entry for the
parallel-modules-vs-shared-helper decision.

`athlete.id` is recorded in
`fitness_auth_state.extra_state_json["upstream_user_id"]` so the same
D8 reconnect-with-different-account check applies to Strava: switching
upstream athletes silently is refused with 409.

The OAuth `redirect_uri` is sourced from `STRAVA_REDIRECT_URI`. Per D4
of the multi-user plan, the production value points at the webapp
callback route (`https://<webapp>/settings/fitness/strava/callback`).
The Strava developer app's Authorization Callback Domain must match;
W13 of the multi-user plan is the one-time operator step that flips
both in production. The default value (`http://localhost:8400/strava/callback`)
still drives the CLI listener path in §2d / §2e for dev/laptop bootstrap.

See [`api.md`](./api.md#get-apifitnessstravaauthorize_url) for the full
endpoint reference and
[`fitness-multiuser-plan.md`](./fitness-multiuser-plan.md) §5 W3 for
context.

### 2c. Garmin — CLI operator fallback

Garmin uses username/password auth (with optional MFA), not OAuth. The
webapp flow in §2a is the primary path for every user; the CLI below
remains as an operator fallback for headless / scripted bootstraps:

```bash
docker exec -it journal-server uv run journal fitness-reauth-garmin \
    --user-id 1 --username user@example.com
```

`--username` is required (no env-var fallback after W6). The password is
read from the controlling terminal via `getpass()` — never from env vars
and never persisted. The command logs into `connect.garmin.com` via
`python-garminconnect`, prompts for an MFA code on stdin if Garmin asks
for one, and persists the resulting token blob into
`fitness_auth_state.extra_state_json["tokens_blob"]`. (Unlike Strava,
Garmin's `access_token` column stays `NULL` — the live credential lives
in `extra_state` because that's the shape `garth` produces.) Subsequent
syncs read the blob, not the password.

The `garminconnect` library tries multiple transports in sequence and
may print intermediate `429: Mobile login returned 429 — IP rate limited
by Garmin` lines before a third transport succeeds. **The `Garmin
re-auth complete — token blob persisted.` line is authoritative.** The
429 lines are not failures (see [§6](#6-troubleshooting)).

### 2d. Strava — CLI operator fallback (laptop / dev box)

When the journal-server is **not** running on the same host (or is not
bound to the redirect URI's port), the CLI listener path is
straightforward:

```bash
uv run journal fitness-reauth-strava --user-id 1
```

The command prints an authorize URL and a status line, then blocks on a
one-shot HTTP listener at the host/port from `STRAVA_REDIRECT_URI`.
Open the URL in any browser, approve the prompt, Strava redirects to
the listener, and tokens persist into `fitness_auth_state` with
`auth_status="ok"`. Useful for first-time dev/laptop setup or for
emergency re-auth from a developer machine.

### 2e. Strava — CLI operator fallback (headless deployment, server on `:8400`)

When the default `STRAVA_REDIRECT_URI` reuses port `8400`, which the
long-running journal-server is already bound to, the §2d listener can't
bind. Two workarounds; the inline-python recipe below is the
recommended path.

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

---

## 3. Historical backfill

After both sources are authorized, populate history with the backfill
orchestrator. Two front doors call the same `services/fitness/backfill.py`
orchestrator under the hood:

- **In-app (W5, recommended for end users)** — `POST /api/fitness/backfill/
  {source}` with body `{start, end?}`, called by the webapp's Backfill
  button. Returns `{job_id}` immediately; progress shows up in the jobs
  view and in `journal fitness-status` as windows complete.
- **CLI (operator fallback)** — `journal fitness-backfill` for headless
  / multi-source / repair runs.

```bash
docker exec journal-server uv run journal fitness-backfill \
    --source both \
    --start 2026-01-01 \
    --user-id 1
```

**Idempotency (spans sync + backfill).** Only one *fetch* job per
`(user_id, source)` runs at a time, and "fetch" covers both
`fitness_sync_{source}` and `fitness_backfill_{source}` worker classes.
Whichever was queued first wins; the colliding submit (whether a
scheduled sync running into a click-triggered backfill, or vice versa)
returns the in-flight job's id with `already_running: true` instead of
queueing a duplicate. This prevents double-clicks and stops a backfill
from interleaving writes with a routine sync on the same date window.

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

### `journal fitness-audit`

Per-user data isolation audit. Walks every fitness table
(`fitness_auth_state`, `fitness_sync_runs`, `fitness_activities`,
`fitness_daily`, `fitness_raw_strava`, `fitness_raw_garmin`) and reports:

- Total row count per table.
- Per-user row count breakdown (one line per `user_id`, joined with the
  user's email so the operator can read it without a DB lookup).
- Violations: any row with `user_id IS NULL` or `user_id` pointing at a
  deleted user (an "orphan" — possible if FK enforcement was off when the
  user was deleted, or if a row was inserted via raw SQL bypassing
  validation).

Exits 0 when clean, exit code 1 when any violation is found. Used as the
pre-flight check before the multi-user rollout (`fitness-multiuser-plan.md`
W1) and as the post-rollout verification gate (W14): the row-count
snapshot before W2/W3 ship is the regression target after user 2 starts
populating their own rows.

```bash
docker exec journal-server uv run journal fitness-audit
```

The command takes no arguments; it scans every user's data because the
whole point is to catch cross-user leakage. Run it against a copy of the
prod DB before shipping any of the multi-user work units.

---

## 6. Troubleshooting

### Strava re-auth fails with `OSError: [Errno 98] Address already in use`

The `STRAVA_REDIRECT_URI` port collides with the long-running journal-server.
Use the inline-python recipe from [§2e](#2e-strava--cli-operator-fallback-headless-deployment-server-on-8400).

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

Only Strava can 503 here — `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET` is
unset in the container's environment, so
`JobRunner.submit_fitness_sync_strava` fails loud at submit time rather than
queueing a row that's guaranteed to fail. Garmin is always wired post-W6
(per-user creds, no global env vars), so `submit_fitness_sync_garmin` never
503s for a config reason — a user without a `fitness_auth_state` row just
produces a clean `auth_broken` sync. Confirm with the inspection recipe
below.

### Inspecting environment in a running container

Always use an **allowlist** for env-var inspection, not a deny-list. Secrets
captured into shell history, terminal scrollback, or assistant-conversation
context have a real cost — and a deny-list misses any var the operator
forgot to exclude.

```bash
docker exec journal-server printenv | \
    grep -E '^(DB_PATH|FITNESS_|STRAVA_REDIRECT_URI|STRAVA_CLIENT_ID)='
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
