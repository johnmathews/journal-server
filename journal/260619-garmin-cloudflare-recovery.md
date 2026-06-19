# 260619 — Garmin auth: Cloudflare block diagnosis, mislabel fix, split-IP recovery

## Symptom

Garmin daily sync had been failing since ~06-15 with `auth_status="broken"`
("Garmin authorization is broken — please re-authorize"). Every manual
reconnect over 06-15 → 06-18 also failed, and the webapp/endpoint reported
**"invalid credentials" (401)** — which sent us chasing a password problem
that didn't exist.

## Root cause (from prod Loki logs)

The reconnects were **not** credential failures. garminconnect's 5-strategy
login chain was hitting Garmin's Cloudflare bot defenses on the server's
egress IP:

```
mobile+cffi returned 429: … IP rate limited by Garmin
mobile+requests returned 429: …
widget+cffi failed: unexpected title 'GARMIN Authentication Application' — Cloudflare rate limiting
Portal login: waiting 19s …
→ POST /api/fitness/garmin/connect — invalid credentials for user_id=1 → 401
```

Two findings:

1. **The IP was Cloudflare-flagged**, escalated by the burst of failed
   logins itself (each retry re-arms the block). A browser on the same
   public IP still worked because it solves the JS/managed challenge that a
   script can't. Normal daily sync was never the trigger — it boots from the
   stored `tokens_blob` (`providers/garmin.py:169`) and does **no** SSO
   login; once `broken` it bails without a network call
   (`fetch.py` started-broken guard).
2. **The endpoint mislabeled the failure.** garminconnect's chain swallows
   the 429s as warnings and, when the portal strategy misreads the
   Cloudflare interstitial, raises a generic
   `GarminConnectAuthenticationError("401 Unauthorized (Invalid Username or
   Password)")`. The connect handler blindly mapped that to
   `invalid_credentials`. The terminal message is indistinguishable from a
   real bad password — but the mid-login 429/challenge **warnings** are not.

## Changes

### 1. Mislabel fix — `api/fitness_garmin.py`

- Added `_capture_garmin_logs()` — a context manager that tees the
  `garminconnect` logger's WARNING+ records during `client.login()`.
- Added `_looks_rate_limited(*texts)` matching rate-limit/challenge signals
  ("429", "cloudflare", "unexpected title", "strategies exhausted", …)
  against both the exception text and the captured warnings.
- `garmin_connect` now reclassifies a `GarminConnectAuthenticationError`
  (and the terminal `GarminConnectConnectionError` "all strategies
  exhausted", previously a generic 502) as a **429 `upstream_rate_limited`**
  when the attempt shows rate-limit/challenge signals. A genuine bad
  password (no such signals) still returns 401 `invalid_credentials`.
- Tests: `test_connect_cloudflare_block_reclassified_from_invalid_credentials`,
  `test_connect_all_strategies_exhausted_is_rate_limited_not_502`,
  `test_connect_genuine_bad_password_still_401`. The fake Garmin gained a
  `login_logs` hook to replay the chain's warnings.

### 2. Split-IP recovery — `cli/fitness.py`

Two new CLI commands so the network login can run off the flagged IP:

- `fitness-garmin-mint-token --username … [--output -|FILE]` — logs in
  (MFA from stdin), prints a portable JSON envelope
  (`source / upstream_user_id / tokens_blob / minted_at`). **No DB access**,
  runs anywhere.
- `fitness-garmin-import-token --user-id N [--input -|FILE]` — validates the
  blob loads into the SDK (offline), warns on D8 account mismatch, upserts
  `fitness_auth_state` with `auth_status="ok"`. **No network login.**

A `garth` OAuth1 token is valid ~1 year, so one mint+import keeps the daily
sync running for months without the server ever doing a fresh SSO login.
Documented in `docs/fitness-operations.md` §2c-bis (+ §6 cross-ref). The
`/done` doc-freshness audit also caught a stale claim in `docs/api.md` (the
connect endpoint's error table still tied every `GarminConnectAuthenticationError`
to `401 invalid_credentials`); updated it to document the two `429` reasons
(`local_cooldown` vs `upstream_rate_limited`) and the reclassification.

## Verification

`2946 passed` (full unit suite), ruff clean on all touched files.

## Follow-up worth considering

- The webapp Settings panel should render `reason: "upstream_rate_limited"`
  with the "stop retrying, wait" guidance (it previously only had to handle
  `invalid_credentials`).
- The connect endpoint could refuse to even attempt a login while the local
  cooldown is hot for the IP (not just per-email), to stop re-arming the
  Cloudflare block from the UI.
