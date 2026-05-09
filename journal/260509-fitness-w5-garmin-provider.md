# W5 — Garmin provider (Protocol + garminconnect adapter)

**Date:** 2026-05-09. **Plan:** [docs/fitness-tier-plan.md](../docs/fitness-tier-plan.md) §W5.

## What shipped

1. **`src/journal/providers/garmin.py`** — `GarminDailyMetrics` and
   `GarminActivitySummary` dataclasses, `GarminProvider` Protocol,
   `GarminConnectGarminProvider` adapter wrapping `garminconnect.Garmin` with an
   injected `persist_tokens: Callable[[str], None]` callback, plus a typed
   `GarminAuthError` for 401/403 propagation. Zero direct DB or HTTP-library
   imports beyond `garminconnect` itself.
2. **Hand-crafted fixtures** — one JSON file per Garmin endpoint
   (`sleep`, `hrv`, `body_battery`, `stress`, `training_status`,
   `training_readiness`, `list_activities_response`) under
   `tests/test_providers/fixtures/garmin/` with a sibling `README.md` carrying
   the `FIXTURE SOURCE: hand-crafted; replace at W13` marker and a
   field-extraction contract table mapping each adapter field to its source
   payload path.
3. **`tests/test_providers/test_garmin.py`** — 22 tests covering the four
   plan-required scenarios (replay daily aggregation, partial-data resilience,
   MFA callback wiring, verbatim `activity_type_str`) plus the D4 token-loading
   sequence (DB blob → filesystem cache → password), `persist_tokens` mirroring
   after a network login, completely-empty-payload tolerance, JSON-safety on
   `raw_payload`, Protocol conformance, list-activities replay, and 401/403 →
   `GarminAuthError` translation on login, daily, and activity paths.
4. **`pyproject.toml` / `uv.lock`** — `garminconnect==0.3.3` (exact pin per
   plan acceptance criteria).

## Decisions worth recording

1. **DB blob is fed straight to `garmin.client.loads`**, not via
   `garmin.login(tokenstore=blob)`. The SDK's tokenstore arg has a
   length-based heuristic (`len > 512` → treat as JSON string; else → treat
   as filesystem path), and our blob shape happens to clear that threshold in
   practice but isn't guaranteed to. Going through `client.loads` directly
   sidesteps the heuristic and makes the D4 "DB first" path explicit in the
   adapter, not implicit in SDK internals.
2. **`mfa_callback` is wired via the SDK's `prompt_mfa` constructor arg**,
   not via the adapter's own retry loop. The plan's test stubs
   `garminconnect.Garmin.login` (the published seam) rather than `garth.login`,
   so the adapter constructs `Garmin(prompt_mfa=mfa_callback)` lazily in
   `login()` and lets the SDK invoke the callback on its 2FA path. Each call
   to `login()` constructs a fresh `Garmin` so a CLI re-auth (W11) can swap
   the callback without rebuilding the provider.
3. **`persist_tokens` only fires on the password path**, not on the DB-blob
   path. If the blob loaded cleanly, the DB row is already canonical — there
   is nothing new to write back. Confirmed by
   `test_login_does_not_persist_when_blob_already_valid`. The fetch service
   (W6) doesn't need to deduplicate identical writes.
4. **Field-extraction contract is documented in
   `fixtures/garmin/README.md`**, not just inferred from tests. The
   hand-crafted fixtures are a precise statement of "what the adapter
   reads from each endpoint"; W13's fixture replacement will swap shapes,
   not the contract, so a centralised mapping table is the artefact future
   work will reach for first.
5. **Auth-error translation lives at the SDK boundary, not at the public
   API surface**, via a small `_call(fn, date)` helper for the per-day
   getters and inline `try/except` on `login` and `list_activities`. Catches
   the failure where it happens (so the stack trace points at the offending
   endpoint) while keeping the typed `GarminAuthError` contract uniform
   across all three protocol methods.

## Pinned

- `garminconnect==0.3.3` (exact). The SDK uses `garth` transitively for the
  underlying SSO flow but our adapter only touches `garminconnect`'s public
  surface (`Garmin`, `GarminConnectAuthenticationError`, `client.loads`,
  `client.dumps`).

## What's not done yet

1. **Fixtures are hand-crafted** — flagged in the sibling `README.md` and
   will be replaced at W13 (first live smoke test) with real anonymised
   responses. Tests that fail after that swap are real bugs, not flakiness
   — the contract table in the README pins what the adapter reads, so
   shape divergence will surface as a real assertion failure.
2. **No live login attempt was made** during W5. Acceptance criteria are
   pure-unit; live SSO + paid-feature endpoint coverage is W13's scope.
   The Garmin creds are present in `.env` (no MFA per current account
   state) but the fetch service (W6) is what wires them through to the
   provider.
3. **State-dir defaulting for `tokens_path`** is deferred to W11 (CLI
   re-auth + first-run flow). The adapter accepts `tokens_path: Path |
   None` as a constructor arg; the per-user default path under
   `<state_dir>/garmin_tokens/` is the fetch/CLI layer's job to compute.

## Tests

- 1968 passed (1946 prior baseline + 22 new), 0 failed.
- Lint clean (ruff). One TC005 + one TC003 surfaced and were fixed by moving
  `Path` into `TYPE_CHECKING`.
- `garminconnect==0.3.3` resolved cleanly with the existing dependency set;
  brought in `curl-cffi` and `ua-generator` as transitives.
