# Conversations (chat with the LLM about a journal answer) — design

**Status:** approved, pending implementation plan. **Date:** 2026-06-17.
**Scope:** cross-cutting — `server/` (migration + repository + service + provider +
REST) and `webapp/` (router + views + store + nav).

## Problem

Today the answer tile is a dead end: `POST /api/search/answer` returns a single
grounded answer and there's no way to keep talking to the model about it. The
user wants to **continue the conversation** — ask follow-ups like "and when did
it get better?" — and to **revisit** past conversations later.

## Decisions (locked during brainstorming)

1. **Dedicated full-page chat view**, not an inline reply box.
2. **Persisted conversations + a history list** — saved server-side, revisitable.
3. **Re-search per reply** — each follow-up re-grounds against the journal
   (using the follow-up + the original question for pronoun/context resolution),
   so new specifics pull the right entries.
4. A conversation is **seeded from the answer the user already has** on Search
   (no second Sonnet call to recreate the first turn).
5. Also in this change: **remove the ✨ star** from the answer tile header.

## Architecture

```
Search page                         Conversations
 ┌─────────────────────┐            ┌──────────────────────────────┐
 │ answer tile          │  "Continue │ /conversations        (list) │
 │  [Continue this →] ──┼──conversation──▶ POST /api/conversations  │
 └─────────────────────┘   (seed)    │ /conversations/:id    (chat) │
                                      │   POST .../messages (reply)  │
                                      └──────────────┬───────────────┘
                                                     ▼
                              ConversationService.reply()
                               ├─ re-retrieve passages (QueryService.search_entries
                               │    on `original_question + "\n" + follow-up`)
                               ├─ Answerer.continue_conversation(history, passages)
                               └─ persist user+assistant turns; return assistant turn
```

Conversations are **user-scoped**. The answerer and hybrid retrieval are reused;
the only genuinely new infrastructure is persistence (one migration + a
repository) and the chat UI.

## Server

### 1. Migration `0030_conversations.sql`

(Next number after the current `0029`. Re-runnable: `CREATE TABLE IF NOT EXISTS`.)

```sql
CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_user
    ON conversations(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS conversation_messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role             TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content          TEXT NOT NULL,
    citations        TEXT,            -- JSON [{entry_id,entry_date,snippet}], assistant only
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversation_messages_conv
    ON conversation_messages(conversation_id, id);
```

Timestamps are ISO-8601 strings (matches the rest of the schema). `ON DELETE
CASCADE` so deleting a conversation removes its messages; deleting a user removes
their conversations.

### 2. `db/conversation_repository.py` — `SQLiteConversationRepository`

Mirrors the style of `jobs_repository.py` / `storyline_repository.py` (connection
via the same factory; every method takes `user_id` and filters on it). Dataclasses
in `models.py`:

```python
@dataclass
class ConversationMessage:
    id: int
    role: str               # "user" | "assistant"
    content: str
    citations: list[dict]   # [{entry_id, entry_date, snippet}]; [] for user turns
    created_at: str

@dataclass
class Conversation:
    id: int
    user_id: int
    title: str
    created_at: str
    updated_at: str
    messages: list[ConversationMessage]   # empty in list views, populated in get()
```

Methods:
- `create(user_id, title, seed_messages) -> Conversation` — insert the conversation
  + the seed user/assistant turns in one transaction; `updated_at = created_at`.
- `list(user_id) -> list[Conversation]` — id/title/created/updated + message_count,
  newest-updated first; no message bodies.
- `get(conversation_id, user_id) -> Conversation | None` — with messages ordered by
  `id`. `None` if not found **or** not owned by `user_id` (callers map to 404).
- `add_messages(conversation_id, user_id, messages) -> list[ConversationMessage]` —
  append turns + bump `updated_at`; returns the inserted rows. Returns `None`/raises
  a not-found sentinel if the conversation isn't owned by `user_id`.
- `delete(conversation_id, user_id) -> bool` — delete if owned; `False` otherwise.

`citations` is JSON-encoded on write and decoded on read inside the repository, so
callers always see `list[dict]`.

### 3. `providers/answerer.py` — multi-turn method

Add (keeping the existing one-shot `answer(question, passages)` untouched):

```python
@dataclass(frozen=True)
class ConversationTurn:
    role: str       # "user" | "assistant"
    content: str

class Answerer(Protocol):
    def answer(self, question, passages) -> AnswerResult: ...
    def continue_conversation(
        self, history: list[ConversationTurn], passages: list[AnswerPassage]
    ) -> AnswerResult: ...
```

`history` is the full thread **including** the latest user turn as its last item.
`AnthropicAnswerer.continue_conversation` builds the Anthropic request:
- `system` = a conversation-grounding prompt (same strict-grounding rules as the
  one-shot, phrased for an ongoing chat: answer only from the passages + the
  conversation; if the passages don't cover the follow-up, say so; cite the
  `entry_id`s used; never invent).
- `messages` = the `history` turns mapped to `{role, content}`, with the
  freshly-retrieved passages appended to the **final user turn's** content (same
  `[entry_id=.. date=..] text` format as the one-shot user message).
- Same adaptive thinking, `cache_control` on the system block, lenient JSON parse
  (`{answer, answered, cited_entry_ids}`), id coercion/validation against the
  passage set, and `AnswerUnavailable` on API error / malformed output as the
  one-shot path. `NoopAnswerer.continue_conversation` returns `answered=False`.

### 4. `services/conversations.py` — `ConversationService`

Injected with the repository, the `QueryService` (retrieval), the `Answerer`, and
config (`context_entries`, `passage_chars`, `model`). Methods:

- `start(user_id, question, answer, citations) -> Conversation` — title =
  `question` trimmed to ≤120 chars; seed messages = `[user: question,
  assistant: answer (+citations)]`; persist and return. No LLM call.
- `reply(user_id, conversation_id, message) -> ConversationMessage` —
  1. `get(conversation_id, user_id)`; 404 sentinel if missing.
  2. `original_question` = the first user message's content.
  3. retrieve: `QueryService.search_entries(query=f"{original_question}\n{message}",
     limit=context_entries, user_id=user_id)`.
  4. `history` = existing messages → `ConversationTurn`s, **plus** the new
     `user: message` turn.
  5. `result = answerer.continue_conversation(history, passages)` (passages built
     from the retrieved results, text truncated to `passage_chars`).
  6. resolve `result.cited_entry_ids` → citations against the retrieved results
     (drop ids not in the set), exactly like `AnswerService`.
  7. `add_messages(conversation_id, user_id, [user: message, assistant: answer
     (+citations)])` — **both turns stored only after the LLM succeeds**, so a
     failed reply leaves the thread consistent.
  8. return the assistant `ConversationMessage`.
  - `AnswerUnavailable` propagates (route → 502; nothing persisted).
- `list(user_id)`, `get(user_id, conversation_id)`, `delete(user_id, conversation_id)`
  pass through to the repository.

### 5. `api/conversations.py` — REST routes

Registered from `api/__init__.py`. All bearer-auth via `get_authenticated_user`;
all scoped to that user; not-found/other-user → `404 not_found`.

| Method & path | Body | Returns |
|---|---|---|
| `POST /api/conversations` | `{question, answer, citations?}` | `201` conversation (id, title, messages) |
| `GET /api/conversations` | — | `{conversations: [{id, title, updated_at, message_count}]}` |
| `GET /api/conversations/{id}` | — | conversation with `messages` |
| `POST /api/conversations/{id}/messages` | `{message}` | `201` the assistant `ConversationMessage` |
| `DELETE /api/conversations/{id}` | — | `204` |

Errors: `400 missing_*` (empty `question` / `message`); `404 not_found`;
`502 answer_unavailable` (reply synthesis failed); `503` if the service isn't wired.
Message JSON shape: `{id, role, content, citations:[{entry_id,entry_date,snippet}],
created_at}`.

### 6. Wiring + config

- `ServicesDict`: add `conversation: ConversationService` and the repository.
- `bootstrap.py`: construct `SQLiteConversationRepository` + `ConversationService`
  (reusing `query_service`, `answerer`, and the existing answer config).
- No new env vars — reuses `ANSWER_MODEL` / `ANSWER_CONTEXT_ENTRIES`.

## Webapp

### Routes & nav
- `/conversations` → `ConversationListView` (history).
- `/conversations/:id` → `ConversationView` (chat).
- Both protected like existing authed routes. Add a **"Conversations"** item to
  `AppSidebar`.

### Types (`types/conversation.ts`)
`ConversationMessage` (`id, role, content, citations: AnswerCitation[], created_at`),
`ConversationSummary` (`id, title, updated_at, message_count`),
`Conversation` (summary + `messages`).

### API client (`api/conversations.ts`)
`createConversation({question, answer, citations})`, `listConversations()`,
`getConversation(id)`, `sendMessage(id, message)`, `deleteConversation(id)`.

### Store (`stores/conversations.ts`)
Current conversation (`conversation`, `messages`, `sending`, `error`) + list
(`summaries`, `listLoading`). Actions: `open(id)`, `loadList()`, `start(seed)`
(returns new id for navigation), `reply(message)`, `remove(id)`.

### Views
- **`ConversationView`**: header (title + back to Search), message list — user
  bubbles (right), assistant bubbles (left) with citation chips (`RouterLink` to
  `/entries/:id`), a "Thinking…" placeholder while `sending`, and a sticky input +
  Send (Enter submits; disabled while sending or empty).
- **`ConversationListView`**: rows of title + relative updated time + message
  count; click opens; a delete control per row (confirm).

### Search integration + star removal
- Answer tile: add **"Continue this conversation →"** — calls `store.start({
  question: store.lastRunQuery, answer: store.answer, citations: store.answerCitations
  })` then routes to `/conversations/:id`. Shown only when an answer is present.
- Remove the `<span aria-hidden="true">✨</span>` from the answer tile header
  (keep the "Answer" label).

## Testing

- **Server:** `conversation_repository` (create/list/get/add/delete + user-scoping
  — another user gets `None`/404); `ConversationService` (`reply` re-retrieves with
  the combined query, resolves citations, persists both turns only on success,
  propagates `AnswerUnavailable` with nothing persisted); `answerer`
  `continue_conversation` (history → messages, citation filtering, raise on
  malformed/API error); `api/conversations` routes (create/list/get/reply/delete,
  404 on another user's id, 400s, 502). Fakes for the answerer/query as in
  `test_answer.py`. Full unit suite green; ruff clean.
- **Webapp:** conversations store (start/open/reply/remove, error handling),
  `ConversationView` (renders turns + citations, send calls `reply`, loading state),
  `ConversationListView` (renders + delete), Search "Continue this conversation"
  navigates with the seed, and the star is gone from the answer tile. Full gate
  green incl. ≥85% coverage.

## Docs & journal
- Server: new `docs/conversations.md` (data model, endpoints, grounding); link it
  from `docs/search.md`'s answer section. Webapp: a note in the search/dev doc.
- Dated journal entries in both repos.

## Cost & latency
Per reply: one hybrid retrieval (cache or fresh) + one Sonnet call (~5–8¢). Token
cost grows with thread length (history is re-sent each turn) but is bounded for the
handful-of-turns case; no cap in v1 (revisit if threads get long).

## Out of scope (YAGNI)
- Streaming the reply (full answer after a "Thinking…" state is fine for v1).
- LLM-generated conversation titles (use the question text).
- Editing/branching/regenerating turns; sharing; search within conversations.
- A per-turn standalone-query rewrite (concatenating original question + follow-up
  is enough for v1).
