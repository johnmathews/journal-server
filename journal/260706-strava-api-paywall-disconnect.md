# 1. Strava API subscriber-only cutover → disconnect, rely on Garmin

**Date:** 2026-07-06

## 1.1 What happened

The daily Strava sync had failed on every run since **2026-07-01**, logging
"failed transiently — will retry on next run" ~once/day. It was not transient.
Prod (`media`) logs showed the real error:

```
403 Client Error: Forbidden
[Forbidden: [{'resource': 'Application', 'field': 'Status', 'code': 'Inactive'}]]
```

This is Strava's **subscriber-only API cutover**: as of **2026-06-30**,
Standard-tier API access (what our single-athlete app uses) requires an active
Strava subscription (~$11.99/mo). The API application was deactivated on the
cutover date. There is no longer a free tier; the "3-months free" grace for
existing developers was scoped to before 2026-06-30 and has expired. The
Extended Access Tier (subscription-exempt) is for multi-user apps only.

## 1.2 Two distinct problems

1. **Operational:** Strava now requires payment for API access.
2. **Code defect:** the 403 was misclassified as transient and retried
   silently for 5 days with no user-facing signal.

Root cause of the defect: `stravalib` maps only HTTP **401** to
`AccessUnauthorized`; a **403** surfaces as a bare `stravalib.exc.Fault`. The
provider only caught `(AccessUnauthorized, AuthError)`, so the 403 escaped
untranslated into `services/fitness/fetch.py`'s `except Exception` catch-all,
which buckets everything unknown as `transient_failure`.

## 1.3 Decision — disconnect Strava, do not subscribe

Prod data showed **Garmin already covers every Strava activity**: 150 vs 140
activities, current through 2026-07-05, and **zero** days where Strava had an
activity Garmin lacked. Activities originate on a Garmin device and flow to
both Garmin Connect (synced directly) and Strava (a redundant mirror). Paying
for Strava buys nothing the journal doesn't already have via Garmin.

Action taken: deleted the `fitness_auth_state` row for `(user_id=1,
source='strava')` on prod (equivalent to the `POST /api/fitness/strava/
disconnect` endpoint → `delete_auth_state`). Historical `fitness_activities`
rows are preserved; reconnect is possible later via OAuth if Strava's terms
change. The scheduler enqueues only sources returned by
`list_users_with_active_auth`, so Strava is now never synced.

## 1.4 Code fix (PR #57, merged + deployed)

`providers/strava.py`: a 403 `Fault` now translates to `StravaAuthError` at all
three call sites (`list_activities`, `get_activity_detail`,
`refresh_token_if_needed`) via `_is_forbidden_fault`. It flows
`StravaAuthError → FitnessAuthError → auth_broken → notify-once` and retries
stop. Non-403 faults (429, 5xx, network) still propagate as plain `Fault`s and
stay transient. Tests: 403→`StravaAuthError` for all three methods plus a
5xx-stays-transient guard (failing-test-first). Deployed to prod
(image `0bce1fd9…`), fix confirmed present in the running container.

## 1.5 Free-data note

Strava's free, non-API bulk export (Settings → My Account → *Download your
data*, FIT/GPX + CSV) remains available for a one-off backfill if ever needed —
but it can't drive automated sync. Garmin remains the source of truth.
