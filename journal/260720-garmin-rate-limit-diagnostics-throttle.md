# Garmin sync: distinguish rate-limit from dead-session + throttle the burst

**Date:** 2026-07-20

## 1. Why

After enabling saved-credential unattended re-login
([260714](260714-garmin-credential-persistence.md)), the open question was
whether the user's recurring Garmin `auth_broken` events (every ~2–4 days, a
bare `API Error 401`, mostly self-healing next-day) are **dead sessions** (which
the re-login fixes) or **Cloudflare/IP rate-limiting** (which it can't — a fresh
login from the same blocked IP hits the same wall). The sync-run log couldn't
tell them apart: every failure was a bare `401 -` with an empty body.

Two problems surfaced while tracing the fetch path:

1. **The data-fetch path couldn't see a rate-limit.** `_call` / `list_activities`
   caught only `GarminConnectAuthenticationError` and wrapped it as
   `GarminAuthError`. A `GarminConnectTooManyRequestsError` (429) wasn't caught
   at all, and a Cloudflare 403 that the SDK surfaces *as* an auth error was
   **misfiled as `auth_broken`** — flipping auth to broken, firing a Pushover,
   and triggering a doomed unattended re-login. Rate-limit classification
   existed only on the *login* path.
2. **No throttle.** One synced day fires 7 endpoint calls back-to-back, and
   `_derive_window` grows the window to every missed day since the last success
   — so a multi-day outage fires a 30+ request burst, a plausible rate-limit
   trigger and a vicious cycle (failure → bigger window → bigger burst).

## 2. What shipped

**Diagnostics (Part A).** `providers/garmin.py` gained `describe_garmin_error`
(HTTP status parsed from the SDK message + `cf-ray` / `cf-mitigated` /
`Retry-After` read from the garth client's `last_resp`) and `_is_rate_limited`
(cf-ray alone is *not* a signal — Garmin fronts every response with Cloudflare;
only `cf-mitigated` / `Retry-After` / 429 / a textual signal counts). The
data-fetch path now catches 429 and reclassifies Cloudflare-blocked
401/403s as `GarminRateLimitError`, and every raised error carries the enriched
`[status=… cf-ray=…]` suffix. `fetch.py` gained an explicit
`except GarminRateLimitError` branch: records `transient_failure` with
`error_class=GarminRateLimitError`, arms the shared upstream cooldown, and does
**not** flip auth to broken or fire the auth-broken alert. Net: the next failure
is self-diagnosing — `[status=401 …]` (dead session) vs `[status=429 …]` /
`[status=403 cf-mitigated=…]` (rate-limit) — straight off the run row.

**Throttle (Part B).** New config `FITNESS_GARMIN_REQUEST_DELAY_S` (default
`2.0`s) sleeps between Garmin calls; `GarminConnectGarminProvider` gained
`request_delay_s` + an injectable `sleep_fn`, threaded from config at the two
sync-path factories (bootstrap + CLI). At 0.5 req/s even a 6-day catch-up is
~0.5 req/s, not a burst. Default is non-zero so prod gets it without any
compose/Ansible change.

## 3. Deferred (deliberately)

A hard **days-per-run cap** was cut from scope. The resume cursor is
`MAX(started_at)` of *successful* runs — the run's wall-clock start, not the
last *fetched* date — so a naive cap would mark success at `now` and skip the
un-fetched middle days (data loss). A safe cap needs a real per-source sync
cursor; the 2s throttle already de-risks the burst, so this waits for evidence.
Documented in `fitness-operations.md` §7.

## 4. Tests

TDD, failing-first: provider tests for the throttle (6 sleeps/day), 429 →
rate-limit, Cloudflare-403 → rate-limit, genuine-401 → auth (with `status=401`
in the message and cf-ray *not* triggering a misclassify); a fetch-service test
that a data-path `GarminRateLimitError` records `transient_failure`, arms the
cooldown, leaves `auth_status=ok`, and fires no auth-broken notification; config
default/validation tests. Full suite green (3271), ruff clean.
