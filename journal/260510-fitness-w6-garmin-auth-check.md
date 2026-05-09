# 2026-05-10 — Fitness W6: Garmin auth check missed `tokens_blob`

**Found during the W13 live smoke.** The first Garmin backfill window
short-circuited to `auth_broken` with `error_class=MissingAuthState`
even though the W11 CLI re-auth had successfully persisted a
`tokens_blob` row to `fitness_auth_state` minutes earlier. Diagnosis:
W6's missing-credentials guard only looked at `access_token`, but
W11 introduced Garmin's blob-based auth pattern where `access_token`
stays `None` and the live credential lives in
`extra_state["tokens_blob"]`. Result: every Garmin sync hit the
guard regardless of whether re-auth had succeeded.

The bug pre-dates W13 — it's been latent since W11 merged. The
smoke is what exposed it because no test exercised the W11 reauth
*shape* end-to-end against the W6 fetch service. The existing
Garmin tests in `test_fetch.py` used a `_seed_auth` helper that
populated `access_token="atok"` for both sources, so the buggy check
happened to be satisfied — the test was inadvertently testing
against the wrong auth shape.

## Fix

`_FetchServiceBase` gains a `_has_credentials(self, auth)` hook
that defaults to `bool(auth.access_token)` (the OAuth pattern Strava
uses). `GarminFetchService` overrides it to return
`bool(auth.extra_state and auth.extra_state.get("tokens_blob"))`.
The guard at the top of `run_sync` now consults the hook instead of
hard-coding the column.

This decouples the credential check from a fixed column. A future
source with yet another auth shape (e.g. mTLS certs, signed JWTs)
can override the hook without reaching back into `run_sync`.

## Tests

Two new tests in `test_fetch.py`:

1. `test_garmin_run_sync_proceeds_when_only_tokens_blob_is_set` —
   seeds a Garmin auth row in the *real W11 shape*
   (`access_token=None`, `extra_state={"tokens_blob": "..."}`) and
   asserts `run_sync` does not short-circuit to `MissingAuthState`.
   This is the regression test for the smoke-found bug.
2. `test_strava_run_sync_still_requires_access_token` — seeds a
   Strava row with `access_token=None` and asserts MissingAuthState
   *does* fire. Pins the OAuth-still-requires-the-token behaviour
   so the fix doesn't over-rotate the check.

Both `_seed_auth` helpers (in `test_fetch.py` and
`test_backfill.py`) updated to mirror what each source's W11 CLI
actually persists: Strava gets the OAuth triple, Garmin gets the
blob in `extra_state`. Four existing Garmin tests were depending on
the *buggy* shape; updating the helper fixed them in one place
(rather than touching each test's assertion).

## Test results

`uv run pytest -m "not integration"`: **2131 passed**, 8 deselected.
Up 2 from the W13 baseline (2129 → 2131). Ruff clean.

## Notes for the W13 smoke journal entry

This is finding #1 of the W13 live smoke. After this fix is on
`main` and the prod image is pulled, Garmin backfill should proceed
without re-auth (the auth_state row from the W11 reauth is still
valid; only the W6 check was wrong, and the auth-broken sync runs
recorded by the buggy attempts didn't transition `auth_status` to
`broken`).

Other W6/W11 deployment-shape findings from the smoke that are
*not* code bugs — namely the OAuth listener / running-server port
collision, and the headless-VM browser-on-laptop case — go into
`260510-fitness-first-fetch.md` as docs/W14 follow-ups, not here.
