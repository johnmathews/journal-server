# Security Tier 1: Per-user data isolation + session token hashing

**Date:** 2026-04-15

## What changed

Implemented both items from the security roadmap's Tier 1 (Critical):

### 1. Per-user data isolation on all queries

Every read, update, and delete query now filters by `user_id`. Previously,
any authenticated user could access any other user's entries, entities, and
jobs by guessing IDs.

**Layers touched (bottom-up):**

- **Models** (`models.py`): Added `user_id` field to `Entry`, `Entity`, `Job`
  dataclasses so the application can reason about ownership.
- **Repository** (`db/repository.py`): Added `user_id: int | None = None` to
  all `EntryRepository` Protocol methods and `SQLiteEntryRepository` SQL
  queries. When not None, adds `AND user_id = ?` to WHERE clauses.
- **Entity store** (`entitystore/store.py`): Same pattern for all
  `EntityStore` Protocol methods and `SQLiteEntityStore`.
- **Job repository** (`db/jobs_repository.py`): Added user_id to `create`,
  `get`, and `list_jobs`. Admin bypass via `user_id=None`.
- **Services** (`services/query.py`, `services/ingestion.py`,
  `services/jobs.py`): Thread user_id through to repository and vector store.
- **Vector store metadata**: `_process_text` now includes `user_id` in
  ChromaDB chunk metadata; `search_entries` adds a `user_id` where-filter to
  ChromaDB queries.
- **API routes** (`api.py`): Every route handler calls
  `get_authenticated_user(request)` and passes `user_id` to service calls.
  Job routes use admin bypass (`None if user.is_admin else user.user_id`).
- **MCP tools** (`mcp_server.py`): A `contextvars.ContextVar` set by the auth
  middleware propagates user_id to tool functions via `get_current_user_id()`.

### 2. Session token hashing

Session tokens now follow the same SHA-256 pattern as API keys:
- `create_session` generates a random token, stores `sha256(token)` in
  `user_sessions.id`, returns the raw token for the cookie.
- `validate_session`, `logout`, `update_session_last_seen` all hash the
  incoming token before DB lookup.
- Migration 0012 clears existing plaintext sessions (forces re-login).

## Code review findings (fixed)

- Merge entities route was missing ownership pre-validation on absorbed IDs.
- `GET /api/entries/{id}/entities` was missing user_id extraction entirely.
- `GET /api/entities/{id}/merge-history` and merge-candidates list routes
  were missing user_id extraction.
- All fixed before commit.

## Tests

- 46 new tests: 39 data isolation tests (`test_data_isolation.py`) covering
  repository, entity store, and job repository isolation at the unit level;
  7 session hashing tests (`test_session_hashing.py`).
- All 1015 tests pass. 83% coverage.

## Decisions

- `user_id` parameters default to `None` (no filter) for backward compat
  with internal callers (CLI, background jobs). The API layer always provides
  a concrete user_id.
- Existing ChromaDB vectors don't have `user_id` in metadata. New ingestions
  will. A backfill could be added later but isn't critical since there's
  currently only one user.
