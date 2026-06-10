# 2026-06-04 — `STRAVA_REFRESH_TOKEN` vestigial audit

**Status:** confirmed unused in code; operator step (prod `.env` removal)
deferred to the user.

## Context

The fitness multi-user plan (`docs/archive/fitness-multiuser-plan.md` once
W6 archives it) flagged `STRAVA_REFRESH_TOKEN` in prod's `.env` as
vestigial — predates the `fitness_auth_state` table introduced in
migration `0023_fitness_auth_and_sync.sql` (9 May 2026). Since W6 of that
plan, every Strava token (access + refresh) lives in the
`fitness_auth_state` row keyed by `(user_id, source='strava')`. The env
var hasn't been consulted by any code path since.

The W6 journal entry (`260510-fitness-multiuser-w6-drop-garmin-env.md`)
noted it as safe to drop alongside `GARMIN_USERNAME` / `GARMIN_PASSWORD`.
This entry closes the loop: the codebase audit is clean.

## What was verified

Two greps against the working tree on the
`fitness-multiuser-final-mile` branch:

```bash
grep -rn 'STRAVA_REFRESH_TOKEN' src/ tests/
grep -rn 'strava_refresh_token' src/ tests/
```

Both return zero matches. The string appears only in:

- `journal/260510-fitness-first-fetch.md` — historical context about
  the W6 env-var cleanup. Correct as-is.
- `journal/260510-fitness-multiuser-w6-drop-garmin-env.md` — same.
- `docs/fitness-operations.md` §1 operator note — flags it as safe to
  drop on the next deploy. Correct as-is; once the prod env is cleaned
  this note can be dropped, but it's load-bearing until then.
- `docs/fitness-multiuser-plan.md` — describes the smell as part of the
  pre-plan current-state survey. Will move into `docs/archive/` under
  W6 of this run.

`.env.example` does **not** list the variable. Verified:

```bash
grep STRAVA /Users/john/projects/journal/server/.env.example
# STRAVA_CLIENT_ID=
# STRAVA_CLIENT_SECRET=
# STRAVA_REDIRECT_URI=http://localhost:8400/strava/callback
```

Three lines, none of them `STRAVA_REFRESH_TOKEN`. No change needed there.

## What still needs to happen (operator)

Prod `.env` removal is the user's call — not the engineering team's. From
the project root on the prod VM:

```bash
# After SSH'ing into the prod host. Inspect first.
grep STRAVA_REFRESH_TOKEN .env

# Remove the line if present, then redeploy.
sed -i.bak '/^STRAVA_REFRESH_TOKEN=/d' .env
docker compose up -d
```

The `.bak` keeps a rollback for one redeploy. Nothing in the running
server consults the variable, so removing it has no behavioral impact —
this is purely env hygiene to reduce the surface area of secrets in the
deploy environment.

## Closeout

This entry plus the operator step above completes W4 of the fitness
multi-user final-mile work documented in
`.engineering-team/runs/manual-2026-06-03-fitness-multiuser/improvement-plan.md`.
No source or test changes were needed — the W6 work in May 2026 already
removed every read site; this run is the formal sign-off.

## 2026-06-10 closure

2026-06-10: removed from prod `.env` (backup `.env.bak-260610`), container
recreated healthy; old token revocation in the Strava UI pending. The
load-bearing operator note in `docs/fitness-operations.md` §1 was removed
the same day.
