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

## Reply flow (classify → dispatch → fallback)

```
POST /api/conversations/{id}/messages  {message}
         │
         ▼
  ConversationService.reply()
   1. load conversation (404 if missing)
   2. IntentClassifier.classify(message, context)
      → IntentResult{intent, search_query, topic, start_date, end_date, dimension}
      • Primary: AnthropicIntentClassifier (Haiku, one cheap JSON call)
      • Fallback: HeuristicIntentClassifier (offline regex) if Haiku errors
   3. dispatch to handler by intent (lookup/aggregate/temporal/trend)
      • any handler error except AnswerUnavailable → fall back to LookupHandler
   4. handler returns ReplyOutcome{answer, answered, citations}
   5. repo.add_messages([user turn, assistant turn])  ← both persisted atomically
   6. return assistant ConversationMessage
```

**Persist-on-success:** both turns are stored only after the handler succeeds. `AnswerUnavailable` propagates → 502, nothing persisted.

### Intent handlers

| Intent | Example | What the handler does |
|---|---|---|
| `lookup` | "what did I say about Rome?" | Hybrid retrieval, candidate pool 20 → adaptive `select_passages` [3,15] → matched-chunk truncation (`window_passage`) → one bounded `search_again` re-retrieval hop |
| `aggregate` | "how many times did I mention my back?" | `get_topic_frequency` → count fed as `context_note` |
| `temporal` | "when did the back pain start?" | `search_entries(sort="date_asc")` so the earliest evidencing entry is present |
| `trend` | "have I gotten happier this year?" | `get_mood_trends` summarized into `context_note` + a few representative entries |

**Fallback safety:** any handler error (except `AnswerUnavailable`) falls back to the `lookup` handler, so the floor is always the previous behavior.

### Adaptive passage selection (lookup)

`select_passages` picks an adaptive number of passages from a ranked candidate pool:

1. Retrieve the top **20** candidates (`_CANDIDATE_POOL`).
2. Keep every result whose score is within a **0.5 band** of the top score.
3. Clamp to **[3, 15]** (`_PASSAGE_FLOOR`, `_PASSAGE_CEILING`).
4. Truncate each passage with `window_passage`, which centers the 800-char window on the matched chunk (dense `char_start`/`char_end` → FTS5 snippet → head fallback), replacing the old naïve head truncation.
5. If the answerer requests a second retrieval (`search_again`), one additional hop is allowed; the same pool+selection logic applies.

## Seeding a conversation

`POST /api/conversations` calls `ConversationService.start()`, which persists the seed turns (user: question, assistant: existing answer + citations) without any LLM call. Title = question trimmed to ≤120 chars.

The webapp calls this when the user clicks "Continue this conversation →" on the Search answer tile, passing the question/answer/citations it already holds — avoiding a second Sonnet synthesis call.

## Config and wiring

| Env var | Default | Used for |
|---|---|---|
| `ANSWER_MODEL` | `claude-sonnet-4-6` | LLM model for `continue_conversation`. |
| `ANSWER_CONTEXT_ENTRIES` | `8` | Passage limit for the legacy path (unused by handlers; handlers use `_CANDIDATE_POOL=20`). |

Intent-routing tunable knobs (compile-time constants in `services/conversations/handlers.py`):

| Constant | Value | Meaning |
|---|---|---|
| `_CANDIDATE_POOL` | `20` | Hybrid-search candidate pool before adaptive trim. |
| `_PASSAGE_FLOOR` | `3` | Minimum passages kept after adaptive selection. |
| `_PASSAGE_CEILING` | `15` | Maximum passages kept after adaptive selection. |
| `_SNIPPET_CHARS` | `160` | Characters per citation snippet. |

`ConversationService` is wired in `mcp_server/bootstrap.py` alongside `SQLiteConversationRepository` and `IntentClassifier`; all injected via `service_registry.py` under the `conversation` key.

## Out of scope (v1)

- Streaming replies (full answer after a "Thinking…" state).
- LLM-generated conversation titles (question text is used directly).
- Editing, branching, or regenerating turns; sharing conversations.
- Per-turn standalone query rewrite (concatenating original + follow-up is sufficient).

## Related

- [search.md](search.md) — hybrid retrieval and answer synthesis that conversations re-use.
- `src/journal/services/conversations/` — `ConversationService` package.
- `src/journal/services/conversations/handlers.py` — four per-intent handlers + `ReplyOutcome`.
- `src/journal/services/conversations/passages.py` — `window_passage`, `select_passages`, `build_citations`.
- `src/journal/providers/intent_classifier.py` — `IntentClassifier` Protocol + Anthropic/heuristic adapters.
- `src/journal/db/conversation_repository.py` — `SQLiteConversationRepository`.
- `src/journal/providers/answerer.py` — `ConversationTurn` + `continue_conversation`.
- `src/journal/api/conversations.py` — REST route registration.
