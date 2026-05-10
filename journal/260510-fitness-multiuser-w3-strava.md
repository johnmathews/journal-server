# 260510 — fitness multi-user W3: Strava OAuth state-bound exchange

Third work unit from `docs/fitness-multiuser-plan.md`. Ships the per-user
Strava OAuth flow as three new REST endpoints plus an in-process pending-
state store. From this unit forward, a logged-in journal user can authorise
their own Strava account through the API instead of the laptop CLI listener
path.

## What shipped

Three new endpoints on `api/fitness.py`:

- `GET /api/fitness/strava/authorize_url` — issues a 256-bit CSPRNG state
  token bound to the calling user, stores `(user_id, expires_at)` in the
  in-memory pending-state store with a 10-minute TTL, and returns
  `{authorize_url, state, expires_at}`. The authorize URL embeds the
  configured `STRAVA_CLIENT_ID`, `STRAVA_REDIRECT_URI`, and the state token
  as the OAuth `state` parameter.
- `POST /api/fitness/strava/exchange` — body `{code, state}`. Peeks the
  pending entry, rejects with 403 if `entry.user_id != user.user_id` (D4
  cross-user replay protection — leaves the entry in place so the
  legitimate user can still complete its own flow), consumes the state,
  calls `providers.strava.exchange_code(...)`, captures the upstream
  `athlete.id`, runs the D8 mismatch check, and persists tokens + upstream
  id via `repo.upsert_auth_state(...)`.
- `POST /api/fitness/strava/disconnect` — deletes the user's
  `fitness_auth_state` row for `source='strava'`. Idempotent.

One new module:

- `services/fitness/strava_pending.py` — `StravaPendingStore` mapping
  `token → StravaPendingState(user_id, expires_at)`. Lazy expiry, 10-min
  TTL, peek-vs-consume, thread-safe. ~70 lines, parallel to (not derived
  from) `garmin_pending.py` — see "Generalisation decision" below.

One signature change inside the existing provider seam:

- `providers/strava.py:exchange_code` now returns `(Tokens, str | None)`
  instead of `Tokens`. The `str | None` is the upstream `athlete.id`,
  rendered as a string for parity with Garmin's D8 mismatch comparison.
  Same SDK roundtrip — stravalib's `Client.exchange_code_for_token`
  already supports `return_athlete=True`, so this captures the upstream
  identity without a second HTTP call. The single CLI caller
  (`cli/fitness.cmd_fitness_reauth_strava`) was updated to unpack the
  tuple and persist the athlete id into `extra_state["upstream_user_id"]`
  too — every connect from W3 onwards records the upstream id, including
  the operator-driven CLI path, because D8 retrofit later is impossible.

Tests:

- `tests/test_strava_pending.py` — 10 unit tests covering issue/peek/
  consume/TTL-sweep/lazy-expiry/user-binding contracts.
- `tests/test_api_fitness_strava_auth.py` — 14 API tests exercising every
  branch through a fake `exchange_code` injected via
  `services["strava_exchange_code"]`: authorize_url shape + state binding,
  missing client_id 500, exchange happy path, missing fields 400, unknown
  state 410, expired state 410, cross-user state 403, reconnect with same
  athlete.id (allowed), reconnect with different athlete.id (409), missing
  athlete identity (502), Strava-side `AccessUnauthorized` (502),
  disconnect when not connected, disconnect after connect, disconnect
  scoped per-user.
- `tests/test_cli_fitness.py` — three existing Strava re-auth tests
  updated to mock the new `(Tokens, athlete_id)` return shape; the happy-
  path test gained an upstream-id assertion to lock the new D8 capture.

Total suite: **2203 passing** (was 2179 before W3 — 24 new tests, with the
3 CLI tests updated rather than added). Ruff clean.

## Generalisation decision: parallel modules, not a shared helper

The W2 journal flagged "worth seeing if a tiny generic helper falls out
in W3" for `garmin_pending.py` ↔ a Strava equivalent. After reading both
shapes side by side I kept them parallel.

**The locking / sweep / CSPRNG-token / TTL / peek-vs-consume scaffold is
genuinely identical** — about 40 lines repeated. That is the case *for*
extracting a helper.

**The value types diverge in ways the helper would have to paper over.**
Garmin's `PendingSession` carries `(user_id, client, state_token,
expires_at)` — the `client` is a *live* `garminconnect.Garmin` instance
that must outlive the connect endpoint and survive into the MFA endpoint
where `resume_login` is called on the same client. Strava's
`StravaPendingState` carries only `(user_id, expires_at)`: the OAuth
state is just a CSRF tag and there's no SDK session to keep alive across
HTTP calls. Forcing both into a `Generic[PayloadT]` base would obscure
the most load-bearing fact about each module — *what each entry is
guarding* — behind type variables that resolve differently in each file.

Two reasons to wait for a third use case before factoring:

1. **The shapes converge on infrastructure but diverge on payload.** The
   value of a generic store is greatest when several callers want exactly
   the same payload contract. With two callers, "same scaffold, different
   payload" is the whole story; abstraction adds an indirection without
   removing the irreducible variation.
2. **Reading each module standalone is the more frequent operation.**
   Future debugging will more often ask "what does the Strava store
   hold?" than "how does the locking work?"; parallel modules answer
   both at a glance.

Concretely: `strava_pending.py` is ~70 LOC self-contained. `garmin_
pending.py` is unchanged. If a third pending-state user appears (Whoop?
Oura? — neither in scope today), the two existing modules become the
specification for what a generic helper would need to support, and the
extraction is mechanical. Until then the user's stated preference for
"small-and-similar over premature abstraction" applies cleanly.

## Plan-vs-code deltas

The plan's §3 D4 / §5 W3 captured the architectural shape correctly; the
implementation drifted only on the SDK-shape questions the brief
explicitly flagged for "resolve by reading code first."

1. **`exchange_code` shape.** The brief offered three options: extend
   `exchange_code` with a `return_athlete=True` kwarg, swap to a tuple
   return, or call out to stravalib a second time. I went with
   *unconditional tuple return*. Reasoning: there is exactly one existing
   caller (the CLI), and the tuple form is cleaner at the seam than a
   bool kwarg with a union return type. The CLI gained a one-line
   `tokens, athlete_id = exchange_code(...)` plus a guarded
   `extra["upstream_user_id"] = athlete_id` — three lines of CLI churn for
   a permanently consistent API. Three call-site test mocks were updated
   in the same commit.

2. **State token consumed on Strava-side error too.** The brief says
   "Idempotent — same state token can't be reused (consume on success)."
   I made the state get consumed on Strava-side errors as well. OAuth's
   CSRF state is a one-shot value: re-using it on a retry would defeat
   the guarantee, and "the user must repeat the connect form after a
   Strava-side rejection" is the right UX (it gives them a chance to
   re-authorise rather than retrying with a stale code that Strava has
   already declined). The state is *not* consumed on cross-user replay
   (403) — same reasoning as W2's MFA endpoint, the legitimate user can
   still complete its flow.

3. **Athlete-id-missing → 502, not silent-pass.** stravalib's docstring
   says `return_athlete=True` is "currently undocumented and could change
   at any time" and that the athlete payload "Will be None if Strava
   doesn't return it." Without an upstream id we cannot enforce D8 on
   future reconnects, so persisting tokens with `upstream_user_id=None`
   would be the same data-integrity bug D8 is designed to prevent. The
   endpoint refuses to persist (502 with `reason: missing_upstream_
   identity`) and forces a retry. The plan is silent on this case;
   failing closed is the safer default.

4. **`STRAVA_REDIRECT_URI` is unchanged in this unit.** Per the brief and
   D4, the redirect URI is now the webapp callback URL, not the backend
   listener path. The W3 endpoint just propagates whatever's in the env
   var into the authorize URL — the actual cutover from
   `http://localhost:8400/strava/callback` (today's prod default) to the
   webapp URL is operator work in W13. The CLI listener flow still works
   for dev/laptop bootstrap until that operator step lands.

## Two gotchas worth recording

1. **`return_athlete=True` is documented as best-effort.** Strava's
   own SDK calls it out: "this return is currently undocumented and
   could change at any time." Calling it from W3 today is fine; the 502
   fallback at the endpoint is the durable backstop if Strava ever stops
   returning it. If that happens, the recovery is to fall back to a
   second SDK call (`Client(access_token=...).get_athlete()`) at the
   endpoint level, not to relax the D8 invariant.

2. **The state is consumed before the SDK call, not after.** I tried the
   "consume on success only" shape first — but if the SDK call hangs or
   the request is double-clicked, the second click could find the same
   state still pending and start a second exchange against the same code.
   Strava would 401 the second exchange (codes are single-use), but the
   *order* of failures is racy. Consuming the state up front gives the
   cleaner property: any second attempt with the same state lands on the
   410 path, regardless of timing.

## Files touched

Code:

- `src/journal/api/fitness.py` — three new endpoints + helpers
  (`_strava_pending`, `_strava_exchange`, `_persist_strava_auth`).
- `src/journal/services/fitness/strava_pending.py` — new module.
- `src/journal/providers/strava.py` — `exchange_code` return shape change
  to `(Tokens, str | None)`.
- `src/journal/cli/fitness.py` — unpack the new tuple, capture
  `upstream_user_id` into `extra_state` on CLI re-auth.

Tests:

- `tests/test_strava_pending.py` — new (10 tests).
- `tests/test_api_fitness_strava_auth.py` — new (14 tests).
- `tests/test_cli_fitness.py` — three existing Strava tests updated for
  the new `exchange_code` shape; happy-path gains an upstream-id
  assertion.

Docs:

- `docs/api.md` — three new endpoint sections under
  `### GET /api/fitness/strava/authorize_url`,
  `### POST /api/fitness/strava/exchange`, and
  `### POST /api/fitness/strava/disconnect`, slotted alongside the W2
  Garmin endpoints.
- `docs/fitness-operations.md` — new §2e "Connecting Strava via the
  webapp" stub note pointing at api.md and the multi-user plan; full
  operator section comes in W12 once the webapp UI lands.

## What's next

W4 (per-user integrity check) and W7 (`--user-id` required) are
independent of W3 and can ship in any order. W5 (backfill workers) gates
on at least one of W2/W3 being merged so we can run a real backfill — W3
satisfies that for Strava. W8 (webapp API client) depends on W3 for the
Strava endpoints to call.

Recommend a fresh session for the next unit. The W3 module surface (Strava
endpoints, stravalib seam, OAuth state) is largely orthogonal to W4/W5/W7,
and any of those would benefit from a fresh context that reads the
relevant existing code without inheriting W3's stravalib-and-OAuth bias.
