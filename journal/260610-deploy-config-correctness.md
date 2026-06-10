# 2026-06-10 — Deployment config correctness (W9)

## What

Engineering-team work unit W9: make the deployment configuration match what the
server actually reads, and fail fast on the one genuinely required secret.

- **docker-compose.yml**
  - Added `JOURNAL_SECRET_KEY=${JOURNAL_SECRET_KEY:?...}` — compose now refuses
    to interpolate without it, surfacing the requirement at `docker compose up`
    time instead of at container boot (the server already fail-closed in
    `mcp_server/runserver.py`, but only after the container started).
  - Removed the dead `JOURNAL_API_TOKEN` injection and its stale "server
    refuses to start" comment — the single bearer-token scheme was retired by
    multi-user auth (2026-04-15) and nothing reads the var anymore.
  - Removed `GARMIN_USERNAME`/`GARMIN_PASSWORD` — Garmin credentials are
    per-user (DB-stored token blobs) since fitness-multiuser W6.
  - Added pass-throughs for `REGISTRATION_ENABLED`, `APP_BASE_URL`,
    `AUTH_RATE_LIMIT_*`, and `SMTP_*`. The compose defaults **mirror
    config.py's defaults** rather than using bare `${VAR:-}`: an empty string
    is not a safe default here — `int("")` crashes config load for
    `SMTP_PORT`/`AUTH_RATE_LIMIT_MAX_REQUESTS`/`AUTH_RATE_LIMIT_WINDOW_SECONDS`,
    and `""` parses as *false* for `AUTH_RATE_LIMIT_ENABLED`, which would have
    silently disabled rate limiting.
  - Fixed the header: this file is the prod-shaped stack; local dev uses
    `docker-compose.dev.yml` + native server.

- **docker-compose.dev.yml** — dev Chroma volume target `/chroma/chroma` →
  `/data`. Verified directly: `chromadb/chroma:latest` runs
  `chroma run /config.yaml` and that file contains `persist_path: "/data"`.
  The old mount point meant dev embeddings were never persisted (nothing of
  value lost by the rename).

- **.env.example** — `JOURNAL_API_TOKEN` section replaced by
  `JOURNAL_SECRET_KEY` (with generation hint); global Garmin creds section
  deleted; auth/SMTP/rate-limit knobs documented with defaults; ChromaDB
  commentary clarified (server default 8000, dev compose publishes 8401).

- **config.py** — deleted the vestigial `api_bearer_token` field. Verified
  zero consumers outside `tests/test_config.py` before deleting. The test
  class became a negative regression test (`JOURNAL_API_TOKEN` in env has no
  effect), matching the existing W6 Garmin pattern.

- **Dockerfile** — `EXPOSE 8000` → `EXPOSE 8400` (matches `MCP_PORT` in the
  compose). Added `.dockerignore`: the build only COPYies
  `pyproject.toml`/`uv.lock`/`src/`/`config/`, but the bare build context
  previously shipped ~250MB including the real `journal.db` and `.venv`.

- **docs/configuration.md** — migration note updated to say the field is now
  deleted, not merely unread.

## Verification

- `docker compose config` resolves with a minimal env (secret + API keys);
  omitting `JOURNAL_SECRET_KEY` fails with the intended message.
- `docker compose -f docker-compose.dev.yml config` resolves.
- Full suite: 2568 passed. `ruff check src/ tests/` clean.
- Grep: no live `JOURNAL_API_TOKEN`/`GARMIN_USERNAME` references remain —
  only negative regression tests and the retirement comment in config.py.
