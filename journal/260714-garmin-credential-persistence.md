# Garmin credential persistence — encrypted saved credentials + unattended re-login (W4–W6)

**Date:** 2026-07-14

Server half of the strava-mothball / garmin-credentials plan (W4 foundation,
W5 capture/store/reconnect, W6 unattended re-login). Webapp counterpart:
journal-webapp W7 and its own `journal/260714-garmin-credential-persistence.md`.
Docs pass: W8.

## 1. Why

Garmin's garth token blob lasts roughly a year, then dies at API-call time —
the blob deserializes fine, but every endpoint 401s mid-sync. Until now that
meant `auth_broken`, a Pushover, and the user re-typing their password (plus
possibly an MFA code) in the webapp. The user's ask was "don't make me
re-enter my credentials". The accepted answer: store the password encrypted
at rest and use it for automatic recovery.

## 2. Design

1. **Fernet foundation (W4).** `services/fitness/credentials.py` — 
   `encrypt_credential` / `decrypt_credential` / `validate_credential_key`
   with two typed exceptions that deliberately split *config bug* from *data
   condition*: `CredentialKeyInvalid` (malformed `FITNESS_CREDENTIAL_KEY`,
   fail fast at startup via `Config.__post_init__`, error message includes
   the key-generation one-liner) vs `CredentialDecryptError` (rotated/lost
   key or garbage token — always caught, degrades to "credentials
   unavailable", never crashes a sync). Unset key = feature off, pre-feature
   behavior byte-for-byte.
2. **Encrypt at first touch (W5).** The connect handler encrypts the
   password while the plaintext is still in handler scope, before the
   existing `del password`. Only ciphertext reaches SQLite
   (`fitness_auth_state.extra_state_json.garmin_username` + `.enc_password`
   — no schema migration, the column is a JSON grab-bag). The CLI
   `fitness-reauth-garmin` does the same.
3. **Ciphertext-only pending sessions.** MFA splits login across two
   requests, so the credentials must survive the gap — the `PendingSession`
   carries `username` + `enc_password` (ciphertext only) and the pair is
   persisted only when the MFA actually completes. Plaintext never outlives
   the login call in either flow.
4. **One shared login path.** `connect` and the new
   `POST /api/fitness/garmin/reconnect` (no body) funnel into a single
   `_login_and_persist` helper: log-captured rate-limit disambiguation, both
   cooldown pre-flights, MFA pending-session issue, D8 account-mismatch
   guard, blob capture, auth-row upsert. Reconnect adds only the load +
   decrypt step in front (404 `no_saved_credentials`, 409
   `credentials_unavailable`).
5. **Retry-once seam in the fetch service (W6).** A dead blob fails at
   API-call time, not login time, so injecting credentials into the provider
   constructor alone is insufficient. On `GarminAuthError`,
   `GarminFetchService` makes **at most one** unattended password re-login
   per sync run (`_attempt_unattended_relogin`, returning
   `(attempted, recovered)`), persists the fresh blob via the existing
   `_persist` seam, and retries the fetch window once. Any failure — no
   creds, cooldown hot, rate-limit, MFA, bad password, or a second auth
   error post-recovery — degrades to the existing `auth_broken` flow, whose
   notification now states when automatic recovery was attempted.
6. **Shared-cooldown discovery.** While wiring the gate we found the connect
   UI and the fetch path each held *separate* `GarminUpstreamCooldown`
   instances — an unattended re-login could have re-armed a Cloudflare block
   the UI had already observed (or vice versa) without either noticing. W6
   makes it one instance serving both, and a rate-limited unattended login
   (`GarminRateLimitError`, deliberately *not* a `GarminAuthError` subclass)
   arms it for the UI too.
7. **No hangs on MFA.** `providers/garmin.py` guarantees that a login with
   `mfa_callback=None` raises a typed `GarminAuthError` instead of blocking
   on a prompt — the unattended path can never wedge a worker thread.
8. **Decrypt failure degrades to blob-only.** The provider factories
   (bootstrap + the CLI's duplicate) inject decrypted credentials when
   available; a `CredentialDecryptError` logs a warning and falls back to
   the empty-credential provider — exactly the pre-W6 behavior.

## 3. Security tradeoff

A **reversible** secret is stored, and that is accepted deliberately (plan
decision, evaluation §1.10.3). Threat model: Fernet protects the SQLite file
at rest — a copied `journal.db` (backup, stolen disk) must not leak the
Garmin password. The key lives in the `FITNESS_CREDENTIAL_KEY` env var
alongside the deployment's other secrets; an attacker with both the DB *and*
the process environment is out of scope — they already hold every API key.
OS-keychain/KMS integration was explicitly rejected as disproportionate for
this single-VM deployment (plan non-goal 4). The kill switch is unsetting
the env var: stored ciphertext goes inert, `credentials_saved` reads false,
and everything reverts to blob-only behavior with zero cleanup.

## 4. MFA limitation

Fully-unattended recovery is impossible when Garmin challenges with MFA. No
TOTP secret is stored (plan non-goal 3) — an MFA challenge during unattended
re-login is a typed failure that lands in the existing `auth_broken`
notification. What MFA accounts still gain: the one-click reconnect returns
the same `mfa_required` + pending-session shape as connect, so recovery
shrinks from "re-type email + password + code" to "type the 6-digit code".

## 5. Key lifecycle

- **Unset (default):** feature dark; no credential material ever written.
- **Malformed:** startup fails fast, error includes
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
- **Rotated:** old ciphertext undecryptable → `credentials_saved: false`,
  reconnect 409s, unattended re-login degrades to blob-only. Users reconnect
  once to re-save under the new key. Nothing crashes.
- **Lost:** identical to rotation — set a fresh key, users reconnect once.
  Plaintext is unrecoverable from ciphertext by design.

Runbook: `docs/fitness-operations.md` §6.

## 6. Surfaces touched

- `POST /api/fitness/garmin/reconnect` (new), `credentials_saved` on the
  garmin payload of `GET /api/fitness/sync/status` + MCP
  `fitness_sync_status` (true only when ciphertext exists *and* the current
  key decrypts it — "saved but unusable" deliberately reads as false).
- Disconnect already deleted the whole auth row, so saved credentials go
  with it — verified by test rather than new code.
- Docs (W8): `configuration.md`, `fitness-operations.md` §6 (new),
  `api.md`, `fitness-schema.md`, `production-deployment.md`, `.env.example`
  (shipped with W4).
