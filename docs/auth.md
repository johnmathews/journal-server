# Authentication & Multi-User Architecture

**Status:** active reference doc. **Last reviewed:** 2026-05-09 against `src/journal/services/auth.py`,
`src/journal/auth.py`, and `src/journal/auth_api/`. See [`security.md`](security.md) for the
broader security posture and [`security-roadmap.md`](security-roadmap.md) for outstanding work.

The journal server supports multiple users with per-user data isolation. Two authentication
mechanisms are supported:

1. **Cookie sessions** — used by the web frontend (httpOnly, Secure, SameSite=Lax, 7-day max-age)
2. **API keys (bearer tokens)** — used by MCP clients and external API consumers

## User Model

Each user has:

- `email` — unique identifier (case-insensitive), used for login
- `display_name` — human-friendly name shown in the UI
- `password_hash` — Argon2id hash (NULL for social auth users, future)
- `is_admin` — admin flag (can manage other users)
- `is_active` — account enabled/disabled by admin
- `email_verified` — must be true before the user can access the app

## Registration Flow

1. User submits email, password, display_name to `POST /api/auth/register`
2. Server creates user with `email_verified=false`
3. Server sends verification email with a signed token (24h expiry)
4. User clicks link → `GET /api/auth/verify-email?token=...`
5. Server sets `email_verified=true`, user can now access the app

Registration is controlled by the `registration_enabled` runtime setting, which is persisted in
the `runtime_settings` table and editable via `PATCH /api/settings/runtime` (admin only). The
initial seed value is taken from the `REGISTRATION_ENABLED` environment variable (default:
`false`); after the first start, the runtime-settings row wins. The frontend checks
`GET /api/auth/config` to show/hide the registration link, and the `POST /api/auth/register`
handler reads the runtime value on every request (`auth_api/account.py`). In prod (2026-05-09)
this is set to `true`.

## Login Flow (Web)

1. User submits email + password to `POST /api/auth/login`
2. Server verifies via Argon2id, generates a random session token (`secrets.token_urlsafe(32)`)
3. Only the SHA-256 hash of the token is stored in `user_sessions.id` — the raw token is never persisted
4. Response includes `Set-Cookie: session_id=<raw_token>; HttpOnly; Secure; SameSite=Lax`
5. On each request, the middleware hashes the cookie value and looks up the hash in the DB
6. Sessions expire after 7 days (configurable via `SESSION_EXPIRY_DAYS`)

## Login Flow (MCP / API)

1. User generates an API key via the web UI (`POST /api/auth/api-keys`)
2. Key is shown once (format: `jnl_<random>`)
3. User adds the key to their MCP client config:
   ```json
   {
     "mcpServers": {
       "journal": {
         "url": "https://journal.example.com/mcp",
         "headers": {
           "Authorization": "Bearer jnl_..."
         }
       }
     }
   }
   ```
4. Server validates the key by SHA-256 hashing and looking up in `api_keys`

## Account Lockout

After 5 consecutive failed login attempts, the account is locked for 15 minutes. The counter
resets on successful login. Constants live in `src/journal/services/auth.py`
(`_MAX_FAILED_ATTEMPTS`, `_LOCKOUT_MINUTES`).

There is no app-level rate limit on `/api/auth/login` itself — see
[`security-roadmap.md`](security-roadmap.md) Tier 2 item 3.

## Password Reset

1. User submits email to `POST /api/auth/forgot-password` (always returns 200, regardless of
   whether the email exists — prevents enumeration)
2. If the email exists, server sends a reset email with a signed `itsdangerous` token
   (30 min expiry, salt `password-reset`)
3. User clicks link → `/reset-password?token=...` (frontend can pre-validate via
   `GET /api/auth/verify-reset-token?token=...`)
4. User submits new password to `POST /api/auth/reset-password`
5. On success, all of the user's existing sessions are deleted (`AuthService.reset_password`
   calls `delete_user_sessions`) and the lockout counter is cleared.

Note: reset tokens are stateless and currently reusable within the 30-minute window. Tracked
as [`security-roadmap.md`](security-roadmap.md) Tier 2 item 5.

## Data Isolation

All user-generated data is scoped by `user_id`:

- **entries** — `user_id` column, all queries filtered
- **entities** — `user_id` column, UNIQUE constraint includes user_id
- **jobs** — `user_id` column, admins can see all
- **ChromaDB vectors** — `user_id` in metadata, filtered via `where` clause
- **FTS5 search** — scoped via JOIN with entries table

A user can never see another user's data through any API endpoint or MCP tool.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `JOURNAL_SECRET_KEY` | (required) | Secret for signing reset / verification tokens (`itsdangerous`). Server fails closed at startup if unset. |
| `REGISTRATION_ENABLED` | `false` | Initial seed for the `registration_enabled` runtime setting. After first start, the runtime-settings row is authoritative. |
| `SESSION_EXPIRY_DAYS` | `7` | Session lifetime in days |
| `SMTP_HOST` | `smtp.gmail.com` | SMTP server for sending emails |
| `SMTP_PORT` | `465` | SMTP port (SSL) |
| `SMTP_USERNAME` | (empty) | SMTP username |
| `SMTP_PASSWORD` | (empty) | SMTP password (Gmail App Password) |
| `SMTP_FROM_EMAIL` | (empty) | From address for system emails |
| `APP_BASE_URL` | `http://localhost:5173` | Base URL for email links |

## Admin Panel

Admin users (`is_admin=true`) can:

- View all users with stats (entry count, word count, job count)
- Enable/disable user accounts
- Promote/demote admin status
- Trigger live config reloads via the four `POST /api/admin/reload/{ocr-context, transcription-context, mood-dimensions, entity-casing}` endpoints — see `configuration.md` and `context-files.md`.
- Edit the `pricing` table and runtime feature flags (`PATCH /api/settings/pricing`, `PATCH /api/settings/runtime`).

Access the admin panel via `/admin` in the web UI (link visible only to admins). Routes are implemented in
`src/journal/auth_api/admin.py` (after the 2026-05-08 `auth_api/` package split).
