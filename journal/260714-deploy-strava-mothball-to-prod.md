# 1. Deploy: Strava mothball + Garmin credential persistence to production

**Date:** 2026-07-14 (evening)

## 1.1 Why

User reported still seeing Strava buttons in the webapp despite the Strava-mothball
work (engineering-team run `manual-20260714T054347Z`, work units W1–W8) being merged
and CI-green since ~08:35 UTC this morning. Triage confirmed the webapp source was
fully gated on `features.strava_enabled` — the cause was purely operational: prod on
`media` has no auto-update, and the running containers were built from **July 13**
images, one day before the mothball shipped. Restarting containers (done earlier in
the day) does not pull new images.

## 1.2 What was done

Followed `docs/production-deployment.md` § Deploy runbook:

1. Backed up the SQLite DB on `media` to
   `/srv/media/config/journal/data/journal.db.pre-deploy-20260714`.
2. `docker compose pull journal-server journal-webapp journal-chromadb && docker
   compose up -d` — all three containers recreated from today's images
   (webapp built 08:35 UTC, server 08:37 UTC).
3. Verified: containers up (chromadb healthy), migrations clean at version 37,
   server startup log reads *"Fitness sync wired (Garmin only — strava: disabled
   (mothballed, STRAVA_ENABLED=false); Strava routes 404)"*, webapp serving HTTP 200.

`STRAVA_ENABLED` is unset in `/srv/media/.env`, so the server default (`false`)
applies — no prod config change was needed.

## 1.3 Follow-up doc fix

The wrap-up docs audit found one staleness item W3's doc pass missed:
`docs/production-deployment.md` § "Renaming the public hostname" still told the
operator to update the Strava OAuth callback domain and verify by running the Strava
connect flow end-to-end — impossible while mothballed. The section now marks the
Strava steps as revival-only and points at `docs/fitness-operations.md` § Reviving
Strava.

## 1.4 Lesson

"Merged + CI green" is not "user-visible": this stack's images are pushed to ghcr by
CI but deployed only by a manual `compose pull` on `media`. When a UI change is the
deliverable, the deploy step belongs in the definition of done (roadmap already
flags Watchtower/SHA-pinning as a future robustness improvement).
