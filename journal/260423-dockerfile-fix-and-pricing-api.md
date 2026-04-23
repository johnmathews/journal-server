# Dockerfile Fix & Pricing API

## Dockerfile startup fix
The Docker CMD `uv run python -m journal.mcp_server` ran a dependency sync on every
container start, downloading ruff and other dev dependencies because `uv run` defaults
to including dev deps. The image was built with `uv sync --no-dev`, creating a mismatch.

Fixed by changing CMD to `/app/.venv/bin/python -m journal.mcp_server` — the venv is
already complete from the build stage, so no sync is needed at startup.

## API pricing configuration
Added server-stored pricing so the webapp can display accurate cost estimates that
admins can update when providers change their rates.

### New files
- `src/journal/db/migrations/0017_pricing.sql` — pricing table with seed data for all
  12 models (8 LLMs, 2 embedding, 2 transcription)
- `src/journal/db/pricing.py` — lightweight `get_all_pricing()` and `update_pricing()`
  functions (no full repository Protocol — this is simple config)
- `tests/test_db/test_pricing.py` — 15 unit tests

### API changes
- `GET /api/settings` now includes a `pricing` array in the response
- `GET /api/settings/pricing` — returns all pricing entries
- `PATCH /api/settings/pricing` — admin-only, updates cost fields per model
- Added `db_conn` to the services dict for lightweight config readers

### Design decisions
- Pricing is server-wide (not per-user) — stored in a shared table, not preferences
- Only cost fields + `last_verified` are writable; `model` and `category` are immutable
- `PATCH` accepts bulk updates with per-model error reporting (207 on partial success)
