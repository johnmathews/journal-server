# Conversations — persisted multi-turn chat about a journal answer

**Date:** 2026-06-17
**Branch:** `feat/conversations`
**Plan:** `docs/superpowers/plans/2026-06-17-conversations.md`
**Reference doc:** [`docs/conversations.md`](../docs/conversations.md)

## Context

The Search answer tile was a dead end: one grounded answer, no follow-up.
This session adds full persisted conversations — the user can click
"Continue this conversation →" on any answer and continue asking questions
that re-ground against the journal, with threads saved and revisitable.

## Key decisions

### Seed from the existing answer (no second LLM call)

The first call is `POST /api/conversations` with the question/answer/citations
the webapp already holds from the Search answer tile. `ConversationService.start()`
writes both seed turns to the DB without calling the answerer at all. Title =
question text trimmed to ≤120 chars — no LLM-generated title, no extra cost.
Consequence: the first turn is grounded by exactly the same passages the answer
was, without re-running retrieval.

### Re-search per reply with the combined query

Each follow-up calls `QueryService.search_entries(original_question + "\n" + follow-up)`.
Concatenating the original question gives the retrievers pronoun/context for
resolution ("and when did it get better?" → the back-pain context is preserved)
without an additional query-rewrite LLM call. A standalone query rewrite is
deferred to v2 if evals show it's needed.

### Persist-on-success

Both the user turn and the assistant turn are written atomically only after
`Answerer.continue_conversation` returns successfully. `AnswerUnavailable`
propagates to the caller (route → 502) and nothing is written — the thread
stays consistent and the message count doesn't increment on failure.

### Reuse the existing answerer and QueryService

`ConversationService` is injected with the same `QueryService` and `Answerer`
instances used by `AnswerService`. The new `continue_conversation` method on
`Answerer` builds a multi-turn Anthropic request: existing turns mapped to
`{role, content}` messages, with the freshly-retrieved passages appended to
the final user turn's content. Same strict-grounding rules, same
`AnswerUnavailable` / malformed-output error handling as the one-shot path.

### Reuse answer config — no new env vars

`ANSWER_MODEL` and `ANSWER_CONTEXT_ENTRIES` are passed through from the
existing config. Adding new env knobs when the semantics are the same would be
noise. Documented explicitly in `docs/conversations.md`.

### User-scoped at every layer

The repository, service, and routes all filter by `user_id`. Another user's
conversation id is indistinguishable from "not found" — both return 404. This
matches the pattern used by `storyline_repository.py` and `jobs_repository.py`.

### Migration number correction (0030 → 0032)

The spec was drafted when the latest migration was `0029`. During Task 1 it
emerged that the branch already contained `0030_storyline_chapter_reorder.sql`
and `0031_storyline_chapter_sectioning.sql` (the storyline-chapter migrations
landed on `feat/conversations` before this work started). The migration was
therefore numbered `0032_conversations.sql`. No schema change — just a sequence
correction.

## New files

- `src/journal/db/migrations/0032_conversations.sql` — two tables (`conversations`, `conversation_messages`) with cascade deletes and CHECK constraint on `role`.
- `src/journal/db/conversation_repository.py` — `SQLiteConversationRepository` (create/list/get/add_messages/delete, user-scoped).
- `src/journal/services/conversations.py` — `ConversationService` (start + re-grounding reply).
- `src/journal/api/conversations.py` — five REST routes registered via `register_conversations_routes`.
- `docs/conversations.md` — data model, endpoints, reply flow, config.
- `journal/260617-conversations.md` — this file.
- `tests/test_db/test_migration_0032_conversations.py` — migration tests.
- `tests/test_db/test_conversation_repository.py` — repository CRUD + user-scoping.
- `tests/test_services/test_conversations.py` — service tests (re-grounding, persist-on-success, propagation).
- `tests/test_api_conversations.py` — route tests (create/list/get/reply/delete, 400s, 502, 503).

## Modified files

- `src/journal/models.py` — `ConversationMessage` + `Conversation` dataclasses.
- `src/journal/providers/answerer.py` — `ConversationTurn` dataclass, `continue_conversation` on `Answerer` Protocol, `NoopAnswerer`, `AnthropicAnswerer`.
- `src/journal/api/__init__.py` — import + call `register_conversations_routes`.
- `src/journal/service_registry.py` — `conversation` + `conversation_repository` keys.
- `src/journal/mcp_server/bootstrap.py` — construct repo + service, add to `_services`.
- `docs/search.md` — link to `conversations.md` from the answer-synthesis section.

## Out of scope (v1)

Streaming, LLM-generated titles, editing/branching turns, per-turn standalone
query rewrite, sharing conversations. All deferred — see `docs/conversations.md`.
