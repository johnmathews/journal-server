# Security Hardening Roadmap

**Status:** active. **Last updated:** 2026-05-09 (post-audit pass; statuses re-verified against
`src/journal/services/auth.py`, `src/journal/auth_api/`, `src/journal/auth.py`, and prod ground
truth on `media`).
**Supersedes:** none. Created 2026-04-14. Tier 1 completed 2026-04-15. Tier 2 partially shipped
(see per-item status). Tiers 3 and 4 remain.
**Scope:** single-VM home server, two users (per prod), no public internet exposure beyond a
Cloudflare Tunnel that fronts the webapp on `:8402`. Items that imply enterprise scale
(SSO, fine-grained RBAC, SOC2 controls) are explicitly out of scope and not enumerated here.

Prioritized security improvements for the multi-user authentication system. Items are grouped
into tiers based on urgency. Each tier should be completed before the one below it.

## Current State (2026-05-09)

The auth system uses Argon2id password hashing (argon2-cffi `PasswordHasher()` defaults:
m=65536 KiB, t=3, p=4 — exceeds the OWASP 2025/2026 minimum of m=19456, t=2, p=1), server-side
SQLite sessions with httpOnly/Secure/SameSite=Lax cookies, per-user API keys (SHA-256 hashed,
prefix-displayed), session token hashing (SHA-256), account lockout (5 attempts / 15 min), email
verification, and `itsdangerous` signed tokens for password reset (30 min) and verification (24 h).
Deployed behind a Cloudflare Tunnel that fronts the webapp on `:8402`; the webapp proxies `/api/*`
and `/mcp` to `journal-server` over the compose-internal network.

Security audit performed 2026-04-14. Fixes already applied:
- Session revocation on password reset (`AuthService.reset_password` calls `delete_user_sessions`).
- HTML escaping in email templates.
- Password max length enforcement (1024 chars).
- Conditional lockout via repository abstraction (thread-safe).
- Repository abstraction bypass fixed in `_maybe_lock_after_failure`.

References:
- [`security.md`](security.md) — current posture, threat model, defences in place.
- [`auth.md`](auth.md) — auth architecture and flows.
- [`archive/audit-2026-05-09.md`](archive/audit-2026-05-09.md) — most recent documentation
  accuracy audit.
- [`archive/tier-1-plan.md`](archive/tier-1-plan.md) — closed Tier 1 implementation plan.

---

## Tier 1 — Critical (do before inviting users) — COMPLETED 2026-04-15

### 1. ~~Complete per-user data isolation on all read queries~~ DONE

~~The single most important gap.~~ `create_entry` and `create_entity` pass `user_id`, but every read
query (`list_entries`, `get_entry`, `search_text`, `get_statistics`, `get_mood_trends`, `delete_entry`,
etc.) has no `WHERE user_id = ?` filter. Any authenticated user can read/modify/delete every other
user's data.

**Verification 2026-05-09:** `EntryRepository`, `EntityStore`, and ChromaDB calls all take
`user_id`; admin bypass on `JobRepository.list_jobs` is in place; integration tests under
`tests/test_isolation/` pass.

### 2. ~~Hash session tokens before storage~~ DONE

~~Session tokens are stored in plaintext in `user_sessions.id`.~~ If the SQLite file is ever exposed
(backup leak, file access), an attacker can impersonate any active user.

**Verification 2026-05-09:** `AuthService._hash_session_token` (SHA-256) is applied in
`create_session`, `validate_session`, `logout`, and the user-repo session paths. Mirrors the
API-key pattern.

---

## Tier 2 — High (do before exposing to the internet long-term)

### 2b. Bring prod compose back to loopback bind for `journal-server` — OPEN

The repo's `compose.yml` pins `journal-server` host port to `127.0.0.1:8400`, but the prod compose
on `media` (`/srv/media/docker-compose.yml`) currently exposes `0.0.0.0:8400` (LAN-reachable).
Public access already goes through the Cloudflare Tunnel that fronts `:8402`, so the LAN-exposed
`:8400` adds attack surface without operational benefit. Prod ground truth (2026-05-09) shows
`MCP_ALLOWED_HOSTS=192.168.2.105:*,localhost:*`, which is consistent with the LAN-exposed bind
(the rebinding allowlist would be `localhost:*` only if the bind were loopback-only).

**Action:** edit the prod compose to `127.0.0.1:8400:8400`, `docker compose up -d`, then narrow
`MCP_ALLOWED_HOSTS` to `localhost:*` (or remove the env var to fall back to the loopback default
in `config.py`). No webapp impact (it proxies through the compose network, not the host port).

### 3. Add app-level rate limiting on auth endpoints — OPEN

Cloudflare provides edge rate limiting for the public hostname; Traefik is no longer in the path
(superseded by the Cloudflare Tunnel + nginx in `journal-webapp`). App-level rate limiting on
auth endpoints adds defence-in-depth against an attacker who bypasses Cloudflare (e.g. by reaching
the LAN-exposed `:8400` directly, until 2b lands).

**Verification 2026-05-09:** no rate-limiting library or in-memory counter exists in
`src/journal/` — `grep -rn 'slowapi\|rate.?limit'` returns nothing.

**Targets:**
- `/api/auth/login`: max 10 attempts per IP per minute.
- `/api/auth/register`: max 3 registrations per IP per hour.
- `/api/auth/forgot-password`: max 5 per IP per hour (prevents email bombing).

**Options:** `slowapi` library (wraps `limits` for Starlette), or a simple in-memory counter with
TTL. For two users, an in-memory dict is sufficient and avoids a Redis dependency.

### 4. Mask email existence on registration — OPEN

`/api/auth/register` returns `"Email already registered"` (`auth_api/account.py` line ~104,
`services/auth.py` line ~64) which enables email enumeration. Forgot-password is already masked.

**Action:** either return a generic 400 (`"Registration could not be completed"`), or accept the
request and silently send a "you already have an account" email to the existing address. The
latter matches the forgot-password flow and is harder for an attacker to distinguish.

### 5. Invalidate password reset tokens after use — OPEN

`itsdangerous` tokens are stateless — the same reset token can be used multiple times within the
30-minute window.

**Verification 2026-05-09:** the `users` table has no `password_changed_at` column (migrations
through `0022_entity_merge_candidates_pair_unique.sql`). `AuthService.reset_password` does revoke
all sessions but does not bind the token to a single use.

**Simplest approach:** Add `password_changed_at TEXT` to `users` (new migration), set it on
password reset/change. In `reset_password`, after validating the `itsdangerous` token, decode the
embedded timestamp and reject if it predates `password_changed_at`. One-line check.

### 6. Enforce password complexity beyond length — OPEN

Current: 8–1024 characters, no other constraints (`auth_api/account.py` lines 90–97 and 249–256).

**Recommended (NIST SP 800-63B-aligned):**
- Reject any password that appears in a small blocklist of the top 100 most common passwords
  (e.g. SecLists `10-million-password-list-top-100.txt`).
- Optionally reject if the password is the user's email local-part or display name.
- **Do not** add uppercase/symbol composition rules — NIST 800-63B explicitly recommends against
  them.

NIST SP 800-63B Rev. 4 (April 2024) recommends length over composition rules and explicitly
endorses checking against breach corpora. A blocklist check is more effective than
uppercase/symbol requirements.

### 7. ZDR (Zero Data Retention) headers for provider calls — NEW (2026-05-09)

`security.md` notes that journal content is sent to Anthropic, OpenAI, and Google for OCR,
transcription, embeddings, mood scoring, and search reranking — and that abuse-monitoring
retention (~30 days) applies on all three. ZDR is available on enterprise plans for Anthropic
and OpenAI; `journal-server` does not currently send any ZDR headers.

**Action (policy + code):** decide whether to apply for ZDR with Anthropic and OpenAI; if yes,
add the org-level header to the SDK clients in `providers/extraction.py` (entity extraction),
`services/mood_scoring.py`, `providers/transcription.py`, `providers/ocr.py`, and any other
provider modules that instantiate Anthropic/OpenAI SDK clients. This is low-effort once the
org-level entitlement is in place.

---

## Tier 3 — Medium (good practice, do when convenient)

### 8. Add `Secure` cookie flag toggle for local development — OPEN

`set_session_cookie` in `src/journal/auth.py` hardcodes `secure=True, samesite="lax"`, which
breaks local HTTP development on `http://localhost`. Add a config flag (`COOKIE_SECURE=true` by
default) that can be set to `false` for dev. SameSite=Lax remains the right default for the
primary session cookie (see OWASP Session Management Cheat Sheet 2025); promoting to
SameSite=Strict is a separate, lower-priority hardening step that requires UX testing of the
"open journal link from another tab" flow.

### 9. Add Content Security Policy headers — OPEN

CSP headers are not currently emitted by `journal-server` or the `journal-webapp` nginx config.
Add to the nginx config in `journal-webapp`:
```
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'
```
Audit the webapp build first — a stricter `script-src` (no `'unsafe-inline'`) is achievable if
the bundler emits hashes for any inline scripts.

### 10. Add session activity logging — OPEN

Log session creation, validation, and revocation with IP + user agent. Creates an audit trail
for investigating suspicious access. The `user_sessions` table already stores `ip_address` and
`user_agent` — add structured logging on the existing call sites in `services/auth.py`, or a
`session_events` table if persistence is wanted. For two users, log-only is probably sufficient.

### 11. Add API key scoping — OPEN

Currently API keys have full access (no scope column on `api_keys`). Add optional scopes (e.g.
`read`, `write`, `admin`) so users can create read-only keys for less-trusted integrations
(MCP clients in agents that don't need to mutate data). Schema change + middleware enforcement
in `auth.py`'s `SessionOrKeyBackend`.

### 12. Add account deletion (GDPR-style self-service) — OPEN

Allow users to delete their own account and all associated data (entries, entities, vectors,
sessions, API keys). Admin can already disable users (`PATCH /api/admin/users/{id}`); cascade
delete is the missing piece. ChromaDB needs a separate deletion call keyed on `user_id`
metadata. Two-user home server makes this lower-priority but still good hygiene.

### 13. Periodic session cleanup — OPEN

`UserRepository.cleanup_expired_sessions()` is implemented (line ~318) and tested
(`tests/test_db/test_user_repository.py:272`) but never called outside tests
(`grep` confirms no call sites in `src/`). Add a periodic task (server startup +
hourly thereafter) to purge expired sessions from the database. Cheapest fix: schedule it in
the existing job runner alongside `cleanup_old_jobs`.

### 14. Security headers middleware — OPEN

Add `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy: strict-origin-when-cross-origin`, and a minimal `Permissions-Policy`
(`camera=(), microphone=(), geolocation=()`) to all responses from `journal-server`. Pair with
#9 (CSP) so the webapp and server both emit a consistent header set.

### 15. Backup integrity and restore drill — NEW (2026-05-09)

`security.md` notes that backups are the user's responsibility and recommends an `age`/`gpg`
encrypted nightly `sqlite3 .backup`. There is no documented integrity check or restore drill.
For a single-VM deployment, the realistic failure mode is silent corruption of the bind-mount
volume.

**Action:**
- Add a `make verify-backup` target that runs `sqlite3 backup.db "PRAGMA integrity_check"` and
  decrypts the latest archive into a scratch dir.
- Document a quarterly restore drill in `production-deployment.md`: pull latest backup,
  restore into a throwaway compose stack, verify the `/health` endpoint reports the same
  entry count.
- Out of scope: ChromaDB doesn't need backup — vectors are recomputed on re-ingest from the
  source files in the bind mount; document this so the user doesn't try to back up Chroma's
  internal state.

---

## Tier 4 — Low (nice to have)

### 16. Add two-factor authentication (TOTP) — OPEN

Optional TOTP via an authenticator app (Google Authenticator, Authy). Adds a `totp_secret`
column to users and a verification step after password auth. Library: `pyotp`. For two users,
this is bordering on overkill given the Cloudflare Tunnel posture (and any Cloudflare Access /
Zero Trust policy that may be configured on the public hostname — verify before relying on it);
revisit only if the deployment grows or the tunnel posture changes.

### 17. Add login notification emails — OPEN

Send an email when a login occurs from a new IP or user agent. Lets users detect unauthorized
access. Cheap to implement (`user_sessions.ip_address` + `user_agent` already populated) and
useful even for two-user deployments.

---

## Out of scope (for this deployment)

Documented to prevent scope creep on future audits:

- Enterprise SSO (SAML, OIDC IdPs).
- Hardware-token MFA (WebAuthn / FIDO2). Could be moved into Tier 4 if user demand emerges.
- Per-tenant encryption keys (single-tenant deployment).
- HSM-backed `JOURNAL_SECRET_KEY` storage. Env-var loading from the host is appropriate at this
  scale; rotate with `docker compose up -d` after editing `.env`.
- SOC2/ISO27001 audit controls.
