## Fitness Multi-User Plan

**Status:** in progress. **Last updated:** 2026-05-11. **Supersedes:** none.

**Progress snapshot (2026-05-11):**
- **Shipped (server):** W1 (audit, `4dd90c4`), W2 (Garmin connect/MFA, `59f7714`),
  W3 (Strava OAuth exchange, `5dca0cc`), W4 (per-user integrity, `fed3775`),
  W5 (backfill workers + endpoint, `be6ab80`), W6 (drop Garmin env vars,
  `6064145`), W7 (CLI `--user-id` required, `14ddb6b`), W11 (worker-level
  auth_status flip test, `18d66b0`).
- **Shipped (webapp):** W8 (API client, `4de33c4`), W9 (settings panel, `6df8d7e`),
  W10 (Strava callback view, `d53f3a7`), W11 (banner Reconnect button, `c5968c3`).
- **Remaining:** W12 (docs sweep), W13 (Strava callback URL — operator step),
  W14 (end-to-end verification with user 2).

This plan moves the fitness pipeline from its current single-user posture (operator-managed
credentials in env vars, CLI re-auth with `--user-id 1` defaults, all data attributed to
`user_id=1`) to a per-user posture where each user connects their own Garmin and Strava accounts
through the webapp UI. Concrete schema and data-flow are unchanged from
[`fitness-schema.md`](./fitness-schema.md) and [`fitness-pipeline.md`](./fitness-pipeline.md);
this plan is an additive layer above the existing pipeline.

For decisions and rationale on the underlying fitness pipeline, see
[`fitness-integration-plan.md`](./fitness-integration-plan.md). This plan amends one resolved
question in that doc (single-user posture → multi-user) and otherwise leaves it intact.

## Contents

1. [Goal & non-goals](#1-goal--non-goals)
2. [Current state (verified)](#2-current-state-verified)
3. [Decisions & tradeoffs](#3-decisions--tradeoffs)
4. [Code surface](#4-code-surface)
5. [Work units](#5-work-units)
6. [Migration & data integrity](#6-migration--data-integrity)
7. [Out of scope](#7-out-of-scope)
8. [Kill criteria](#8-kill-criteria)
9. [References](#9-references)

---

## 1. Goal & non-goals

**Goal.** Each user of journal-server can connect their own Garmin Connect and/or Strava
accounts through the webapp UI, and their fitness data is stored, queried, and surfaced
strictly per-user. The two prod users (`user_id=1` admin, `user_id=2` demo) become an
ordinary case rather than an implicit assumption.

**Non-goals (this plan).**
- Multiple Strava developer apps. One Strava OAuth app per server is shared across all
  users (operator-global `STRAVA_CLIENT_ID`/`STRAVA_CLIENT_SECRET`).
- Schema changes. The existing schema (migration 0023 onward) already carries `user_id`
  on `fitness_auth_state`, `fitness_sync_runs`, `fitness_activities`, `fitness_daily`,
  and `fitness_raw_*`. No new migrations.
- Encryption-at-rest for tokens. Tokens stay in plaintext columns / `extra_state_json`
  same as today. Garmin password is never persisted (token-only after first login).
- Cross-user data sharing or admin-views-other-users UI.
- Backfill scheduling. Backfill remains operator-triggered (now via UI button or CLI).

---

## 2. Current state (verified)

Read 2026-05-10 — facts grounded in code and prod DB, not assumptions.

**DB layer is already multi-user-ready.** Every fitness table has `user_id` with proper
indexes and FK to `users(id) ON DELETE CASCADE`. Every `FitnessRepository` method takes an
explicit `user_id` parameter; every query filters `WHERE user_id = ?`.

**REST/MCP layer is already per-user-aware.** All four `/api/fitness/*` GET endpoints
(`list_activities`, `list_daily`, `sync_status`, `integrity`) extract `user.user_id` from
`get_authenticated_user(request)`. The `integrity` endpoint authenticates but does not yet
filter by user — see the second smell below. All MCP tools call
`_user_id(ctx)`. Job submission via `POST /api/fitness/sync/{source}` and the
`fitness_trigger_sync` MCP tool both attach `user_id` to the job params; the workers in
`services/jobs/workers/fitness_sync_{strava,garmin}.py` read it back via `params["user_id"]`.

**The single-user assumption lives in two places:**

1. **CLI defaults.** `cli/__init__.py` lines 618, 634, 663, 696, 711 default `--user-id 1`
   on every fitness subcommand. `_DEFAULT_USER_ID = 1` in `cli/fitness.py:65`.

2. **Config / env vars.** `config.py` lines 412–430 read `GARMIN_USERNAME`,
   `GARMIN_PASSWORD`, `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_REDIRECT_URI`
   as global env vars. Garmin username/password are inherently per-user; their global
   posture is the bug.

**Two smaller smells.**

- `GET /api/fitness/integrity` and the `fitness_integrity_check` MCP tool are scope-less
  (no `user_id` filter). Comment at `api/fitness.py:252-257` flags this as deliberate
  pending multi-user.
- Prod env contains `STRAVA_REFRESH_TOKEN` — not read by any code; vestigial from a
  pre-`fitness_auth_state` shape. Safe to remove.

**Prod state (verified 2026-05-10).** Two users (`mthwsjc@gmail.com` admin / `mthwsjc+demo@gmail.com`
non-admin). Only user 1 has fitness data: 80 Strava activities + 80 Garmin activities + 129
days of Garmin daily metrics. Both `fitness_auth_state` rows are user_id=1, status=ok. User 2
has no fitness rows — there is no data to re-attribute.

**No new schema needed.** Both providers (`providers/strava.py`, `providers/garmin.py`)
already accept credentials via constructor and persist tokens through an injected callback;
they are user-agnostic.

---

## 3. Decisions & tradeoffs

These are the load-bearing choices. Each names what was picked, what was rejected, and why.

### D1. Strava is operator-global, Garmin is per-user.
**Picked.** `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET` / `STRAVA_REDIRECT_URI` stay as
server-level env vars (one Strava developer app per journal-server deployment). Each user
OAuths against that one app and gets their own access/refresh tokens stored in their own
`fitness_auth_state` row. `GARMIN_USERNAME` / `GARMIN_PASSWORD` are removed from the
global config — Garmin credentials are inherently per-user.

**Rejected.** Per-user Strava apps. Forces every user to register a developer app at
developers.strava.com — friction without benefit at homelab scale. Strava's app-level rate
limits (200/15min, 2000/day) are generous enough for two users.

**Why.** Strava's OAuth model treats apps as third-party integrations; this is the standard
multi-tenant pattern. Garmin's auth is direct user/password, so per-user is mandatory.

### D2. Garmin two-step login via `return_on_mfa` / `resume_login`.
**Picked.** `python-garminconnect` (≥0.3.x) exposes a non-blocking two-step API that we use
directly:

- `Garmin(username, password, return_on_mfa=True).login()` runs synchronously and returns
  either a successful login (no MFA needed) or the tuple `("needs_mfa", state)` where
  `state` is an opaque dict the library uses to resume.
- `client.resume_login(state, mfa_code)` completes the login.

We expose this as two endpoints:

- `POST /api/fitness/garmin/connect` takes `{username, password}`, runs `Garmin(...)
  .login()` synchronously (FastAPI executes blocking handlers in its threadpool — no manual
  thread management). On success: capture the token blob via `client.client.dumps()`, fetch
  the upstream profile (D8), persist via `repo.upsert_auth_state(...)`, return
  `{connected: true}`. On `("needs_mfa", state)`: store an entry in the in-memory pending
  map keyed by a 256-bit CSPRNG token, return `{mfa_required: true, pending_session,
  expires_at}`.
- `POST /api/fitness/garmin/connect/mfa` takes `{pending_session, code}`, looks up the
  entry, **rejects if `pending.user_id != current_user.user_id`**, calls
  `client.resume_login(state, code)`, persists the token blob + upstream profile, returns
  `{connected: true}`.

**Rejected.**

- The "park a thread on a Queue/Event" pattern. `prompt_mfa` is a synchronous callback;
  emulating its blocking semantics across threads adds thread-leak risk, asyncio-bridging
  awkwardness, and shared mutable state that `return_on_mfa` removes entirely. The earlier
  draft of this plan specified that pattern under the (mistaken) assumption that
  `prompt_mfa` was the only entry point.
- Out-of-band MFA delivery (Pushover/email). Adds a dependency for marginal UX gain.

**Why.** The library directly supports the two-step flow we need. Each endpoint is a short
synchronous call; the only state we hold between calls is the resumable `state` dict plus a
user-bound TTL entry. No threads to leak, no Event/Queue to coordinate, no asyncio shim.
Pending-map lives in process memory with a 10-minute TTL; expiry just means the user
repeats the form. Single-process server, no inter-process coordination needed.

**Security: pending-session token must be user-bound.** The token returned to the client is
the only thing protecting an in-flight MFA. Bind it to the originating `user_id` at issue
and reject any consume from a different authenticated user — this prevents a leaked token
(logs, screenshot, browser-history hand-off) from being used by another logged-in user to
bind a Garmin account they don't own. Token entropy: 256 bits CSPRNG, base64url-encoded.

**Implementation note on Garmin rate limits.** Garmin's auth rate-limiter keys on
`clientId + account email` rather than IP (per `python-garminconnect` issue #344), so a
user who mistypes their password twice in quick succession can lock themselves into a
account-wide 429 for a while. The connect endpoint must apply a per-email server-side
cool-down (track recent failures by email, refuse retries inside a short window) and
surface 429 responses from Garmin as a clear "too many attempts, try again in N minutes"
error rather than letting users retry-loop into deeper lockouts. Two specific failure modes
worth surfacing distinctly per `python-garminconnect` issues #312/#337: "wrong MFA code"
(retryable) and "MFA accepted but post-login profile fetch failed" (intermittent, not the
user's fault — surface as "Garmin is flaky right now, please retry").

### D3. Garmin password is discarded after first login; only the token blob persists.
**Picked.** After the two-step login completes, capture the token blob via
`client.client.dumps()` and write it into `fitness_auth_state.extra_state_json["tokens_blob"]`.
The plaintext password is never written to disk and is dropped from the connect endpoint's
request handler as soon as the login result is known.

**Rejected.** Encrypt + store the password so we can re-login non-interactively when the
token expires. Cheaper for the user (no re-auth event) but exposes credentials if both DB
and the encryption key leak. Token-only matches the stated security posture.

**Token lifetime in 2026 — the "365 days" figure is dated; expect earlier re-auth.** The
blob carries an OAuth1 token (historically valid ~365 days) plus a short-lived OAuth2
access token (auto-refreshed before each request as long as the OAuth1 token is good). Two
near-term events shift the practical re-auth cadence:

- `garth` (the predecessor library) was deprecated 2026-03-28 because "Garmin changed their
  auth flow." `python-garminconnect` 0.3.x reimplements auth and consumes the same blob
  format, but the auth surface itself is moving.
- Garmin's own developer docs flag **OAuth1 retirement on 2026-12-31** with a migration to
  OAuth2 PKCE. Every connected user will need to reconnect before that date — earlier than
  the nominal 365-day TTL would suggest, and the migration may force a library bump that
  invalidates existing blobs entirely.

**Implication.** Plan for users to repeat the connect flow at least once before
2026-12-31, possibly sooner if Garmin SSO breaks `python-garminconnect` mid-year. The
webapp's `FitnessAuthBanner.vue` is the user-visible escape hatch.

**Pre-flight check before W11:** verify that sync workers actually write
`auth_status='broken'` (not just `'error'`) when Garmin returns 401/expired-token. The
banner is only useful to users if it has the right data to bind to. If they don't, the
plan needs a small additional unit to wire that up — the banner-redesign in W11 is
worthless if the underlying flag never flips.

### D4. Strava callback is a webapp route; the API endpoint exchanges the code.
**Picked.** Strava redirects to `<webapp>/settings/fitness/strava/callback`. A Vue route at
that path reads `code` and `state` from the query string and POSTs them to
`/api/fitness/strava/exchange`, which validates the state token (CSRF), calls
`exchange_code`, and persists the resulting tokens for the authenticated user.

**State token must be user-bound.** When `GET /api/fitness/strava/authorize_url` issues the
state token, store `(user_id, state, expires_at)` in an in-memory map (10-min TTL).
`POST /api/fitness/strava/exchange` validates the state AND **rejects if `pending.user_id
!= current_user.user_id`**. Without this binding the flow is replay-vulnerable: an attacker
could craft an authorize URL embedding their own pre-issued state and trick a logged-in
journal user into attaching the attacker's Strava account to the victim's journal account.
Token entropy: 256 bits CSPRNG, base64url-encoded.

**Rejected.** Backend-route callback (`<server>/api/fitness/strava/callback`) — workable
but the SPA loses control of error UX (denied authorization, expired state, exchange
failure, network error) and we'd need a server-side redirect back to the webapp after
exchange. The webapp-route pattern keeps the SPA in charge of its own error states and
avoids the backend-to-frontend redirect dance.

**Note on cookies (clarifying a misconception in earlier drafts):** an earlier rationale
claimed "the redirect comes from Strava without the user's session cookie if the SPA and
API serve different origins." That is not generally true — `SameSite=Lax` cookies *are*
sent on top-level GETs, including OAuth redirects. The decision still stands on the SPA
error-UX argument above; cookie behavior is not the load-bearing reason.

**Implication.** The Strava developer app needs its Authorization Callback Domain updated
to the webapp's prod hostname. `STRAVA_REDIRECT_URI` env var becomes a build-time webapp
URL (not a backend listener bind anymore). The CLI listener path (port 8400) still works
for dev/laptop bootstrap, but is no longer the recommended in-app path.

### D5. CLI `--user-id` becomes required.
**Picked.** Drop the `default=1` on every `fitness-*` subcommand; argparse `required=True`.
The CLI will error if `--user-id` is omitted. Drop `_DEFAULT_USER_ID = 1` in
`cli/fitness.py`.

**Rejected.** Keep the default with a warning — backwards-compatible, but invites
operational mistakes (operator runs `fitness-sync` from cron, accidentally targets
user 1 because that's the default).

**Why.** The CLI is now a fallback path. The webapp is the primary surface. Forcing the
operator to name the user is the right safety affordance. Any cron / script that depended
on the implicit default needs a one-line edit to add `--user-id 1`.

### D6. Backfill stays explicit (button, not auto).
**Picked.** After a user successfully connects a source, the UI shows a "Backfill" button
with a date picker (default `FITNESS_BACKFILL_START`, currently 2026-01-01). Clicking it
enqueues a `fitness_backfill_{strava,garmin}` job for that user via a new
`POST /api/fitness/backfill/{source}` endpoint.

**Rejected.** Auto-trigger backfill on first connect — magical but surprising; user 2 might
not want a 4-month backfill the moment they click connect.

**Implication.** This requires a new job worker (`fitness_backfill_strava` /
`fitness_backfill_garmin`) wrapping the existing `services/fitness/backfill.py` orchestrator.
Today the orchestrator is CLI-only. Modest lift — mirrors the existing `fitness_sync_*`
worker shape.

### D7. Integrity check becomes per-user.
**Picked.** Add a `user_id` filter to `GET /api/fitness/integrity` and the
`fitness_integrity_check` MCP tool. Return only orphans owned by the calling user.

**Rejected.** Make it admin-only (returns global orphans) — fewer changes, but couples a
reporting endpoint to admin role and leaks the existence of other users' raw rows in the
report.

### D8. Capture upstream account identity at connect; refuse silent reconnect with a different account.
**Picked.** When a connect succeeds, record the upstream provider's stable account
identifier in `fitness_auth_state.extra_state_json`:

- **Strava:** `athlete.id` from the token-exchange response payload.
- **Garmin:** `display_name` (or profile id) from a `client.get_user_profile()` call
  immediately after `login()` / `resume_login()`.

On reconnect, compare the incoming upstream id against the stored one. If they differ,
**refuse the connect** and return an error directing the user to either disconnect first
(which clears their fitness data for that source) or use the original upstream account.

**Rejected.** Silently overwrite tokens. The current schema would happily upsert new
tokens onto the existing row, but historical `fitness_activities` rows would then mix two
real upstream users' data under one journal `user_id` — a data-integrity bug we cannot
detect after the fact because the upstream identity isn't recorded.

**Why.** Without an upstream-id field, "reconnect with a different account" is silent and
corrupting. The schema already has `extra_state_json`, so this is purely additive — no
migration. Disconnect already deletes the auth row; if a user genuinely wants to switch
upstream accounts, the disconnect flow becomes a "this also clears your fitness data from
this source" confirmation, then they connect fresh. Detecting and refusing the silent case
is the load-bearing change here; an explicit "switch account, wipe data" UX is a follow-up
if anyone needs it.

**Why this is in scope (not deferred).** Without the upstream-id field captured at the
*first* connect, we lose the ability to retrofit the check later — there's no way to
reconstruct which Strava athlete or Garmin profile the existing tokens belong to without
calling the upstream API, and by then the user may already have reconnected with a
different account. Every connect from W2/W3 onwards must record the upstream id.

---

## 4. Code surface

| Layer | Change | Files |
|---|---|---|
| DB / migrations | None | — |
| Models | None | `models.py` (FitnessAuthState already has user_id) |
| Repository | None | `db/fitness_repository.py` |
| Providers | None | `providers/strava.py`, `providers/garmin.py` |
| Services / fetch | None | `services/fitness/fetch.py` |
| Services / backfill | Wrap as job worker | `services/fitness/backfill.py` (no changes), new workers |
| Job workers (new) | `fitness_backfill_strava`, `fitness_backfill_garmin` | `services/jobs/workers/fitness_backfill_*.py` |
| Job runner | New `submit_fitness_backfill_*` methods | `services/jobs/runner/*` |
| API (new) | Garmin connect/MFA/disconnect, Strava authorize-url/exchange/disconnect, backfill submit | `api/fitness.py` (or new `api/fitness_auth.py` if it grows) |
| API (modify) | `/api/fitness/integrity` gains user_id filter | `api/fitness.py` |
| Pending-session stores | Two new in-memory maps (Garmin MFA `state` + Strava OAuth `state`), each keyed by 256-bit CSPRNG token, value bound to `(user_id, expires_at)`, 10-min TTL, lazy expiry | `services/fitness/garmin_pending.py` (new), `services/fitness/strava_pending.py` (new) |
| Upstream identity capture | On connect, fetch + store provider's stable account id (`athlete.id` / `display_name`) into `fitness_auth_state.extra_state_json` to power D8's reconnect-account-switch check | `services/fitness/auth_capture.py` (new helper) or inline in connect endpoints |
| MCP tool | `fitness_integrity_check` gains user_id filter | `mcp_server/tools/fitness.py` |
| CLI | `--user-id` becomes required on all five fitness subcommands | `cli/__init__.py`, `cli/fitness.py` |
| CLI / Garmin | Drop `GARMIN_USERNAME` / `GARMIN_PASSWORD` env reads; require `--username`/`--password` flags or interactive prompts | `cli/fitness.py` |
| Config | Drop `garmin_username` / `garmin_password` fields; keep Strava fields | `config.py` |
| Webapp / API client | New: `connectGarmin`, `submitGarminMfa`, `disconnectGarmin`, `getStravaAuthorizeUrl`, `exchangeStravaCode`, `disconnectStrava`, `triggerBackfill` | `webapp/src/api/fitness.ts` |
| Webapp / view | New `FitnessConnectionsPanel.vue` mounted in `SettingsView.vue` | `webapp/src/views/SettingsView.vue`, `webapp/src/components/settings/FitnessConnectionsPanel.vue` |
| Webapp / route | New route `/settings/fitness/strava/callback` → `StravaCallbackView.vue` | `webapp/src/router/index.ts`, `webapp/src/views/StravaCallbackView.vue` |
| Webapp / store | Extend fitness store with connection-status + connect/disconnect actions | `webapp/src/stores/fitness.ts` |
| Webapp / banner | `FitnessAuthBanner.vue` — change CLI re-auth instructions to "Reconnect via Settings" link | `webapp/src/components/FitnessAuthBanner.vue` |
| Docs | Update `fitness-operations.md`, `fitness-integration-plan.md` (amend Q2), `configuration.md`, `roadmap.md` | various |

**Boundary discipline preserved.** No new cross-package imports. `services/fitness/` still
talks only to `db.fitness_repository`, `services.jobs`, and `services.notifications` (per
the boundary rule in `fitness-integration-plan.md` §4).

---

## 5. Work units

Sequenced for safe incremental delivery. Each unit is "ship a green CI" sized — under a
day's work.

### W1. Pre-flight data audit (read-only)
**Status:** shipped 2026-05-09 (server `4dd90c4`).
**Priority:** Critical. **Depends on:** none.
- Add `tests/integration/test_fitness_data_isolation.py` (or a CLI subcommand
  `journal fitness-audit`) that asserts every `fitness_auth_state`, `fitness_sync_runs`,
  `fitness_activities`, `fitness_daily`, and `fitness_raw_*` row has a non-NULL `user_id`
  matching a user in `users`.
- Run against a copy of the prod DB and confirm clean. Snapshot the row counts as the
  baseline regression target for W14.
- **Acceptance.** Audit passes locally and against the prod-DB copy. Output captured in
  the journal entry.

### W2. Garmin pending-session store + connect/MFA endpoints
**Status:** shipped 2026-05-10 (server `59f7714`).
**Priority:** Critical. **Depends on:** W1.
- New `services/fitness/garmin_pending.py` implementing a small in-memory map keyed by a
  256-bit CSPRNG token. Each entry holds `(user_id, garmin_state, created_at, expires_at)`
  with a 10-minute TTL and lazy expiry on read. No threads, no Events, no Queues — the
  library's `return_on_mfa` flow makes that machinery unnecessary (see D2).
- New endpoints in `api/fitness.py`: `POST /api/fitness/garmin/connect`,
  `POST /api/fitness/garmin/connect/mfa`, `POST /api/fitness/garmin/disconnect`.
- **Connect endpoint:** pull `user_id` from session, instantiate
  `Garmin(username, password, return_on_mfa=True)`, call `login()` (FastAPI runs the
  blocking call in its threadpool — no manual thread management). On
  `("needs_mfa", state)`: store an entry keyed by a fresh CSPRNG token bound to
  `user_id`, return `{mfa_required: true, pending_session, expires_at}`. On successful
  login: capture the token blob via `client.client.dumps()`, fetch the upstream profile
  (D8) via `client.get_user_profile()`, persist tokens + upstream-id via
  `repo.upsert_auth_state(...)`, drop the password from local scope, return
  `{connected: true}`. On any auth failure: apply the per-email cool-down (D2), return a
  useful error, and surface 429 from Garmin distinctly so the UI can show "too many
  attempts, try again in N minutes."
- **MFA endpoint:** validate `pending_session`, **reject with 403 if `pending.user_id !=
  current_user.user_id`** (per D2 security note), call `client.resume_login(state, code)`,
  persist token blob + upstream profile, return `{connected: true}`. Surface "wrong code"
  and "post-MFA failure" as distinct errors (post-MFA can fail intermittently per
  `python-garminconnect` issues #312/#337).
- **Reconnect with different upstream account** (per D8): if a `fitness_auth_state` row
  already exists for this user/source and the newly-fetched upstream id differs from the
  stored one, refuse the connect and return an error explaining the user must disconnect
  first.
- **Disconnect endpoint:** delete the user's `fitness_auth_state` row for `source='garmin'`.
- Tests with a fake `Garmin` factory covering: no-MFA success, MFA-required success, wrong
  MFA code, post-MFA fetch failure, expired pending session, **cross-user pending-session
  attempt (rejected with 403)**, **reconnect with same upstream id (allowed)**,
  **reconnect with different upstream id (rejected)**, per-email cool-down after repeated
  failures.
- **Acceptance.** New endpoints documented in `docs/api.md`. Unit tests cover all branches
  including cross-user rejection, upstream-id mismatch rejection, and cool-down behavior.
  Manual end-to-end against real Garmin from a dev laptop.

### W3. Strava OAuth endpoints (state-bound exchange)
**Status:** shipped 2026-05-10 (server `5dca0cc`).
**Priority:** Critical. **Depends on:** W1.
- New endpoints: `GET /api/fitness/strava/authorize_url`,
  `POST /api/fitness/strava/exchange`, `POST /api/fitness/strava/disconnect`.
- `authorize_url` issues a 256-bit CSPRNG state token, stores `(user_id, state,
  expires_at)` in an in-memory map with 10-min TTL, returns `{authorize_url, state}`.
- `exchange` validates the state token AND **rejects with 403 if `pending.user_id !=
  current_user.user_id`** (per D4 security note). On valid state: calls
  `providers.strava.exchange_code`, captures `athlete.id` from the response payload (D8),
  and persists tokens + upstream id via `repo.upsert_auth_state(...)`. Idempotent —
  re-exchange of the same code is fine.
- **Reconnect with different upstream account** (per D8): if a `fitness_auth_state` row
  already exists for this user/source and the newly-fetched `athlete.id` differs from the
  stored one, refuse the exchange and return an error explaining the user must disconnect
  first.
- **Disconnect:** delete the user's `fitness_auth_state` row for `source='strava'`.
- **Acceptance.** Unit tests with a fake `exchange_code` factory cover: happy path, invalid
  state, expired state, **cross-user state attempt (rejected with 403)**, **reconnect with
  same `athlete.id` (allowed)**, **reconnect with different `athlete.id` (rejected)**, and
  Strava-side errors. Manual end-to-end with the real Strava app.

### W4. Per-user integrity check
**Status:** shipped 2026-05-10 (server `fed3775`).
**Priority:** High. **Depends on:** none.
- Modify `db.fitness_integrity` (or wherever the orphan query lives) to take `user_id`.
- Modify `GET /api/fitness/integrity` and `mcp_server/tools/fitness.fitness_integrity_check`
  to pass the calling user's id and filter results.
- Existing tests updated; new tests assert orphans owned by user A do not appear in user B's
  report.
- **Acceptance.** No global orphan list ever returned to a non-admin user.

### W5. Backfill job workers + endpoint
**Status:** shipped 2026-05-10 (server `be6ab80`).
**Priority:** High. **Depends on:** W2 or W3 (whichever lands first — gives a connected
user to test against).
- New worker modules `services/jobs/workers/fitness_backfill_strava.py` and
  `fitness_backfill_garmin.py` wrapping `services/fitness/backfill.{backfill_strava,
  backfill_garmin}`. Read `user_id`, `start`, `end` from job params.
- New runner methods `submit_fitness_backfill_strava(user_id, start, end)` and
  `submit_fitness_backfill_garmin(user_id, start, end)` mirroring the existing
  `submit_fitness_sync_*` shape.
- **Idempotency / conflict policy.** Before enqueue, check whether any `queued` or
  `running` job exists for the same `(user_id, source)` across both sync and backfill
  worker classes. If one does, do **not** enqueue a new job — return the existing
  `{job_id}` so the UI can link to it. Policy: only one fetch job per `(user_id, source)`
  runs at a time; whichever was enqueued first wins, and a colliding submit (whether
  scheduled sync or on-demand backfill) is rejected idempotently. This prevents
  double-clicks and avoids backfill/sync interleaving on the same date window.
- **Mid-run robustness (applies to all four workers — both new backfill workers and the
  existing `fitness_sync_strava`/`fitness_sync_garmin`):** workers re-fetch
  `fitness_auth_state` at the start of each provider call. If the row is missing (user
  disconnected mid-run) or `auth_status='broken'`, mark the `fitness_sync_runs` row
  `failed` with `error="auth removed during run"` (or `"auth broken during run"`) and exit
  cleanly. Do not leave runs stuck in `running`.
- New endpoint `POST /api/fitness/backfill/{source}` body `{start, end?}` — pulls user_id
  from session, validates the date range, enqueues the job (or returns the in-flight one
  per the idempotency rule above), returns `{job_id}`.
- New MCP tool `fitness_trigger_backfill(source, start, end)` mirroring `fitness_trigger_sync`,
  with the same idempotency behavior.
- **Acceptance.** Worker unit tests use a fake backfill orchestrator and cover: happy
  path, **concurrent-submit rejection (returns existing job_id rather than enqueueing)**,
  **mid-run disconnect handled cleanly (run row ends `failed`, no stuck `running` rows)**,
  **mid-run auth_status flip to broken handled cleanly**. Endpoint test covers happy path
  + validation. Manual run against a small window.

### W6. Drop global Garmin env vars
**Status:** shipped 2026-05-11 (server `6064145`).
**Priority:** Medium. **Depends on:** W2 (the new connect endpoints are the replacement).
- Remove `garmin_username` and `garmin_password` fields from `config.py`.
- Update `cli/fitness.py` `cmd_fitness_reauth_garmin` to require `--username` and
  prompt for password via `getpass()` (no env-var fallback). MFA prompt unchanged.
- Update `docs/configuration.md` and `docs/fitness-operations.md` — remove references
  to `GARMIN_USERNAME`/`GARMIN_PASSWORD`. Add operator note: prod env still has them
  set; safe to remove after deploy.
- Note in journal entry: prod env also has a vestigial `STRAVA_REFRESH_TOKEN` not read
  anywhere — safe to remove same time.
- **Acceptance.** `grep -r GARMIN_USERNAME src/ tests/ docs/` returns nothing in active
  code.

### W7. CLI `--user-id` required
**Status:** shipped 2026-05-10 (server `14ddb6b`).
**Priority:** Medium. **Depends on:** none.
- In `cli/__init__.py`, change `--user-id` arg from `default=1` to `required=True` on all
  five fitness subcommands (`fitness-reauth-strava`, `fitness-reauth-garmin`,
  `fitness-sync`, `fitness-backfill`, `fitness-status`).
- Drop `_DEFAULT_USER_ID = 1` from `cli/fitness.py`.
- Update `docs/fitness-operations.md` examples — every command shows `--user-id N`
  with no implicit default.
- **Acceptance.** Tests updated. Running any `fitness-*` subcommand without `--user-id`
  exits non-zero with an argparse error.

### W8. Webapp API client
**Status:** shipped 2026-05-10 (webapp `4de33c4`).
**Priority:** High. **Depends on:** W2, W3, W5.
- Extend `webapp/src/api/fitness.ts` with `connectGarmin`, `submitGarminMfa`,
  `disconnectGarmin`, `getStravaAuthorizeUrl`, `exchangeStravaCode`, `disconnectStrava`,
  `triggerBackfill(source, start, end?)`.
- Type definitions match the response shapes from W2/W3/W5.
- **Acceptance.** `npm run lint` + `npm run build` pass. Vitest unit tests for each
  client function.

### W9. Webapp settings panel
**Status:** shipped 2026-05-10 (webapp `6df8d7e`).
**Priority:** High. **Depends on:** W8.
- New `webapp/src/components/settings/FitnessConnectionsPanel.vue`. Two cards (Garmin,
  Strava). Each card shows current connection status (not connected / connected (since
  date) / broken (since date)), Connect / Disconnect / Reconnect buttons, and a Backfill
  section visible when connected.
- Garmin card: form with email + password fields. On submit, calls `connectGarmin`. If
  response is `{mfa_required: true}`, swap the form for an MFA code input bound to the
  pending session token. On success, refresh the panel.
- Strava card: "Connect Strava" button calls `getStravaAuthorizeUrl` and redirects the
  current tab (or opens a new tab — pick one and stick with it; recommend same tab so
  the callback returns to the SPA naturally) to `authorize_url`.
- Backfill section: source-specific date picker (default `2026-01-01`), button → calls
  `triggerBackfill`, shows the resulting job id and links to the jobs view.
- Mount the panel inside `SettingsView.vue` as a new section.
- **Acceptance.** Vitest unit tests for the panel's state transitions (not connected →
  connecting → MFA prompt → connected; backfill submit). Coverage stays above 85%
  thresholds.

### W10. Strava callback route
**Status:** shipped 2026-05-10 (webapp `d53f3a7`).
**Priority:** High. **Depends on:** W8.
- New route `/settings/fitness/strava/callback` → `StravaCallbackView.vue`.
- The view reads `code` and `state` from the query string, calls `exchangeStravaCode`,
  shows a brief "Connecting Strava..." spinner, then redirects to `/settings#fitness`
  on success or `/settings#fitness?strava_error=...` on failure.
- Handle the `error` query param (user denied authorization).
- **Acceptance.** Vitest test covers happy path and the three failure modes (denied,
  bad state, exchange error). Manual end-to-end against real Strava.

### W11. Update the FitnessAuthBanner copy + verify the `auth_status='broken'` path
**Status:** shipped 2026-05-10 (server `18d66b0`, webapp `c5968c3`).
**Priority:** Medium. **Depends on:** W9.
- The existing `FitnessAuthBanner.vue` directs broken users to CLI commands. Change the
  CTA to a "Reconnect" button that routes to `/settings#fitness`.
- **Pre-flight verification (per D3):** the banner is only useful if sync workers
  actually flip `fitness_auth_state.auth_status` to `'broken'` on Garmin/Strava 401s and
  expired-token responses. Read each worker's error path and confirm the field is set
  (not just `error` written to `fitness_sync_runs`). If the flip is missing, add it as
  part of this unit — otherwise the banner stays green forever for users whose tokens
  silently expired.
- Add a test at the worker level that asserts `auth_status='broken'` is written when the
  provider returns 401.
- **Acceptance.** Banner test updated. Worker tests assert `auth_status` is flipped on
  401. Manual confirmation: revoke a Strava token, run a sync, observe banner light up.

### W12. Docs sweep
**Priority:** Medium. **Depends on:** W2–W11 landed.
- `docs/fitness-integration-plan.md` — amend resolved Q2 (or add a Q7) noting the
  multi-user pivot and link this plan.
- `docs/fitness-operations.md` — new "Connecting via the webapp" section as the primary
  path; existing CLI sections become "operator fallback" (and gain `--user-id N`
  everywhere). Note env vars removed in W6.
- `docs/configuration.md` — drop GARMIN_*; mark STRAVA_REDIRECT_URI as the webapp
  callback URL.
- `docs/api.md` — document the new endpoints.
- `docs/roadmap.md` — link this plan; add it to "Active planning docs". Cross-out
  "in-app re-auth flow" from Tier 1 #1 deferred list when this ships.
- **Acceptance.** `grep -r GARMIN_USERNAME docs/` returns nothing in active docs.
  Roadmap and integration plan link this doc.

### W13. Strava developer-app callback URL update (operator step)
**Priority:** Critical (one-time, manual). **Depends on:** W3, W10 deployed.
- Operator updates the Strava app's Authorization Callback Domain at developers.strava.com
  to the webapp's prod hostname.
- Update `STRAVA_REDIRECT_URI` in the prod `.env` to
  `https://<webapp>/settings/fitness/strava/callback`.
- Document in the journal entry; do **not** delete the old laptop-bootstrap path from
  the docs (W11 inline-python recipe remains valid for emergency dev re-auth).

### W14. End-to-end verification with user 2
**Priority:** Critical. **Depends on:** all above.
- In a prod-like environment (or staging if available), log in as the demo user
  (mthwsjc+demo@gmail.com), connect Strava, connect Garmin, trigger a 7-day backfill
  for each.
- Verify: data appears for user 2 only, user 1's data is unchanged, sync_runs are
  scoped per-user, integrity check returns clean per-user reports.
- Re-run the W1 audit script: every row still has correct user_id.
- **Acceptance.** Screenshots of user 2's fitness view alongside user 1's, plus the
  audit script output. Captured in the journal entry.

**Parallelism.** W2, W3, W4, W7 are independent and can ship as separate PRs in any
order. W5 depends on at least one of W2/W3 being merged so we can manually verify with
real tokens. W8 depends on W2/W3/W5. W9/W10/W11 depend on W8. W6, W12 are docs/cleanup,
ship after the relevant code lands. W13 is operator-only after W3+W10 deploy. W14 is the
gate.

---

## 6. Migration & data integrity

No SQL migrations. The audit script (W1) is the safety net. Rollback strategy:

- **W2/W3 endpoints rollback.** Revert the deploy. Existing tokens in
  `fitness_auth_state` continue to drive sync from CLI/jobs. No data loss.
- **W6 env-var removal rollback.** Re-add the env vars. CLI Garmin re-auth from env
  works again. No data loss.
- **W13 Strava callback URL rollback.** Update the Strava app back to the old domain
  and revert `STRAVA_REDIRECT_URI`. Existing user 1 tokens are unaffected (refresh
  doesn't touch the callback URL — only initial OAuth does).

The only irreversible change is user 2 connecting their accounts — but that's the goal,
not a regression.

**No backfill of existing data.** User 1's 80+80 activities and 129 daily rows already
carry user_id=1. User 2 starts empty and the W5 backfill button populates them on demand.

**Worker robustness against mid-run auth removal.** A user may disconnect a source
(deleting their `fitness_auth_state` row) while a sync or backfill job is mid-run. The
W5 hardening makes all four fitness workers re-read auth state at each fetch step and
fail the run cleanly with `error="auth removed during run"` rather than leaving rows
stuck in `running`. This applies retroactively to the existing `fitness_sync_*` workers,
not just the new backfill workers.

**Upstream account identity is captured at every connect (D8).** Every connect from W2/W3
forward records the provider's stable account id (Strava `athlete.id`, Garmin
`display_name`/profile id) into `extra_state_json`. Without this field captured at the
*first* connect, we cannot retrofit the silent-account-switch detection later. The check
is small but load-bearing for data integrity.

---

## 7. Out of scope

- **Multi-tenant Strava apps.** One server, one Strava developer app.
- **Token encryption-at-rest.** Plaintext tokens in SQLite, same as today.
- **Garmin password persistence.** Token-only; users re-enter password annually when the
  garth token expires.
- **OAuth state durability.** The Strava CSRF state token lives in process memory; if the
  server restarts mid-OAuth, the user retries. Acceptable.
- **Pending-session durability.** Same — single-process server, in-memory store.
- **Admin "view fitness for any user" feature.** Each user sees only their own data; no
  admin-impersonation UI.
- **Webhook-based Strava sync.** Polling unchanged.
- **The W7 normalize watermark fix.** Tracked separately in `fitness-operations.md` §7.
- **The W11 OAuth listener / `--code <code>` CLI shortcut.** Separate small follow-up;
  unchanged by this plan.

---

## 8. Kill criteria

We would abandon, defer, or significantly redesign this initiative if:

- Garmin permanently breaks the `return_on_mfa` / `resume_login` flow such that
  in-browser auth becomes infeasible (then revert to operator CLI re-auth as a "shared
  admin runs it for each user" pattern).
- The single-process pending-session model proves insufficient (e.g. we move to multi-replica
  deployment) — then both pending-state stores need externalised state (Redis), and the
  cost-benefit shifts toward keeping Garmin re-auth as a CLI-only path.
- We discover a third user wants Strava data with a separate developer app's rate limits —
  then revisit D1.

**Forcing event on the calendar (not a kill criterion, but plan accordingly).** Garmin's
**OAuth1 retirement on 2026-12-31** will require every connected user to reconnect, possibly
via a `python-garminconnect` library bump that invalidates existing token blobs. If that
migration lands before this plan ships, fold OAuth2-PKCE handling into W2 rather than
shipping the OAuth1 flow and immediately retiring it.

---

## 9. References

- [`fitness-integration-plan.md`](./fitness-integration-plan.md) — underlying decisions; this
  plan amends the single-user item under §6 Q2 (or adds Q7).
- [`fitness-schema.md`](./fitness-schema.md) — schema, unchanged by this plan.
- [`fitness-pipeline.md`](./fitness-pipeline.md) — data-flow, unchanged.
- [`fitness-operations.md`](./fitness-operations.md) — operator runbook, gets a new
  "Connecting via the webapp" section (W12).
- [`api.md`](./api.md) — gets new endpoint docs (W12).
- [`configuration.md`](./configuration.md) — env-var reference, drops GARMIN_* (W12).
- [`roadmap.md`](./roadmap.md) — links this plan once approved.
- Provider docs: <https://developers.strava.com/docs/authentication/>,
  <https://github.com/cyberjunky/python-garminconnect>.
