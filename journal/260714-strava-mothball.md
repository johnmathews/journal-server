# Strava mothballed behind STRAVA_ENABLED (W1 of strava-mothball plan)

**Date:** 2026-07-14

## 1. Why

Strava paywalled Standard-tier API access behind an active Strava subscription
(~$11.99/mo) effective **2026-06-30**
([announcement](https://communityhub.strava.com/insider-journal-9/an-update-to-our-developer-program-13428)).
No subscription is held, so the journal's Strava app can no longer complete
OAuth or sync — the integration was already suspended in prose
(`journal/260713-fitness-w14-suspended.md`, roadmap D8), and Garmin already
covers 100% of activities. This session turned the prose suspension into an
enforced mothball: a single `STRAVA_ENABLED` env flag, default **false**.

## 2. What shipped (server W1)

`config.py` gains `strava_enabled: bool` from `STRAVA_ENABLED` (default false,
usual boolean idiom). With the flag off:

1. **Bootstrap** — `strava_configured` in `mcp_server/bootstrap.py` ANDs the
   flag into the existing credential gate, so Strava stays unwired even when
   `STRAVA_CLIENT_ID`/`STRAVA_CLIENT_SECRET` are present. Startup log says
   "Garmin only — strava: disabled (mothballed, STRAVA_ENABLED=false)".
2. **OAuth routes** — the three `/api/fitness/strava/*` routes
   (`authorize_url`, `exchange`, `disconnect`) return `404`
   `{"error": "Strava integration is disabled on this server"}`. The guard is
   at request time (routes register at import, before config exists), but the
   observable behavior matches an unregistered route.
3. **Job-trigger routes** — `POST /api/fitness/sync/strava` and
   `POST /api/fitness/backfill/strava` return the same 404.
4. **MCP tools** — `fitness_trigger_sync` / `fitness_trigger_backfill` reject
   `source="strava"` with the same error string.
5. **Scheduler** — the daily `FitnessSyncScheduler` is constructed with
   `sources=("garmin",)`, so it never lists or submits Strava work.
6. **CLI** — `fitness-reauth-strava` exits 1; `fitness-sync` /
   `fitness-backfill` reject `--source strava` **and** `--source both` (the
   default) with "use `--source garmin`" rather than silently degrading.

The shared 404 body and `_strava_enabled()` helper live in `api/_shared.py`;
both helpers are fail-closed (missing config reads as disabled).

## 3. Decisions

1. **Code kept, not deleted.** Providers, workers, endpoints, tests, and the
   `stravalib` dependency all stay — this is a mothball, and revival is a
   one-flag change plus credentials. (Plan non-goal 1.)
2. **Historical data kept and served.** `fitness_raw_strava` and
   `fitness_activities` rows with `source='strava'` remain queryable through
   every read surface; read tools/routes are flag-independent.
3. **`GET /api/fitness/sync/status` keeps both source keys.** The webapp
   contract is unchanged — prod already serves Strava as "not connected", so
   no page-load errors and no client migration. (Plan decision 2.)
4. **Flag surfaced to the webapp** as `features.strava_enabled` in
   `GET /api/settings`, mirroring `mood_scoring` — but frozen config, not a
   runtime setting. The webapp fails closed until settings load (W2, see the
   webapp repo's `journal/260714-strava-mothball.md`).
5. **"both" is rejected, not degraded.** CLI `--source both` errors instead of
   quietly running Garmin only, so the operator gets an explicit signal.

## 4. Revival path

Subscribe to Strava (account that owns the API app) → set `STRAVA_ENABLED=true`
plus `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET` (and prod
`STRAVA_REDIRECT_URI`) → restart the server → Settings → Fitness → Connect
Strava. Documented in
[`docs/fitness-operations.md` § Reviving Strava](../docs/fitness-operations.md#reviving-strava).

## 5. Files touched (docs pass, W3)

- `docs/configuration.md` — `STRAVA_ENABLED` documented (default, gated
  surfaces, revival).
- `docs/fitness-operations.md` — banner now says "mothballed via
  STRAVA_ENABLED=false"; new "Reviving Strava" subsection; flag notes on §1,
  §2b, §2d, §3, §4.
- `docs/api.md` — 404-when-disabled on the Strava OAuth + sync/backfill
  routes and MCP trigger tools; `features.strava_enabled` noted on
  `GET /api/settings`.
- `docs/roadmap.md` — D8 updated: mothball implemented 2026-07-14.
- `docs/fitness-pipeline.md`, `docs/external-services.md`, `README.md` —
  accuracy notes (Strava dormant, pricing no longer free).
