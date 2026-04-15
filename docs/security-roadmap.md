# Security Hardening Roadmap

**Created:** 2026-04-14
**Last updated:** 2026-04-15

Prioritized security improvements for the multi-user authentication system. Items are grouped
into tiers based on urgency. Each tier should be completed before the one below it.

## Current State

The auth system uses Argon2id password hashing, server-side SQLite sessions with httpOnly/Secure/SameSite=Lax
cookies, per-user API keys (SHA-256 hashed), account lockout (5 attempts / 15 min), email verification,
and itsdangerous signed tokens for password reset. Deployed behind Cloudflare + Traefik reverse proxy.

Security audit performed 2026-04-14. Fixes already applied:
- Session revocation on password reset
- HTML escaping in email templates
- Password max length enforcement (1024 chars)
- Conditional lockout via repository abstraction (thread-safe)
- Repository abstraction bypass fixed in `_maybe_lock_after_failure`

---

## Tier 1 — Critical (do before inviting users) — COMPLETED 2026-04-15

### 1. ~~Complete per-user data isolation on all read queries~~ DONE

~~The single most important gap.~~ `create_entry` and `create_entity` pass `user_id`, but every read
query (`list_entries`, `get_entry`, `search_text`, `get_statistics`, `get_mood_trends`, `delete_entry`,
etc.) has no `WHERE user_id = ?` filter. Any authenticated user can read/modify/delete every other
user's data.

**Work required:**
- Add `user_id` parameter to every method on `EntryRepository` Protocol
- Add `user_id` parameter to every method on `EntityStore` Protocol
- Add `user_id` to `JobRepository.list_jobs` (admin bypass for `user_id=None`)
- Update all SQL queries with `AND user_id = ?` (or `AND e.user_id = ?` for JOINs)
- Update `QueryService` to pass `user_id` to both repository and ChromaDB `where` filter
- Update every route handler in `api.py` to call `get_authenticated_user()` and pass `user_id`
- Update every MCP tool in `mcp_server.py` to extract user from context
- Write integration tests: create 2 users, verify complete data separation

### 2. ~~Hash session tokens before storage~~ DONE

~~Session tokens are stored in plaintext in `user_sessions.id`.~~ If the SQLite file is ever exposed
(backup leak, file access), an attacker can impersonate any active user.

**Work required:**
- In `create_session`: store `sha256(token)` as the session ID, return the raw token to the client
- In `get_session`: hash the incoming token before the DB lookup
- In `delete_session` / `delete_user_sessions`: hash before delete
- In `update_session_last_seen`: hash before update
- Mirrors the API key pattern already in use

---

## Tier 2 — High (do before exposing to the internet long-term)

### 3. Add app-level rate limiting on auth endpoints

Traefik handles network-level rate limiting, but app-level rate limiting adds defense-in-depth.

**Targets:**
- `/api/auth/login`: max 10 attempts per IP per minute
- `/api/auth/register`: max 3 registrations per IP per hour
- `/api/auth/forgot-password`: max 5 per IP per hour (prevents email bombing)

**Options:** `slowapi` library (wraps `limits` for Starlette), or a simple in-memory counter with
TTL. For ~10 users, an in-memory dict is sufficient.

### 4. Mask email existence on registration

Currently `/api/auth/register` returns `"Email already registered"` which enables email enumeration.
Change to a generic error or return success (with a "check your email" flow) regardless of whether
the email exists — same pattern as forgot-password.

### 5. Invalidate password reset tokens after use

`itsdangerous` tokens are stateless — the same reset token can be used multiple times within the
30-minute window.

**Simplest approach:** Add `password_changed_at TEXT` to `users`, set it on password reset/change.
In `reset_password`, after validating the itsdangerous token, check that the token was issued after
`password_changed_at`. Since itsdangerous includes the timestamp in the token, this is a one-line check.

### 6. Enforce password complexity beyond length

Current: 8-1024 characters, no other constraints.

**Add:**
- Must contain at least one letter and one digit (blocks "12345678" and "aaaaaaaa")
- Check against a small blocklist of the top 100 most common passwords

NIST SP 800-63B recommends length over complexity rules. A blocklist check is more effective than
uppercase/symbol requirements.

---

## Tier 3 — Medium (good practice, do when convenient)

### 7. Add `Secure` cookie flag toggle for local development

`set_session_cookie` hardcodes `secure=True`, which breaks local HTTP development. Add a config flag
(`COOKIE_SECURE=true` by default) that can be set to `false` for dev.

### 8. Add Content Security Policy headers

Add CSP headers to the nginx config in journal-webapp:
```
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'
```

### 9. Add session activity logging

Log session creation, validation, and revocation with IP + user agent. Creates an audit trail for
investigating suspicious access. The `user_sessions` table already stores `ip_address` and
`user_agent` — add structured logging or a `session_events` table.

### 10. Add API key scoping

Currently API keys have full access. Add optional scopes (e.g., `read`, `write`, `admin`) so users
can create read-only keys for less-trusted integrations.

### 11. Add account deletion (GDPR-style)

Allow users to delete their own account and all associated data (entries, entities, vectors, sessions,
API keys). Admin can also delete users from the admin panel.

---

## Tier 4 — Low (nice to have)

### 12. Add two-factor authentication (TOTP)

Optional TOTP via an authenticator app (Google Authenticator, Authy). Adds a `totp_secret` column
to users and a verification step after password auth. Library: `pyotp`.

### 13. Add login notification emails

Send an email when a login occurs from a new IP or user agent. Lets users detect unauthorized access.

### 14. Periodic session cleanup cron

The `cleanup_expired_sessions()` method exists but is never called. Add a periodic task (e.g., on
server startup + every hour) to purge expired sessions from the database.

### 15. Security headers middleware

Add `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy` to all responses.
