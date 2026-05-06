# Entity aliases & description-driven re-embed — Slice A (server)

**Date:** 2026-05-06
**Branch:** `worktree-eng-entity-aliases-and-recognition`
**Plan:** `../.engineering-team/plan-entity-aliases-and-recognition.md` (lives in
the parent workspace, not this repo).

## Context

User wanted (a) the description field on an entity to actually influence future
recognition (it didn't — embedding was only computed once at creation), and
(b) user-managed aliases on entities, including a "this alias maps to X — merge?"
collision flow. This slice covers the server foundation: the webapp slice will
follow.

## What changed

### WU1 — Alias CRUD endpoints

Three new routes on `api.py`:

- `POST /api/entities/{id}/aliases` — body `{"alias": str}`. 201 with the
  updated entity, or **409** with `existing_entity_id` + `existing_canonical_name`
  + `existing_entity_type` so the webapp can offer "merge?". Idempotent on the
  same-entity case (existing alias just re-asserts).
- `DELETE /api/entities/{id}/aliases/{alias:path}` — 200 with the updated
  entity, 404 if alias absent.
- `GET /api/entities/aliases/lookup?alias=X` — non-mutating type-agnostic
  lookup so the webapp can warn before submit.

Two new `EntityStore` methods to support them:

- `remove_alias(entity_id, alias) -> bool`
- `find_entity_by_alias_for_user(alias, *, user_id)` — type-agnostic; the
  existing `find_by_alias` is type-scoped (used during extraction stage-b)
  but not the right tool for collision warnings.

All scoped through `user_id`. Tests in `test_entity_store.py::TestAliases` and
new `test_api.py::TestEntityAliasEndpoints`.

### WU2 — Async re-embed-on-description-update

Enabling description edits to actually drive recognition. The entity's stored
embedding is what stage-c similarity matching compares against during
extraction (`entity_extraction.py:_resolve_entity`); without re-embedding,
description edits were cosmetic.

Pieces:

- **`EntityExtractionService.reembed_entity_for_description(entity_id, *, user_id)`** —
  fetches the entity (user-scoped), builds `f"{name} {description}".strip()`,
  embeds, persists. Empty / whitespace descriptions short-circuit with
  `embedded=False` rather than writing a meaningless embedding of just the
  name. (We could revisit if we ever want name-only embeddings, but for now
  that's worse than the existing creation-time vector.)
- **`JobRunner.submit_entity_reembed(entity_id, *, user_id)`** + worker
  `_run_entity_reembed`. New job type `entity_reembed` validated through the
  same `_validate_params` machinery as the existing types.
- **`api.py update_entity`** snapshots the old description before calling
  `update_entity()`, then enqueues the job iff `description != old_description`.
  Best-effort — if no `job_runner` is wired into `services` (some test setups),
  the PATCH still succeeds and just doesn't include `reembed_job_id` in the
  response. Pre-existing test
  `test_patch_text_succeeds_without_job_runner` is the model for that.
- **`notifications.py`** new topic `notif_job_success_entity_reembed` with
  `default: False` — the description edit / re-embed loop will be frequent and
  routine; opt-in only. Failure notifications go through the existing global
  `notif_job_failed` toggle (the user can mute that globally; a per-type
  failure toggle wasn't worth the surface for now). Added to
  `_SUCCESS_TOPIC_MAP` and `_JOB_TYPE_LABELS`.

The PATCH response was extended with an optional `reembed_job_id` top-level
field. Existing webapp consumers that ignore unknown fields are unaffected;
the webapp slice will pick this up to plug into `useJobsStore`.

Tests added across three files:

- `test_entity_extraction.py::TestReembedDescription` — service-level (5 cases)
- `test_jobs_runner.py::TestEntityReembed` — JobRunner submit/run/fail (4 cases)
- `test_api.py::TestUpdateEntity` — 4 new cases covering enqueue-on-change,
  no-enqueue-on-no-change, no-enqueue-on-name-only-change, success-without-runner.

### WU3 — `journal backfill-entity-embeddings` CLI

Plan originally said "enqueue jobs through JobRunner". Implemented inline
instead — building a JobRunner inside a short-lived CLI just to enqueue work
that would never run (the runner shuts down with the CLI process) was the
wrong shape. The CLI command is a one-shot script the user runs manually
after deploy; doing the embeds inline is simpler, has the same final state,
and avoids the awkward "spin up the whole notification stack to backfill
500 rows" pattern.

`SELECT id, user_id, canonical_name, description FROM entities WHERE
description IS NOT NULL AND TRIM(description) != ''`, optionally filtered by
`--user-id`. For each row: `embeddings.embed_query(...)` →
`set_entity_embedding(...)`. Per-row failures don't abort the run. `--dry-run`
counts candidates without API calls. Cost estimate noted in the docstring
(~$0.003 for 500 entities at text-embedding-3-large pricing).

Tests: 4 cases in `test_cli.py` covering dry-run, real run with persistence
verification, `--user-id` scoping, and continue-on-per-row-failure. Also added
`backfill-entity-embeddings` to the `test_cli_all_commands_registered` list.

## Test summary

- Baseline: 1712 passed, 84% coverage.
- Final: 1751 passed (+39 new), 84% coverage held.
- Lint: clean.

One regression caught and fixed:
`test_non_admin_sees_no_admin_topics` was hardcoding the topics count.
Updated comment + count to reflect the new `entity_reembed` topic.

## Decisions worth flagging for future me

1. **Re-embed trigger is description-only.** Renaming an entity (canonical_name
   change) does NOT enqueue a re-embed today. The stored embedding still
   references the old name in its text. If retrieval relevance suffers
   meaningfully, revisit — but adding a re-embed on every name change felt
   like over-engineering for a comparatively rare edit.
2. **Failure notifications use the global toggle.** No per-type failure
   topic for `entity_reembed`. Cheap to add later.
3. **Stage-0 LLM-asserted match (planned in WU4) is not in this slice.** That's
   the bigger architectural change in the next slice (Slice B); kept separate
   to keep this PR coherent.

## What's next

- Slice B: WU4 — known-entity injection into the extraction prompt + four-guard
  hybrid sanity check on LLM-asserted matches.
- Slice C: webapp — alias edit UI on `EntityDetailView`, collision dialog,
  job-toast pipeline for re-embed.
