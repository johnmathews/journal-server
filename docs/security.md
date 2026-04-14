# Security

This document describes the security posture of journal-server: what it protects, what it does not, and how to deploy it
safely. It is deliberately narrow — this is a single-user personal tool, not a multi-tenant SaaS, and the defences match
that threat model.

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
4. **Another local user** on the developer's workstation reading the SQLite file directly.

Explicitly out of scope:

- Nation-state adversaries.
- Provider-side data retention at Anthropic / OpenAI (addressed by policy, not code — see "Provider data retention"
  below).
- Side-channel attacks on the host.
- Prompt injection via OCR content (a handwritten page that tries to manipulate downstream LLM analysis — worth knowing
  about but not something this server can mitigate).

## Defences in place

### API bearer token (REQUIRED)

Every request to `/api/*` and `/mcp` must carry `Authorization: Bearer <token>` matching `JOURNAL_API_TOKEN`. The server
refuses to start without a token set — there is no "auth off" mode.

- Generate a token with:
  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  ```
- Add it to your `.env` as `JOURNAL_API_TOKEN=...`.
- Set the same value in any client (webapp, Slack bot, MCP client).
- Comparison is constant-time (`hmac.compare_digest`) so a timing attack cannot recover the token one byte at a time.
- `OPTIONS` requests are allowed through without a token so CORS preflight works for the webapp. Every other HTTP method
  must authenticate.

### Loopback port binding + reverse proxy expected

`docker-compose.yml` binds the host-side of the journal port to `127.0.0.1:8400`, not `0.0.0.0:8400`. This means the port
is only reachable from the VM itself. External access must go through a reverse proxy (Caddy, Traefik, nginx) running on
the same host that:

1. Terminates TLS with a real certificate.
2. Optionally adds a second layer of auth (basic auth, mTLS, an authenticating forward proxy like Authelia).
3. Forwards decrypted traffic to `127.0.0.1:8400` locally.

This is belt-and-braces with the bearer token: losing either (token leak OR proxy misconfiguration) still leaves the
other as a defence.

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

`journal_ingest_from_url` and `journal_ingest_multi_page_from_url` accept arbitrary URLs as parameters. Before any socket
is opened, `IngestionService._download` calls `_validate_public_url`, which:

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

API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `SLACK_BOT_TOKEN`, `JOURNAL_API_TOKEN`) are loaded from environment
variables only — never from command-line args, never from source files. `.env` is in `.gitignore`. Logging is
intentionally free of secrets, prompts, LLM responses, and entry content — only counts and IDs are logged.

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

Journal content is sent to two third-party providers:

- **Anthropic** — handwritten page images for OCR via Claude Opus 4.6.
- **OpenAI** — voice audio for Whisper transcription, and chunk text for `text-embedding-3-large` embeddings.

Both providers retain API inputs and outputs for approximately 30 days by default as part of abuse monitoring, and
neither uses API data for model training on the commercial tier (OpenAI stopped in March 2023; Anthropic's commercial API
terms exclude training). If your threat model includes the providers themselves, apply for Zero Data Retention (ZDR) with
both — it is available on request for eligible customers — and use the organisation-level ZDR header from then on.

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
- **Rate limiting** on expensive endpoints (OCR, transcription, embeddings) is not implemented. A legitimate client with
  the bearer token that runs wild could burn API credits. The bearer token is the only gate.

## Security-relevant files

- `src/journal/auth.py` — `BearerTokenMiddleware`
- `src/journal/mcp_server.py` — DNS rebinding config, middleware wiring, fail-closed startup check
- `src/journal/services/ingestion.py` — `_validate_public_url` SSRF check
- `src/journal/config.py` — `api_bearer_token`, `mcp_allowed_hosts`
- `docker-compose.yml` — loopback port binding, ChromaDB isolation
- `.env.example` — auth token requirement and allowed hosts default
