# W13 — Strava developer-app callback URL flip (operator step)

Date: 2026-05-11
Plan: `docs/fitness-multiuser-plan.md` §5 W13

## What changed

Two coordinated edits in production, both completed by the operator
(John) on 2026-05-11. No code, no commits, no CI. The pipeline only
notices once W14's first user 2 connect attempt hits the prod callback.

1. **Strava developer-app Authorization Callback Domain** at
   `developers.strava.com` updated to the prod webapp hostname.
   Strava's field accepts a bare domain only (no scheme, no path), and
   redirects from `/oauth/authorize` to any URL under that domain.
2. **`STRAVA_REDIRECT_URI` in prod `.env`** flipped to the full webapp
   callback URL:
   `https://journal-insights.itsa-pizza.com/settings/fitness/strava/callback`.
   This is the value embedded in the authorize URL minted by
   `GET /api/fitness/strava/authorize_url` (W3) and what the Strava
   token-exchange endpoint matches against during W3's
   `POST /api/fitness/strava/exchange` call.

## Why this is one operator change, not two unrelated ones

Strava enforces that the `redirect_uri` parameter on every OAuth
roundtrip is under the registered Authorization Callback Domain. If the
two values disagree — for example, the env var points at the webapp but
the developer-app's callback domain is still `localhost` — Strava
refuses the authorize step with an opaque error. Both edits ship
together or neither ships.

## Verification posture

No automated tests verify W13. The webapp settings panel does not
ping the OAuth round-trip on load (it just renders the connection
card), so the breakage surface is "user 2 clicks Connect Strava in W14
and the redirect to `/settings/fitness/strava/callback` either lands
cleanly or 4xxs at Strava."

User 1 is already connected via the pre-W13 laptop-listener flow, so
their tokens stay valid — Strava only re-validates the callback domain
at the *next authorize step*, not on each refresh-token call.
Refreshes continue to work against the old tokens.

## Rollback

Reverse both edits together:

1. Restore the Authorization Callback Domain at developers.strava.com
   to its previous value (was `localhost` per the §2d / §2e CLI
   listener path).
2. Revert `STRAVA_REDIRECT_URI` in the prod `.env` to
   `http://localhost:8400/strava/callback`.

User 1's existing tokens are not affected by the rollback for the same
reason as above — refreshes don't re-check the callback domain.

## Out of scope

User 2 has not connected Strava yet. That happens in W14 (end-to-end
verification). The W13 change unblocks W14 but does not perform it.
