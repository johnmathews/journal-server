# Fitness Operations

**Status:** active. **Last updated:** 2026-07-14 (Strava mothballed behind `STRAVA_ENABLED=false`; previous updates
2026-07-13 — Strava suspension note; 2026-06-21 — Garmin Cloudflare-block handling: connect endpoint reclassifies IP/bot-challenge blocks as `upstream_rate_limited` and trips a global pre-flight cooldown; split-IP mint/import recovery in §2c-bis; see `journal/260619-garmin-cloudflare-recovery.md` and `journal/260621-garmin-upstream-cooldown.md`).

> **Strava integration mothballed via `STRAVA_ENABLED=false` (2026-07-14).**
> Strava paywalled Standard-tier API access behind an active Strava
> subscription effective 2026-06-30
> ([announcement](https://communityhub.strava.com/insider-journal-9/an-update-to-our-developer-program-13428)).
> The integration is now gated by the `STRAVA_ENABLED` env flag (default
> `false`): the three `/api/fitness/strava/*` OAuth routes and the Strava
> sync/backfill routes return `404`, the MCP trigger tools and the CLI refuse
> `source=strava`, and the daily scheduler processes Garmin only. Historical
> Strava rows are kept and still served, and `GET /api/fitness/sync/status`
> keeps both source keys. The Strava sections below are kept for reference and
> only apply when the flag is on — see [Reviving Strava](#reviving-strava) and
> roadmap D8. Journal entry: `journal/260714-strava-mothball.md`.

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

### Reviving Strava

1. Buy a Strava subscription for the account that owns the API app
   (Standard-tier API access requires it since 2026-06-30).
2. Set `STRAVA_ENABLED=true` plus `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET`
   (and `STRAVA_REDIRECT_URI` for prod) in the server environment.
3. Restart the server — the flag is read at startup, not at runtime.
4. Reconnect via the webapp: Settings → Fitness → Connect Strava (§2b). The
   webapp re-shows all Strava UI automatically once `features.strava_enabled`
   flips true in `GET /api/settings`.

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

Strava needs `STRAVA_ENABLED=true` (mothballed off by default — see the banner
above) plus server-level OAuth env vars (one OAuth app per server, shared
across users) and a registered API application. Garmin needs no global env vars
from W6 of the fitness multi-user plan onwards — credentials are per-user in
`fitness_auth_state`, populated either via the webapp Settings panel
(`POST /api/fitness/garmin/connect`) or via the
`journal fitness-reauth-garmin --user-id N --username EMAIL` operator
fallback.

| Variable | Source | Required for |
|---|---|---|
| `STRAVA_ENABLED` | Strava (default `false` — mothballed) | Any Strava surface at all (routes, CLI, scheduler, MCP triggers) |
| `STRAVA_CLIENT_ID` | Strava | Strava re-auth + sync |
| `STRAVA_CLIENT_SECRET` | Strava | Strava re-auth + sync |
| `STRAVA_REDIRECT_URI` | Strava (default `http://localhost:8400/strava/callback`) | Strava re-auth listener bind + authorize URL |
| `FITNESS_BACKFILL_START` | both (default `2026-01-01`) | `fitness-backfill` default `--start` |
| `FITNESS_TRANSIENT_FAILURE_THRESHOLD` | both (default `3`) | W6 fetch service auth-broken trip + backfill streak abort |
| `FITNESS_HEALTH_BROKEN_DEGRADED_HOURS` | both (default `48`) | `/api/health` downgrade when a source has been broken longer than this |

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
> `docs/archive/fitness-multiuser-plan.md` W7). There is no implicit default —
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
out account-wide. The connect endpoint applies two complementary 429
guards, both checked **before** any upstream call:

- a **per-email** cool-down (`reason: "local_cooldown"`, 5 failures
  within ~15 minutes → `retry_after_seconds`) so a user mistyping does
  not deepen an account-wide upstream lockout; and
- a **global** cool-down (`reason: "upstream_rate_limited"`, default 5
  minutes) tripped the moment any attempt is blocked by Garmin's
  Cloudflare / IP rate-limiter. That block lives on the server's shared
  egress IP, so the next connect for *any* account is refused pre-flight
  until it ages out — stopping the UI from re-arming a block already in
  place (the per-email guard can't, since a different email sails
  straight through). Recover via
  [§2c-bis](#2c-bis-garmin--split-ip-recovery-when-cloudflare-blocks-the-server).

The webapp surfaces both 429s as "stop retrying and wait N minutes"
rather than auto-retrying.

See [`api.md`](./api.md#post-apifitnessgarminconnect) for the full
endpoint reference (including the `post_mfa_profile_fetch_failed`
branch that surfaces intermittent Garmin profile-endpoint flakiness as
a distinct retry signal) and
[`archive/fitness-multiuser-plan.md`](./archive/fitness-multiuser-plan.md) §5 W2 for
context.

### 2b. Strava — connecting via the webapp (primary)

> Mothballed: with `STRAVA_ENABLED=false` (the default) all three endpoints
> below return `404` and the webapp hides the Strava card entirely.

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
[`archive/fitness-multiuser-plan.md`](./archive/fitness-multiuser-plan.md) §5 W3 for
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

### 2c-bis. Garmin — split-IP recovery when Cloudflare blocks the server

`fitness-reauth-garmin` (§2c) and the webapp connect flow (§2a) both run
the username/password login **from the server's egress IP**. When Garmin's
Cloudflare bot defenses flag that IP — typically after a burst of failed
login attempts — every fresh login from the server fails with a 429 / bot
challenge **even though the credentials are correct**. The symptom in logs
is a run of `mobile+cffi returned 429` / `unexpected title 'GARMIN
Authentication Application' … Cloudflare rate limiting` lines, and the
webapp surfaces `reason: "upstream_rate_limited"` (a 429), no longer the
misleading `invalid_credentials` it reported before this was fixed.

The fix is to do the network login somewhere **unflagged** and ship only
the resulting token blob to the server. Because a `garth` OAuth1 token is
valid ~1 year and the daily sync boots from the stored blob (no fresh SSO
login), a single mint+import keeps syncing working for months.

```bash
# 1. MINT — on a laptop / unflagged network (e.g. phone hotspot, NOT the
#    server's network). Prompts for the password via getpass; never writes
#    the DB. Prints a JSON envelope to stdout (or use --output FILE).
uv run journal fitness-garmin-mint-token --username user@example.com \
    --output garmin-token.json

# 2. IMPORT — on the server. No network login; just writes the blob into
#    fitness_auth_state and sets auth_status='ok'.
docker exec -i journal-server uv run journal fitness-garmin-import-token \
    --user-id 1 --input - < garmin-token.json

# …or pipe the two directly if the laptop can reach the server:
uv run journal fitness-garmin-mint-token --username user@example.com \
    | docker exec -i journal-server uv run journal \
        fitness-garmin-import-token --user-id 1
```

Notes:
- **Stop retrying first.** Every failed login from the flagged IP re-arms
  the Cloudflare block. If even the laptop mint 429s, the account/IP is
  still hot — wait (24h+) and mint from a different network.
- **The connect endpoint now enforces this for you.** The first
  `upstream_rate_limited` trips a server-wide cooldown (default 5 min), and
  every connect attempt after it — any account — is refused pre-flight with
  the same `reason` until it ages out, so the UI can't keep hammering the
  block. This only gates the interactive **connect/re-auth** path; the
  split-IP import above writes the blob directly and does no network login,
  so it is never blocked by the cooldown.
- The mint command needs the repo + a loadable `.env` (to start the CLI)
  but **no DB access and no prod network**, so it runs anywhere the package
  is installed.
- Import validates the blob loads into the SDK before writing, and warns on
  stderr if it belongs to a different Garmin account than the one already
  stored (the D8 guard).
- **Verify with a live sync, not just the import.** The offline SDK load only
  proves the blob is well-formed — it does not prove the token works against
  Garmin's API. Run `fitness-sync --source garmin --user-id N` right after
  importing and confirm `status=success fetched>0`; if it comes back
  `auth_broken`/429 the token was minted on a still-flagged IP and must be
  re-minted from a cleaner network.
- If the mint hit 429s mid-login, the post-login profile fetch can fail and
  `upstream_user_id` falls back to the **login email** instead of the Garmin
  display name. The token is usually still valid (verify per above); the only
  side effect is that the import's D8 check may warn about an account change.
- Run the CLI **inside the prod container** as
  `docker exec [-i] journal-server uv run journal …` — the bare `journal`
  console script is not on the container's `$PATH`.

### 2d. Strava — CLI operator fallback (laptop / dev box)

> Mothballed: with `STRAVA_ENABLED=false` (the default),
> `journal fitness-reauth-strava` prints
> `Error: Strava integration is disabled (STRAVA_ENABLED=false).` and exits 1.
> Same for the §2e variants.

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
bind. Three approaches in descending preference.

**Primary — `--code <code>`.** Pass the authorization code straight to
the CLI; no listener is started, no port juggling, no inline-python
boilerplate. Build the authorize URL once (see Fallback A below for the
shape), paste it into a browser, approve, and copy the `code` param out
of the redirect URL bar (the browser tab will fail to load — that's
fine):

```bash
docker exec journal-server uv run journal fitness-reauth-strava \
    --user-id 1 --code "<PASTE_CODE_HERE>"
```

Exits 0 with `Strava re-auth complete — tokens persisted.` on success; on
an invalid or expired code the upstream error surfaces on stderr and the
command exits non-zero (no DB row is written).

**Fallback A — inline-python recipe.** Useful if you want full control
over the exchange-and-persist path (e.g. preserving a hand-curated
`extra_state` blob across the re-auth). Same outcome as `--code`, more
typing. Build the authorize URL by hand, paste it into a browser, copy
the `code` param out of the redirect URL bar (the browser tab will fail
to load — that's fine), then exchange the code in a one-off Python
invocation against the live container:

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

**Fallback B — stop-server + one-off container with `--service-ports`.**
If you want the native OAuth roundtrip (e.g. to verify the listener
works after a config change), free port `8400` and run the CLI in a
fresh container that publishes it:

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
recipe on the VM. Two SSH sessions, fragile. The `--code` primary path
above makes this go away — preferred for headless deployments.

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
`garmin`, or `both` — but with `STRAVA_ENABLED=false` (the default) both
`strava` and `both` are rejected with exit 1 (use `--source garmin`), and
`POST /api/fitness/backfill/strava` returns `404`.

**Resume.** Re-running is safe and cheap. Per-source watermarks
(`MAX(local_date)` over normalized rows, with Garmin using `min(activities,
daily)`) skip windows already fully ingested. Mid-window crashes leave clean
state because the raw layer is `INSERT OR IGNORE` on payload SHA and
normalized rows are upserts.

**After a fresh backfill, run a force-renormalize once on any DB whose
backfill predates the W3 watermark fix (server `fitness-multiuser-final-mile`,
2026-06-04).** Pre-fix, the per-window normalize step undercounted because
multiple raw rows could land within the same SQLite-1-second `fetched_at`
and the scalar watermark's strict `>` filter dropped the second-and-later
rows in each tied group. Post-fix the composite `(fetched_at, id)`
watermark makes this safe. Running the one-liner on a post-fix DB is a
clean no-op. The recovery is idempotent either way:

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

See [§7 Known limitations](#7-known-limitations) for the watermark fix
status and what the original quirk looked like.

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

> Mothballed: with `STRAVA_ENABLED=false` (the default), `--source strava` and
> `--source both` (the CLI default) are rejected with exit 1 — pass
> `--source garmin`. `POST /api/fitness/sync/strava` returns `404` and the MCP
> `fitness_trigger_sync` tool rejects `source="strava"`.

The CLI runs synchronously inline (no JobRunner). The long-running server
exposes the same operation via `POST /api/fitness/sync/{source}` and the
`fitness_trigger_sync` MCP tool — both queue a `fitness_sync_strava` /
`fitness_sync_garmin` job through the JobRunner. See
[`api.md`](api.md#fitness-endpoints) and
[`jobs.md`](jobs.md#fitness_sync_strava--fitness_sync_garmin) for the
HTTP / job shapes.

### Daily auto-sync

The server runs a `FitnessSyncScheduler` daemon thread
(`services/fitness/scheduler.py`) that enqueues an incremental sync for every
user with working credentials once per day.

**When it fires.** The scheduler wakes at **17:00 server-process local time**
(`datetime.now()` inside the container). The production `media` VM's container
inherits the host timezone (no `TZ` is set), which is **CEST (UTC+2)** as of
2026-06-14 — so it fires at **17:00 CEST = 5pm local European time (15:00
UTC)**, *not* 17:00 UTC. If the host/container timezone ever changes (or `TZ`
is set explicitly), the fire time follows it. If the server is down at 17:00, that day's run
is skipped; the next day's incremental sync pulls from each source's
existing watermark, so at most one day of activity data is delayed.

**Which users and sources.** With `STRAVA_ENABLED=false` (the default) the
scheduler is constructed Garmin-only and never lists or submits Strava work.
When the flag is on, for each source (`strava`, `garmin`) the
scheduler calls `FitnessRepository.list_users_with_active_auth(source)` and
submits an incremental sync for every returned user ID. That query returns
users whose `fitness_auth_state` row has `auth_status != 'broken'` and a
present credential (Strava: non-empty `access_token`; Garmin: non-empty
`tokens_blob` in `extra_state_json`). A user with only Strava connected gets
only a Strava sync; both connected gets both; neither connected (or
`auth_status='broken'` for that source) is skipped entirely.

**Quiet notifications.** Scheduled syncs are submitted with
`quiet_success=True`. A run that fetches zero new rows produces no Pushover
notification. Runs that import new data, and auth failures, still notify.
Manual syncs triggered via the REST API or MCP tools are unaffected — they
always notify on success.

**Collision safety.** There is no submit-time dedup in the scheduler. If a
manual sync for the same `(user, source)` pair is already running when the
daily fire occurs, the fetch service's in-flight guard (`find_running_sync_run`)
makes the queued job a clean no-op: status transitions `running` → succeeded,
no data is duplicated.

**Error isolation.** A failure listing one source's users, or submitting for
one user, is logged and skipped without aborting the rest of the run.

**Lifecycle.** The scheduler is started in `mcp_server/bootstrap.py` (right
after the `HealthPoller`) and stopped in the `_shutdown_job_runner` atexit
hook. Controlled by the `FITNESS_SYNC_ENABLED` env var (default `true`); set
it to a falsey value (`0`, `false`, `no`, `off`) to disable entirely without
restarting. See [`configuration.md`](configuration.md#optional--fitness-integration)
and the design rationale in
[`docs/superpowers/specs/2026-06-14-daily-fitness-auto-sync-design.md`](superpowers/specs/2026-06-14-daily-fitness-auto-sync-design.md).

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
pre-flight check before the multi-user rollout (`archive/fitness-multiuser-plan.md`
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
Use the `--code <code>` primary path in [§2e](#2e-strava--cli-operator-fallback-headless-deployment-server-on-8400)
— no listener bind, no port juggling.

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

If a Garmin re-auth itself keeps failing with 429 / `upstream_rate_limited`
(webapp) or a wall of `… returned 429 … IP rate limited by Garmin` lines
(CLI), the server's IP is Cloudflare-blocked — the credentials are fine.
Stop retrying (each attempt re-arms the block) and use the split-IP
mint/import recovery in [§2c-bis](#2c-bis-garmin--split-ip-recovery-when-cloudflare-blocks-the-server).

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

### W11 OAuth listener is colocated with the server — **fixed**

**Status:** fixed 2026-06-04 (server `fitness-multiuser-final-mile` W2).
`fitness-reauth-strava` now accepts `--code <code>` and exchanges the
code directly, skipping the in-process HTTP listener entirely. See
[§2e](#2e-strava--cli-operator-fallback-headless-deployment-server-on-8400)
for the primary path; the previous inline-python recipe stays in §2e as
Fallback A for operators who want full control of the exchange call.

### W7 incremental normalize watermark loses rows under dense writes — **fixed**

**Status:** fixed 2026-06-04 (server `fitness-multiuser-final-mile` W3).
The watermark used by `normalize_strava` / `normalize_garmin` is now the
composite `(fetched_at, id)` tuple returned by
`max_normalized_fetched_at`, and `list_raw_since` filters with row-value
comparison (`(fetched_at, id) > (?, ?)`). The AUTOINCREMENT raw-row id
breaks ties at SQLite's default 1-second `fetched_at` resolution, so
raw rows landing in the same wall-clock second can no longer drop on a
subsequent normalize pass. Regression test:
`tests/test_db/test_fitness_repository.py::test_watermark_tied_fetched_at_no_row_loss`.

The force-renormalize one-liner in [§3](#3-historical-backfill) remains
useful for any DB state that accumulated unprojected rows before the
fix; running it once is a safe no-op on a clean DB. Routine syncs in
steady state were unaffected (a single watch upload per minute doesn't
tie at second resolution).

### ~~`Rowing` Strava activities collapse to `activity_type="other"`~~ — resolved 2026-06-04

`row` is now the eighth canonical type (`run`, `ride`, `swim`, `walk`,
`hike`, `row`, `strength`, `other`) — added as W5 of the fitness
multi-user final-mile plan. `_activity_type_map.py` maps Strava
`Rowing` to `row`, and migration `0029_fitness_activity_type_add_row.sql`
backfills pre-existing rows that had collapsed to `other` (identified
via their preserved `source_subtype`) when it runs on deploy.
