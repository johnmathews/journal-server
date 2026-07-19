# Garmin auto-auth was dark in prod — `FITNESS_CREDENTIAL_KEY` never reached the container

**Date:** 2026-07-20

## 1. Symptom

The webapp reported Garmin as not authenticated, and clicking reconnect showed
the full email+password form instead of the one-click reconnect. The unattended
re-login shipped in W6 (2026-07-14) was never firing.

## 2. Root cause

The saved-credentials / unattended-re-login feature is gated on
`FITNESS_CREDENTIAL_KEY`. Unset (from the app's view) = feature dark:
`_garmin_credentials_saved` returns false, so the webapp never offers the
one-click affordance and the fetch service never attempts an unattended
password re-login.

The enablement runbook (`docs/production-deployment.md` §"Optional secret")
has three steps: (1) generate key, (2) add to `/srv/media/.env`, (3) add
`- FITNESS_CREDENTIAL_KEY=${FITNESS_CREDENTIAL_KEY}` to the compose
`environment:` block + re-sync the versioned mirror. Steps 1–2 were done;
**step 3 was skipped.** Compose's root `.env` is only used for `${VAR}`
interpolation — it is not auto-injected into containers — so
`docker exec journal-server printenv FITNESS_CREDENTIAL_KEY` came back empty
even though the key was present in `.env`. The repo's versioned mirror
(`deploy/docker-compose.prod.yml`) was missing the line too, so the drift was
baked into the source of truth.

## 3. Fix

- Added `- FITNESS_CREDENTIAL_KEY=${FITNESS_CREDENTIAL_KEY}` to the
  `journal-server` environment block in `deploy/docker-compose.prod.yml`.
- Applied the same line to `/srv/media/docker-compose.yml` on the `media` VM
  and recreated `journal-server`. Verified the key is now present in the
  container and the server started clean (restarts=0 — a malformed key would
  fail fast at startup, so a clean boot also validates the key).

## 4. Follow-up (user action)

The key was never active, so no Garmin password ciphertext was ever written.
`credentials_saved` stays false until the user reconnects Garmin once via the
webapp — that capture-at-first-touch encrypts the password. From then on
re-auths are one-click and the background unattended re-login can run.
