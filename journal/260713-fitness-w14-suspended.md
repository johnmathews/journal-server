# Fitness W14 verification suspended; Strava integration suspended

**Date:** 2026-07-13

## What happened

Prepared the long-pending fitness-multiuser W14 acceptance session (end-to-end
verification with user 2 per `docs/archive/fitness-multiuser-plan.md`), then
suspended it before the OAuth stage on John's call: Strava paywalled
Standard-tier API access behind an active Strava subscription (~$11.99/mo)
effective **2026-06-30**
([announcement](https://communityhub.strava.com/insider-journal-9/an-update-to-our-developer-program-13428)),
so the Strava half of the walkthrough is impossible without paying, and the
Garmin-only half was deliberately skipped for now.

## Baseline captured before suspension

`docker exec journal-server uv run journal fitness-audit` (2026-07-13):

- **PASS, 0 violations** — every row carries `user_id=1`.
- `fitness_activities` 295 · `fitness_daily` 193 · `fitness_sync_runs` 78 ·
  `fitness_raw_strava` 140 · `fitness_raw_garmin` 1408 · `fitness_auth_state` 1.
- Users in prod: 1 (admin), 2 (`mthwsjc+demo@gmail.com`, verified, empty), 3.
- User 1's auth state: `garmin: broken`, **no Strava row** (the
  `STRAVA_REFRESH_TOKEN` env var was removed 2026-06-10 and Strava was never
  reconnected via the in-app flow).

If the walkthrough is ever revived (Garmin side), this is the regression
target: user 1's counts must be unchanged after user 2 connects.

## Decisions recorded

1. **Strava integration suspended** — code kept, no active connection, no
   plan to subscribe. Resume path: subscribe → Settings → Fitness → Connect.
   Revoking the old (already-removed-from-env) refresh token in Strava's web
   UI remains pending hygiene.
2. **W14 gate moved to roadmap D8** — no longer the standing "open gate";
   roadmap header, Item 1, and the multiuser-plan bullet updated accordingly.
3. **User 1's Garmin reconnect** (split-IP mint/import, operations runbook
   §2c-bis) noted in D8 as the step to take when fresh Garmin data is wanted.

## Files touched

- `docs/roadmap.md` — header, Item 1 (suspension record), Active-plans bullet,
  new D8.
- `docs/fitness-operations.md` — Strava suspension banner at the top.
