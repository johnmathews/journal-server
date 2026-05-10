# 260510 — fitness multi-user W2: Garmin connect / MFA / disconnect

Second work unit from `docs/fitness-multiuser-plan.md`. Ships the per-user
Garmin login flow as three new REST endpoints plus an in-process pending-
session store and a per-email cool-down tracker. From this unit forward, a
logged-in journal user can connect their own Garmin Connect account through
the API without env-var credentials and without operator involvement.

## What shipped

Three new endpoints on `api/fitness.py`:

- `POST /api/fitness/garmin/connect` — body `{username, password}`. Builds
  `Garmin(email=..., password=..., return_on_mfa=True)` and runs `login()`
  inside `asyncio.to_thread` (the existing fitness routes are `async def` —
  the threadpool wrap is the right level). On no-MFA success the handler
  fetches the upstream profile, captures the token blob via
  `client.client.dumps()`, and persists via `repo.upsert_auth_state(...)`
  with `auth_status='ok'`. On `("needs_mfa", legacy)` the live `Garmin`
  client is parked in the pending-session store under a fresh 256-bit
  CSPRNG token bound to `user.user_id`, and the response carries
  `{mfa_required: true, pending_session, expires_at}`.
- `POST /api/fitness/garmin/connect/mfa` — body `{pending_session, code}`.
  Peeks the pending entry, rejects with 403 if `pending.user_id !=
  user.user_id` (D2 cross-user replay protection — preserves the entry on
  rejection so the legitimate user can still complete their flow), calls
  `client.resume_login(state, code)`, fetches the upstream profile,
  captures the blob, persists, then consumes the pending entry.
- `POST /api/fitness/garmin/disconnect` — deletes the user's
  `fitness_auth_state` row for `source='garmin'`. Idempotent.

Two new modules:

- `services/fitness/garmin_pending.py` — `GarminPendingStore` (token →
  `(user_id, client, state_token, expires_at)`, lazy expiry, 10-min TTL)
  and `GarminCooldownTracker` (per-email failure window, configurable
  threshold + window, default 5 failures / 15 minutes).
- One new repo method: `FitnessRepository.delete_auth_state(user_id,
  source)` for the disconnect endpoint.

Tests:

- `tests/test_garmin_pending.py` — 16 unit tests covering the pending
  store TTL/sweep/peek-vs-consume + cool-down threshold/window/case-norm/
  reset behaviour.
- `tests/test_api_fitness_garmin_auth.py` — 19 API tests exercising every
  branch through a fake `Garmin` client injected via
  `services["garmin_client_factory"]`: no-MFA happy, MFA-required happy,
  wrong code, post-MFA fetch fail, expired pending, unknown pending,
  cross-user 403, reconnect with same upstream id, reconnect with
  different upstream id (409), per-email cool-down trips after threshold
  failures, missing-body-fields 400 for both endpoints, upstream 429
  surfaced distinctly, disconnect-when-not-connected, disconnect after
  connect, disconnect only affects calling user, factory kwargs check.

Total suite: 2179 passing (was 2144 before W2 — exactly 35 new tests).
Lint clean.

## Plan-vs-code deltas

The plan's §3 D2 / §5 W2 captured the architectural shape correctly; the
implementation only drifted on small details that surfaced once I started
reading the SDK source.

1. **`Garmin.login()` returns `(mfa_status, legacy_token)` on both paths,
   not just `("needs_mfa", state)`.** The success path returns
   `(None, legacy_token)`. The disambiguation is `result[0] == "needs_mfa"`,
   and the `state_token` we hand to `resume_login` is just the second
   element of the tuple — the *MFA state itself* lives on the live `Garmin`
   client instance, not the tuple. So the pending entry stores the live
   `Garmin` client (under field name `client`) plus the legacy_token (under
   `state_token`); calling `resume_login(state_token, code)` on the same
   client completes the flow. The plan's "store `(user_id, garmin_state,
   expires_at)` in an in-memory map" wording reads as if the state is a
   serialisable dict you stash and restore — in fact it's a live SDK
   instance you keep alive. Functionally equivalent for a single-process
   server; would matter if we ever externalised pending state to Redis
   (the kill-criteria section already flags this as a redesign trigger).

2. **The cool-down also fires on `429` upstream, not only on
   `GarminConnectAuthenticationError`.** The plan reads `429` as "surface
   distinctly" but doesn't say the local counter increments on 429 too.
   I made it count both, on the reasoning that an upstream-429-then-retry
   loop is exactly the failure mode the cool-down is meant to prevent.
   The body `reason` field still distinguishes `local_cooldown` from
   `upstream_rate_limited` for the UI.

3. **Cool-down policy: 5 failures within 15 minutes.** The plan says
   "after repeated failures … refuse retries for a short window" without
   pinning numbers. I picked threshold=5, window=15min as the defaults,
   exposed both as kwargs on `GarminCooldownTracker` for future tuning.
   Reasoning: Garmin's own clientId+email rate limiter is reportedly
   stingy (3-5 attempts / few minutes per `python-garminconnect` issue
   #344), so the local counter has to be tighter than the upstream limit
   to actually protect users — but not so tight that a quick fat-finger
   correction trips it. Five attempts is generous enough for two typos
   plus a "wait, I have an MFA app" pause; fifteen minutes matches a
   reasonable "the user walked away to find their phone" case.

4. **`Garmin.resume_login(client_state, mfa_code)` ignores
   `client_state`.** Reading the bundled `garminconnect.client.Client`
   source, the parameter is `_client_state` (underscored, unused) — the
   MFA state is stored on `self.client` and consumed by `_complete_mfa`.
   The pending entry still carries the legacy_token in `state_token` for
   future-proofing (if a library version starts using it the wiring is
   ready) but the current SDK doesn't read it.

5. **Upstream id source.** The plan suggests "`display_name` or profile
   id"; I used `profile.get("displayName")` falling back to
   `profile.get("userName")` and finally to the typed username (only on
   the no-MFA path, where `username` is in scope). On the MFA path the
   handler doesn't have the original username — the resume call is
   user-scoped via the pending entry, not the email — so the fallback
   chain is just `displayName` → `userName` → 502. This is the correct
   posture: D8's reconnect-detection only works if every connect captures
   a stable upstream id, so failing closed (502) when none is available
   is safer than persisting a row with an empty upstream id.

6. **D8 mismatch on the MFA path consumes the pending entry; on the no-
   MFA path it does not.** Asymmetric for a reason: the no-MFA mismatch
   path didn't even authenticate the new account meaningfully (`login`
   succeeded but we're not persisting), so the user can still connect a
   different account by disconnecting first and trying again. The MFA
   mismatch path *did* consume an MFA challenge — the pending session
   has served its purpose, and the user must repeat the connect form for
   any subsequent attempt. Either way, no auth state is updated.

## Cool-down policy details

The tracker normalises emails (strip + lower) before counting. Garmin's
own rate-limiter is documented as case-insensitive at the email level,
so `Alice@Example.com` and `alice@example.com ` must share a single
failure budget. There's a regression test for this — the
case-and-whitespace normalisation halves protective effect if it gets
removed accidentally.

`reset()` is called on the no-MFA success path with the original email.
On the MFA path, the original email isn't available at MFA time (the
pending entry doesn't carry it — we drop the password and don't keep
the username either, partly to minimise sensitive-data scope and partly
because the pending entry is keyed by user_id, not email). The MFA
success path doesn't `reset()`; failures age out within the window and
the user moves on. Acceptable trade — the failure budget is 5 within 15
minutes, so any successful flow has at most 4 stale failures left, well
under the threshold.

## Two gotchas worth recording

1. **`Garmin.login()` is genuinely synchronous.** Each fitness route is
   `async def` (matching the existing read endpoints), so the blocking
   call must be wrapped in `asyncio.to_thread`. Without that wrap a
   single connect attempt (which can take 5-10 seconds on the slow path)
   would block the entire event loop. The wrap is one line; the bug if
   missed is silent + production-only. Same wrap on `resume_login` and
   on `get_user_profile`.

2. **`fitness_auth_state` users-FK in tests.** Migration 0011 already
   seeds `user_id=1` so any test that upserts an auth row for user 2
   needs to insert that user first — `INSERT OR IGNORE` avoids the
   conflict against migration-seeded user 1. The
   `test_disconnect_only_affects_calling_user` test uses this pattern.

## What W5 still needs to add (not W2)

Per plan §6: workers must re-fetch `fitness_auth_state` at each provider
call and fail cleanly on missing/broken auth. I read
`services/fitness/fetch.py:137` — `run_sync` reads `get_auth_state`
exactly once at the start of the run, then builds the provider and goes.
A user disconnecting mid-run today would leave the in-flight fetch
running against now-deleted credentials until the SDK noticed (probably
401 on the next API call, but no fast-fail). W5 will need to add a
re-fetch at the top of each fetch loop iteration plus a cheap
`auth_status='broken'` check; W2 itself doesn't change worker behaviour.

## Files touched

Code:

- `src/journal/api/fitness.py` — three new endpoints + helpers.
- `src/journal/services/fitness/garmin_pending.py` — new module.
- `src/journal/db/fitness_repository.py` — new `delete_auth_state`.

Tests:

- `tests/test_garmin_pending.py` — new (16 tests).
- `tests/test_api_fitness_garmin_auth.py` — new (19 tests).

Docs:

- `docs/api.md` — three new endpoint sections under
  `### POST /api/fitness/garmin/connect[/mfa|...]`.
- `docs/fitness-operations.md` — new §2d "Connecting via the webapp"
  stub note pointing at api.md and the multi-user plan; full operator
  section comes in W12 once the webapp UI lands.

## What's next

W3 (Strava OAuth state-bound exchange) is the symmetric unit on the
Strava side. The same pending-store shape will mostly carry over — the
value type differs (Strava state token + redirect_uri context vs Garmin
live client) but the user-binding + TTL + 256-bit CSPRNG token + lazy
expiry scaffolding is identical. Worth seeing if a tiny generic helper
falls out, but force-factoring at one user is premature — W3 will tell
us whether the shapes really converge.

Recommend a fresh session for W3: different module surface
(`services/fitness/strava_pending.py` + `api/fitness.py` Strava-side
endpoints), different SDK (`stravalib`), different test scaffolding.
The pending-store/cool-down primitives are now in the codebase and the
W3 session can read them without inheriting W2 context bloat.
