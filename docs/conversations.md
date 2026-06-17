# Conversations

Conversations let a user continue chatting with the LLM about a journal answer — follow-up questions that re-ground against the journal — with persisted, revisitable threads.

## Data model

Migration **`0032_conversations.sql`** (two tables, re-runnable with `CREATE TABLE IF NOT EXISTS`).

### `conversations`

| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER PK` | |
| `user_id` | `INTEGER` | FK → `users(id) ON DELETE CASCADE` |
| `title` | `TEXT` | Question text trimmed to ≤120 chars. |
| `created_at` | `TEXT` | ISO-8601. |
| `updated_at` | `TEXT` | Bumped when a reply is appended. |

Index: `(user_id, updated_at DESC)` for the list query.

### `conversation_messages`

| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER PK` | |
| `conversation_id` | `INTEGER` | FK → `conversations(id) ON DELETE CASCADE` |
| `role` | `TEXT` | `CHECK (role IN ('user', 'assistant'))` |
| `content` | `TEXT` | |
| `citations` | `TEXT` | JSON `[{entry_id, entry_date, snippet}]`; `NULL` for user turns. |
| `created_at` | `TEXT` | ISO-8601. |

Index: `(conversation_id, id)` for ordered message fetch.

**Cascade behaviour:** deleting a conversation deletes its messages; deleting a user deletes their conversations. `PRAGMA foreign_keys=ON` is set in `db/connection.py`.

**User-scoping:** every repository method takes `user_id` and filters on it. A conversation owned by another user is invisible (`get`/`list` return `None`/empty) or refused (`add_messages`/`delete` raise `ConversationNotFound`).

## REST endpoints

All routes require bearer auth. "Another user's id" is indistinguishable from "not found" — both return `404 not_found`.

| Method & path | Body | Returns |
|---|---|---|
| `POST /api/conversations` | `{question, answer, citations?}` | `201` — full conversation (id, title, created_at, updated_at, messages) |
| `GET /api/conversations` | — | `200` — `{conversations: [{id, title, updated_at, message_count}]}` |
| `GET /api/conversations/{id:int}` | — | `200` — full conversation with messages |
| `POST /api/conversations/{id:int}/messages` | `{message}` | `201` — the new assistant `ConversationMessage` |
| `DELETE /api/conversations/{id:int}` | — | `204` |

### Message JSON shape

```json
{
  "id": 7,
  "role": "assistant",
  "content": "Your back pain first appears on 2026-02-14 …",
  "citations": [{"entry_id": 42, "entry_date": "2026-02-14", "snippet": "back started hurting"}],
  "created_at": "2026-06-17T10:00:00+00:00"
}
```

`citations` is `[]` for user turns.

### Error codes

| Status | `error` field | Trigger |
|---|---|---|
| `400` | `missing_question` | `question` is empty or whitespace. |
| `400` | `missing_message` | `message` is empty or whitespace. |
| `404` | `not_found` | Conversation not found or belongs to another user. |
| `502` | `answer_unavailable` | LLM call failed during reply synthesis. |
| `503` | `service_unavailable` | `ConversationService` not wired in the registry. |

## Reply flow (re-grounding)

```
POST /api/conversations/{id}/messages  {message}
         │
         ▼
  ConversationService.reply()
   1. load conversation (404 if missing)
   2. combined_query = original_question + "\n" + message
   3. QueryService.search_entries(combined_query, limit=context_entries, user_id)
   4. build history: existing turns → ConversationTurn list
                     + new user turn appended
   5. Answerer.continue_conversation(history, passages)
      • passages appended to the final user turn in the Anthropic request
      • same strict grounding rules as the one-shot answer
      • AnswerUnavailable propagates → 502, nothing persisted
   6. resolve cited_entry_ids → citations (ids not in results are dropped)
   7. repo.add_messages([user turn, assistant turn])  ← both persisted atomically
   8. return assistant ConversationMessage
```

The **combined query** (`original_question + "\n" + follow-up`) gives the dense and BM25 retrievers pronoun context without a separate query-rewrite LLM call.

**Persist-on-success:** both turns are stored only after the LLM succeeds. A failed reply leaves the thread consistent — the message count does not increment.

## Seeding a conversation

`POST /api/conversations` calls `ConversationService.start()`, which persists the seed turns (user: question, assistant: existing answer + citations) without any LLM call. Title = question trimmed to ≤120 chars.

The webapp calls this when the user clicks "Continue this conversation →" on the Search answer tile, passing the question/answer/citations it already holds — avoiding a second Sonnet synthesis call.

## Config and wiring

Conversations reuse the existing answer configuration — no new env vars:

| Env var | Default | Used for |
|---|---|---|
| `ANSWER_MODEL` | `claude-sonnet-4-6` | LLM model for `continue_conversation`. |
| `ANSWER_CONTEXT_ENTRIES` | `8` | Number of passages retrieved per reply. |

`ConversationService` is wired in `mcp_server/bootstrap.py` alongside `SQLiteConversationRepository`; both are injected via `service_registry.py` under the `conversation` key.

## Out of scope (v1)

- Streaming replies (full answer after a "Thinking…" state).
- LLM-generated conversation titles (question text is used directly).
- Editing, branching, or regenerating turns; sharing conversations.
- Per-turn standalone query rewrite (concatenating original + follow-up is sufficient).

## Related

- [search.md](search.md) — hybrid retrieval and answer synthesis that conversations re-use.
- `src/journal/services/conversations.py` — `ConversationService`.
- `src/journal/db/conversation_repository.py` — `SQLiteConversationRepository`.
- `src/journal/providers/answerer.py` — `ConversationTurn` + `continue_conversation`.
- `src/journal/api/conversations.py` — REST route registration.
