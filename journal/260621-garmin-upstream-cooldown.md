# 260621 — Garmin connect: global upstream cooldown (don't re-arm the Cloudflare block)

Follow-up to `260619-garmin-cloudflare-recovery.md`. That entry fixed the
*mislabeling* (Cloudflare/IP blocks were reported as `invalid_credentials`)
and added split-IP token recovery. It left two follow-ups open; this entry
closes the server one. (The webapp one — rendering `upstream_rate_limited` in
the Settings panel — shipped alongside in `journal-webapp`.)

## Problem

The connect endpoint had a per-email cool-down (`GarminCooldownTracker`):
after 5 failed attempts for one email within 15 min, further attempts for
**that email** are refused pre-flight. But the actual Cloudflare block is on
the **server's egress IP**, which is shared by every user and every email.
So the per-email tracker has a blind spot: once the IP is flagged, a connect
attempt for a *different* account sails straight through to garminconnect and
re-arms the block. The connect UI could keep deepening a block that was
already in place.

## Fix

New `GarminUpstreamCooldown` (`services/fitness/garmin_pending.py`): a single
global timestamp, account-agnostic.

- One observed block trips it (`record_block`) — unlike a mistyped password,
  there is no benign reason to retry into a live block, so the threshold is 1,
  not 5.
- `check()` returns remaining seconds (or `None` when clear); default block
  window is `DEFAULT_UPSTREAM_BLOCK_S = 5 min`, matching the
  `retry_after_seconds` we already advertise.
- A successful upstream contact (`login()` returns without a rate-limit —
  MFA challenge or straight success) clears it (`reset`).

Wired into `api/fitness_garmin.py`:

- Checked **first**, before the per-email cool-down and before any upstream
  call. When hot, return `429 upstream_rate_limited` with the real remaining
  time — no garminconnect call, so the block is not re-armed.
- `record_block()` on all three rate-limit detection branches: the
  reclassified `GarminConnectAuthenticationError`, the direct
  `GarminConnectTooManyRequestsError`, and the terminal "all strategies
  exhausted" `GarminConnectConnectionError`.
- `_garmin_rate_limited_response` now takes an optional `retry_after_seconds`
  (defaults to 300) so the pre-flight path can report the actual countdown.

Registered `garmin_upstream_cooldown` in `service_registry.ServicesDict` for
parity with the other two garmin services (lazy-created by the endpoint).

## Tests

- `test_garmin_pending.py`: unit coverage for the new class — single block
  trips, window expiry releases, reset clears, re-arm extends.
- `test_api_fitness_garmin_auth.py`:
  - `test_connect_upstream_block_refuses_other_accounts_preflight` — a block
    from account A refuses a *valid* account B pre-flight; asserts B's client
    factory was never even constructed (proves no upstream call).
  - `test_connect_success_leaves_upstream_gate_clear` — success path resets,
    doesn't arm.

`uv run ruff check` clean on touched files; full unit suite green.

## Docs

- `docs/api.md` — connect error table: `upstream_rate_limited` now documents
  the global pre-flight cooldown and the variable `retry_after_seconds`.
- `docs/fitness-operations.md` §2c-bis — operator note that the endpoint now
  self-enforces "stop retrying", and that split-IP import bypasses the gate
  (no network login).

Both 06-19 follow-ups are now closed.
