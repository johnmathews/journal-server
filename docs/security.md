# Security

This document describes the security posture of journal-server: what it protects, what it does not, and how to deploy it
safely. The server supports multiple users with per-user data isolation.

## Threat model

The primary asset is the journal entry content: handwritten pages (via OCR), voice notes (via transcription), and any
extracted text, chunks, mood scores, or derived data stored in SQLite and ChromaDB. This is personal, private, and
sensitive.

Realistic adversaries:

1. **An attacker on the same network** as the VM running the server (home LAN, shared Wi-Fi, compromised other device on
   the LAN).
2. **A malicious webpage** open in the user's browser making cross-origin `fetch()` calls to the MCP endpoint via DNS
   rebinding.
3. **A stolen or lost VM disk image / backup tape** containing the SQLite database and ChromaDB data directory.
4. **Another authenticated user** accessing data belonging to a different user through the API.

Explicitly out of scope:

- Nation-state adversaries.
- Provider-side data retention at Anthropic / OpenAI (addressed by policy, not code — see "Provider data retention"
  below).
- Side-channel attacks on the host.
- Prompt injection via OCR content (a handwritten page that tries to manipulate downstream LLM analysis — worth knowing
  about but not something this server can mitigate).

## Defences in place

### Multi-user authentication

Two authentication mechanisms, checked in order by the middleware:

1. **Cookie sessions** — used by the web frontend (httpOnly, Secure, SameSite=Lax, 7-day expiry)
2. **API keys (bearer tokens)** — used by MCP clients and external API consumers (format: `jnl_<random>`)

See [auth.md](auth.md) for the full authentication architecture (registration, login, password reset, account lockout).

### Per-user data isolation

Every read, update, and delete query filters by `user_id`. A user can never see, modify, or delete another user's data
through any API endpoint or MCP tool. Isolation is enforced at the repository layer (SQL `WHERE user_id = ?`) and the
vector store layer (ChromaDB `where` filter on `user_id` metadata).

Admin users (`is_admin=true`) can see all jobs via the jobs API, but cannot access other users' entries or entities.
Admins can also trigger live reloads of file-backed config — OCR glossary, transcription context, and mood dimensions
— via `POST /api/admin/reload/{ocr-context,transcription-context,mood-dimensions}` (see `docs/configuration.md`).
Non-admin sessions get 403; unauthenticated requests get 401.

### Session and API key storage

- **Session tokens** are SHA-256 hashed before storage in `user_sessions.id`. The raw token is returned once (in the
  Set-Cookie header) and never stored. If the SQLite file is exposed, an attacker cannot impersonate active users.
- **API keys** follow the same pattern: SHA-256 hashed before storage in `api_keys.key_hash`. Only the 12-char prefix
  is stored for display purposes.

### Account lockout

After 5 consecutive failed login attempts, the account is locked for 15 minutes. The counter resets on successful login.
Lockout is enforced at the repository layer (thread-safe).

### Loopback port binding + reverse proxy expected

The repo's `compose.yml` binds the host-side of the journal port to `127.0.0.1:8400`, not `0.0.0.0:8400`, so when run
with the in-repo compose the port is only reachable from the VM itself. External access goes through a Cloudflare
Tunnel that fronts the webapp on `:8402`, which proxies `/api/*` to `journal-server` over the compose-internal
network — `journal-server`'s host port doesn't need LAN exposure for normal use.

> **Drift note (2026-05-09):** the production compose at `/srv/media/docker-compose.yml` on `media` currently exposes
> `8400:8400` (LAN-reachable, not loopback). Public access still goes via the Cloudflare Tunnel on `:8402`, but the
> defence-in-depth posture documented here only holds when the prod compose is brought back in line with the in-repo
> file. Tracked as a security-roadmap follow-up.

The expected stance for any LAN-exposed deployment: a reverse proxy (Caddy, Traefik, nginx) on the same host that
(1) terminates TLS with a real certificate, (2) optionally adds a second layer of auth, and (3) forwards decrypted
traffic to `127.0.0.1:8400` locally.

This is belt-and-braces with the per-user API keys / sessions: losing either (key/session leak OR proxy
misconfiguration) still leaves the other as a defence.

### ChromaDB has no external port

The `journal-chromadb` service has no `ports:` publish in `docker-compose.yml`. ChromaDB has no authentication of its own
and stores chunk text in cleartext alongside the vectors, so publishing its port would create a second unauthenticated
exfiltration path. The `journal` service reaches it via compose-internal DNS (`journal-chromadb:8000`) which is only
resolvable inside the compose network.

### DNS rebinding protection always on

The MCP transport security layer is configured with `enable_dns_rebinding_protection=True` and `MCP_ALLOWED_HOSTS` set to
loopback by default. This blocks a malicious webpage in the user's browser from tricking the server into trusting a
rebound DNS name as if it were loopback. Add any externally-facing hostname to `MCP_ALLOWED_HOSTS` if you put the service
behind a reverse proxy.

### SSRF protection on URL ingestion

`journal_ingest_media_from_url` and `journal_ingest_multi_page_from_url` accept arbitrary URLs as parameters. Before any socket
is opened, the URL-source helper calls `_validate_public_url` (defined in
`src/journal/services/ingestion/url_sources.py`), which:

1. Rejects any scheme that isn't `http` or `https` (blocks `file://`, `gopher://`, `ftp://`, etc.).
2. Resolves the hostname via DNS.
3. Refuses the request if ANY resolved address is loopback (`127.0.0.0/8`, `::1`), private (RFC1918 / RFC4193),
   link-local (`169.254.0.0/16` — this includes cloud metadata endpoints like `169.254.169.254`), multicast, reserved, or
   unspecified.

This does not defend against DNS rebinding between the resolution check and the socket connection — an attacker who
controls authoritative DNS for a public-looking domain could return a public IP to the check and a private IP to the
actual connect. Closing that gap requires patching the connection pathway to pin the resolved IP, which is out of scope
for this tool. Loopback and RFC1918 are the realistic threat surface for a personal server and they are closed.

### Secrets hygiene

API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `SLACK_BOT_TOKEN`) and the server signing secret
(`JOURNAL_SECRET_KEY`) are loaded from environment variables only — never from command-line args, never from source
files. `.env` is in `.gitignore`. Logging is intentionally free of secrets, prompts, LLM responses, and entry
content — only counts and IDs are logged.

### Filesystem permissions (your responsibility)

The SQLite database file is loaded from `DB_PATH` with default filesystem permissions (`0644` on most Unixes), which
means any other local user on the host can read your journal. For a single-user workstation this is usually fine; on a
multi-user host, chmod the file and its parent directory to `0600` / `0700`:

```bash
chmod 600 journal.db
chmod 700 /path/to/journal/data
```

In the Docker deployment the bind mount `/srv/media/config/journal/` inherits the host's permissions; make sure that
directory is owned by the user running Docker and is not world-readable.

## Provider data retention

Journal content is sent to three third-party providers:

- **Anthropic** — handwritten page images for OCR via Claude Opus 4.6 (when `OCR_PROVIDER=anthropic` or as the second
  pass under `OCR_DUAL_PASS=true`); entity extraction (Opus); mood scoring (Sonnet 4.5); search reranking (Haiku 4.5).
- **Google (Gemini)** — handwritten page images for OCR via Gemini 2.5 Pro (when `OCR_PROVIDER=gemini`, the prod
  default); voice audio for transcription via Gemini 2.5 Pro (when `TRANSCRIPTION_PROVIDER=gemini` or as a parallel
  shadow via `TRANSCRIPTION_SHADOW_PROVIDER=gemini`).
- **OpenAI** — voice audio for transcription (default `gpt-4o-transcribe`, fallback `whisper-1`), and chunk text for
  `text-embedding-3-large` embeddings.

Anthropic and OpenAI retain API inputs and outputs for approximately 30 days by default as part of abuse monitoring,
and neither uses API data for model training on the commercial tier (OpenAI stopped in March 2023; Anthropic's
commercial API terms exclude training). Google's Gemini API retention is governed by the paid Vertex AI / Gemini API
terms — abuse-monitoring retention applies, and customer data is not used to improve Google models on the paid tier;
specifics are in Google's data-handling docs. If your threat model includes the providers themselves, apply for Zero
Data Retention (ZDR) with all three (where available) and use the organisation-level ZDR header from then on.

This is a policy decision, not a code change. The journal-server does not currently send any ZDR headers.

## Backups

There is no built-in backup mechanism. If the bind mount `/srv/media/config/journal/data` is lost, the entire journal is
gone. Recommended: add an external host-level backup of that directory, encrypted at rest. A simple pattern is a nightly
cron that runs `sqlite3 journal.db ".backup '/backup/journal-$(date +%F).db'"` and pipes the result through `age` or
`gpg` before writing it to the backup volume.

## What is NOT protected

For the sake of informed consent:

- **TLS** is not terminated by the server itself. Run a reverse proxy (see above) in any deployment where the network
  between client and server is not trusted.
- **Storage at rest** is not encrypted. If you need encryption at rest, put the bind-mount parent directory on an
  encrypted volume (APFS encrypted, LUKS, ZFS native encryption) or migrate the SQLite file to SQLCipher.
- **Backup encryption** is the user's responsibility — see above.
- **App-level rate limiting** on auth endpoints is not yet implemented (Traefik handles network-level rate limiting).
  See [security-roadmap.md](security-roadmap.md) Tier 2 for planned improvements.
- **Password complexity** is minimal (8–1024 chars, no other constraints). See security roadmap Tier 2.
- **Password reset tokens** are stateless (itsdangerous) and can be reused within the 30-minute window. See security
  roadmap Tier 2.

## Security-relevant files

- `src/journal/auth.py` — `SessionOrKeyBackend`, `RequireAuthMiddleware`, contextvar propagation for MCP tools
- `src/journal/auth_api/` — REST surface for auth (login/logout, registration, profile, API keys, admin); split into
  `core.py`, `account.py`, `profile.py`, `api_keys.py`, `admin.py`, `_shared.py`
- `src/journal/services/auth.py` — `AuthService` (password hashing, session management, API keys, token signing)
- `src/journal/db/user_repository.py` — user, session, and API key persistence
- `src/journal/mcp_server/runserver.py` — DNS rebinding config, middleware wiring, fail-closed `JOURNAL_SECRET_KEY`
  startup check
- `src/journal/services/ingestion/url_sources.py` — `_validate_public_url` SSRF check (called from
  `IngestionService` before fetching any user-supplied URL)
- `src/journal/config.py` — `secret_key`, `mcp_allowed_hosts`, registration/SMTP/session config
- `compose.yml` / `docker-compose.yml` — loopback port binding, ChromaDB isolation
- `docs/security-roadmap.md` — prioritised list of remaining security improvements
