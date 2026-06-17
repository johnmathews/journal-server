# Conversations (chat about a journal answer) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user continue chatting with the LLM about a journal answer — follow-up questions that re-ground against the journal — with persisted, revisitable conversations, across `server/` (migration + repository + provider + service + REST) and `webapp/` (types + client + store + router + views + nav).

**Architecture:** A conversation is seeded server-side from the answer the user already has on Search (no second synthesis call). Each follow-up re-retrieves passages with `QueryService.search_entries(original_question + "\n" + follow-up)`, hands the full thread to a new multi-turn `Answerer.continue_conversation`, and persists both turns only after the LLM succeeds. Conversations are user-scoped; the answerer and hybrid retrieval are reused — the only new infrastructure is persistence (one migration + one repository) and the chat UI.

**Tech Stack:** Python 3.13 / uv / pytest / ruff / SQLite (server); Vue 3 + TypeScript / Vite / Pinia / Vitest / Tailwind 4 (webapp).

**Source spec:** `server/docs/superpowers/specs/2026-06-17-conversations-design.md`.

**Verified facts (do not re-derive):**
- Latest migration on the branch is `0031_storyline_chapter_sectioning.sql` → new file is `0032_conversations.sql`. (An earlier date-sorted `ls` masked the `0030`/`0031` storyline-chapter migrations already present on `feat/conversations`; corrected during Task 1.)
- `users` primary key column is `id` (see `db/migrations/0011_multi_tenant.sql`).
- `PRAGMA foreign_keys=ON` is set in `db/connection.py:44`, so `ON DELETE CASCADE` fires.
- Answer config already exists: `config.answer_model` (`ANSWER_MODEL`, default `claude-sonnet-4-6`), `config.answer_context_entries` (`ANSWER_CONTEXT_ENTRIES`, default `8`). No new env vars.
- Route auth: every body runs under `_FakeAuthMiddleware` in tests; prod uses app-wide middleware. Read the user via `get_authenticated_user(request).user_id`.
- Migration 0011 seeds an admin user `id=1`, which test harnesses use as `_TEST_USER_ID`.

**Working agreement for every task:** All server commands run from inside `server/` (`cd server` or a subshell). Run `uv run pytest -m "not integration" -q` for the unit suite and `uv run ruff check src/ tests/` for lint. All webapp commands run from inside `webapp/`. Commit to the existing `feat/conversations` branch in `server/`; create and commit to a new `feat/conversations` branch in `webapp/`.

---

## Server file structure

- Create `src/journal/db/migrations/0030_conversations.sql` — schema.
- Modify `src/journal/models.py` — add `ConversationMessage` + `Conversation` dataclasses.
- Create `src/journal/db/conversation_repository.py` — `SQLiteConversationRepository` + `ConversationNotFound`.
- Modify `src/journal/providers/answerer.py` — `ConversationTurn` + `continue_conversation` on the Protocol, `NoopAnswerer`, `AnthropicAnswerer`.
- Create `src/journal/services/conversations.py` — `ConversationService` + `ConversationNotFoundError`.
- Create `src/journal/api/conversations.py` — `register_conversations_routes`.
- Modify `src/journal/api/__init__.py` — import + call the new register fn.
- Modify `src/journal/service_registry.py` — add `conversation` + `conversation_repository` keys.
- Modify `src/journal/mcp_server/bootstrap.py` — construct the repo + service, add to `_services`.
- Create `src/journal/db/conversation_repository.py` test, provider test additions, service test, route test.
- Create `docs/conversations.md`; link from `docs/search.md`; add `journal/260617-conversations.md`.

## Webapp file structure

- Create `src/types/conversation.ts`.
- Create `src/api/conversations.ts`.
- Create `src/stores/conversations.ts`.
- Create `src/views/ConversationListView.vue`, `src/views/ConversationView.vue`.
- Modify `src/router/index.ts` — two routes.
- Modify `src/components/layout/AppSidebar.vue` — nav item.
- Modify `src/views/SearchView.vue` — "Continue this conversation →" + remove the ✨ star.
- Create matching `__tests__` files; update `docs/` note + add `journal/260617-conversations.md`.

---

## Task 1: Migration `0030_conversations.sql`

**Files:**
- Create: `src/journal/db/migrations/0030_conversations.sql`
- Test: `tests/test_db/test_migration_0030_conversations.py`

- [ ] **Step 1: Write the failing test**

```python
"""Migration 0030 creates the conversations tables."""

from __future__ import annotations

from pathlib import Path

from journal.db.factory import ConnectionFactory
from journal.db.migrations import run_migrations


def _migrated(tmp_path: Path) -> ConnectionFactory:
    factory = ConnectionFactory(tmp_path / "m.db")
    run_migrations(factory.get())
    return factory


def test_conversation_tables_exist(tmp_path: Path) -> None:
    conn = _migrated(tmp_path).get()
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "conversations" in names
    assert "conversation_messages" in names


def test_message_role_check_constraint(tmp_path: Path) -> None:
    import sqlite3

    conn = _migrated(tmp_path).get()
    conn.execute(
        "INSERT INTO conversations (user_id, title, created_at, updated_at) "
        "VALUES (1, 't', '2026-06-17T00:00:00Z', '2026-06-17T00:00:00Z')"
    )
    cid = conn.execute("SELECT id FROM conversations").fetchone()[0]
    # A bogus role must be rejected by the CHECK constraint.
    try:
        conn.execute(
            "INSERT INTO conversation_messages "
            "(conversation_id, role, content, created_at) "
            "VALUES (?, 'system', 'x', '2026-06-17T00:00:00Z')",
            (cid,),
        )
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised


def test_cascade_delete_removes_messages(tmp_path: Path) -> None:
    conn = _migrated(tmp_path).get()
    conn.execute(
        "INSERT INTO conversations (user_id, title, created_at, updated_at) "
        "VALUES (1, 't', '2026-06-17T00:00:00Z', '2026-06-17T00:00:00Z')"
    )
    cid = conn.execute("SELECT id FROM conversations").fetchone()[0]
    conn.execute(
        "INSERT INTO conversation_messages "
        "(conversation_id, role, content, created_at) "
        "VALUES (?, 'user', 'hi', '2026-06-17T00:00:00Z')",
        (cid,),
    )
    conn.commit()
    conn.execute("DELETE FROM conversations WHERE id = ?", (cid,))
    conn.commit()
    remaining = conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0]
    assert remaining == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_db/test_migration_0030_conversations.py -q`
Expected: FAIL — `conversations` table does not exist.

- [ ] **Step 3: Write the migration**

Create `src/journal/db/migrations/0030_conversations.sql`:

```sql
-- Conversations: persisted multi-turn chats about a journal answer.
--
-- A conversation is seeded from an existing Search answer (one user turn
-- + one assistant turn) and grows by re-grounding each follow-up against
-- the journal. Both tables are user-scoped; deleting a conversation
-- cascades to its messages, and deleting a user cascades to their
-- conversations. Timestamps are ISO-8601 strings, matching the rest of
-- the schema. Re-runnable: CREATE TABLE IF NOT EXISTS.

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

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_db/test_migration_0030_conversations.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/journal/db/migrations/0030_conversations.sql tests/test_db/test_migration_0030_conversations.py
git commit -m "feat(db): migration 0030 — conversations + messages tables"
```

---

## Task 2: Conversation dataclasses in `models.py`

**Files:**
- Modify: `src/journal/models.py` (add two dataclasses near the other dataclasses)
- Test: `tests/test_models_conversations.py`

- [ ] **Step 1: Write the failing test**

```python
"""Conversation domain dataclasses."""

from journal.models import Conversation, ConversationMessage


def test_message_defaults_to_empty_citations() -> None:
    m = ConversationMessage(
        id=1, role="user", content="hi", citations=[], created_at="2026-06-17T00:00:00Z"
    )
    assert m.role == "user"
    assert m.citations == []


def test_conversation_holds_messages_and_count() -> None:
    m = ConversationMessage(
        id=1, role="user", content="hi", citations=[], created_at="2026-06-17T00:00:00Z"
    )
    c = Conversation(
        id=5, user_id=1, title="hi", created_at="t", updated_at="t",
        messages=[m], message_count=1,
    )
    assert c.id == 5
    assert c.messages[0] is m
    assert c.message_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_models_conversations.py -q`
Expected: FAIL — `ImportError: cannot import name 'Conversation'`.

- [ ] **Step 3: Add the dataclasses**

In `src/journal/models.py`, add (alongside the other `@dataclass` definitions; keep the file's existing import of `dataclass`/`field`):

```python
@dataclass
class ConversationMessage:
    """One turn in a conversation about a journal answer."""

    id: int
    role: str               # "user" | "assistant"
    content: str
    citations: list[dict]   # [{entry_id, entry_date, snippet}]; [] for user turns
    created_at: str


@dataclass
class Conversation:
    """A persisted chat thread, seeded from a Search answer."""

    id: int
    user_id: int
    title: str
    created_at: str
    updated_at: str
    messages: list[ConversationMessage] = field(default_factory=list)
    message_count: int = 0
```

If `field` is not already imported in `models.py`, change the dataclasses import to `from dataclasses import dataclass, field`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_models_conversations.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/journal/models.py tests/test_models_conversations.py
git commit -m "feat(models): Conversation + ConversationMessage dataclasses"
```

---

## Task 3: `SQLiteConversationRepository`

**Files:**
- Create: `src/journal/db/conversation_repository.py`
- Test: `tests/test_db/test_conversation_repository.py`

The repository mirrors `db/jobs_repository.py`: a `ConnectionFactory`, per-call `_conn()`, JSON (de)serialisation of `citations` inside the repo so callers always see `list[dict]`, ISO timestamps. Every method takes `user_id` and filters on it. Input messages are plain dicts `{"role", "content", "citations"?}`.

- [ ] **Step 1: Write the failing test**

```python
"""SQLiteConversationRepository — create/list/get/add/delete + user scoping."""

from __future__ import annotations

from pathlib import Path

import pytest

from journal.db.conversation_repository import (
    ConversationNotFound,
    SQLiteConversationRepository,
)
from journal.db.factory import ConnectionFactory
from journal.db.migrations import run_migrations

USER_A = 1
USER_B = 2


def _repo(tmp_path: Path) -> SQLiteConversationRepository:
    factory = ConnectionFactory(tmp_path / "conv.db")
    run_migrations(factory.get())
    # Seed a second user so user-scoping tests have a real foreign key.
    factory.get().execute(
        "INSERT INTO users (id, email, display_name) VALUES (?, ?, ?)",
        (USER_B, "b@example.com", "B"),
    )
    factory.get().commit()
    return SQLiteConversationRepository(factory)


def _seed() -> list[dict]:
    return [
        {"role": "user", "content": "when did my back hurt?", "citations": []},
        {
            "role": "assistant",
            "content": "On 2026-02-14.",
            "citations": [
                {"entry_id": 42, "entry_date": "2026-02-14", "snippet": "back"}
            ],
        },
    ]


def test_create_persists_conversation_and_seed_messages(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    conv = repo.create(USER_A, "when did my back hurt?", _seed())
    assert conv.id > 0
    assert conv.title == "when did my back hurt?"
    assert conv.created_at == conv.updated_at
    assert [m.role for m in conv.messages] == ["user", "assistant"]
    # citations decoded back to list[dict]
    assert conv.messages[1].citations[0]["entry_id"] == 42
    assert conv.messages[0].citations == []


def test_get_returns_none_for_other_user(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    conv = repo.create(USER_A, "t", _seed())
    assert repo.get(conv.id, USER_A) is not None
    assert repo.get(conv.id, USER_B) is None
    assert repo.get(99999, USER_A) is None


def test_list_returns_summaries_newest_first_with_counts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = repo.create(USER_A, "first", _seed())
    second = repo.create(USER_A, "second", _seed())
    # Other user's conversation must not leak in.
    repo.create(USER_B, "hidden", _seed())
    summaries = repo.list(USER_A)
    assert [c.id for c in summaries] == [second.id, first.id]
    assert summaries[0].message_count == 2
    assert summaries[0].messages == []  # list view carries no bodies


def test_add_messages_appends_and_bumps_updated_at(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    conv = repo.create(USER_A, "t", _seed())
    added = repo.add_messages(
        conv.id,
        USER_A,
        [
            {"role": "user", "content": "and when did it get better?"},
            {
                "role": "assistant",
                "content": "Around 2026-03-01.",
                "citations": [
                    {"entry_id": 7, "entry_date": "2026-03-01", "snippet": "better"}
                ],
            },
        ],
    )
    assert [m.role for m in added] == ["user", "assistant"]
    assert added[1].citations[0]["entry_id"] == 7
    reloaded = repo.get(conv.id, USER_A)
    assert reloaded is not None
    assert len(reloaded.messages) == 4
    assert reloaded.updated_at >= reloaded.created_at


def test_add_messages_rejects_other_user(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    conv = repo.create(USER_A, "t", _seed())
    with pytest.raises(ConversationNotFound):
        repo.add_messages(conv.id, USER_B, [{"role": "user", "content": "x"}])


def test_delete_only_when_owned(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    conv = repo.create(USER_A, "t", _seed())
    assert repo.delete(conv.id, USER_B) is False
    assert repo.delete(conv.id, USER_A) is True
    assert repo.get(conv.id, USER_A) is None
    # messages cascade-deleted
    remaining = repo.connection.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE conversation_id = ?",
        (conv.id,),
    ).fetchone()[0]
    assert remaining == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_db/test_conversation_repository.py -q`
Expected: FAIL — module `conversation_repository` does not exist.

- [ ] **Step 3: Write the repository**

Create `src/journal/db/conversation_repository.py`:

```python
"""SQLite repository for chat conversations about journal answers.

Mirrors ``jobs_repository`` / ``storyline_repository``: a process-wide
``ConnectionFactory`` hands out one ``sqlite3.Connection`` per OS thread,
so running on worker threads is safe by construction. Every method takes
``user_id`` and filters on it — a conversation owned by another user is
invisible (``get``/``list``) or refused (``add_messages``/``delete``).

``citations`` is JSON-encoded on write and decoded on read inside the
repository, so callers always work with ``list[dict]``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from journal.models import Conversation, ConversationMessage

if TYPE_CHECKING:
    from journal.db.factory import ConnectionFactory


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_message(row: sqlite3.Row) -> ConversationMessage:
    raw = row["citations"]
    return ConversationMessage(
        id=row["id"],
        role=row["role"],
        content=row["content"],
        citations=json.loads(raw) if raw else [],
        created_at=row["created_at"],
    )


class ConversationNotFound(Exception):  # noqa: N818
    """Raised when a conversation isn't owned by the requesting user.

    Used by ``add_messages`` so the caller can map it to a 404. ``get``
    and ``list`` return ``None`` / empty instead of raising.
    """


class SQLiteConversationRepository:
    """Repository for persisted conversations, backed by SQLite."""

    def __init__(self, factory: ConnectionFactory) -> None:
        self._factory = factory

    def _conn(self) -> sqlite3.Connection:
        return self._factory.get()

    @property
    def connection(self) -> sqlite3.Connection:
        """Calling thread's connection — for test setup / assertions."""
        return self._factory.get()

    @staticmethod
    def _insert_messages(
        conn: sqlite3.Connection,
        conversation_id: int,
        messages: Sequence[dict[str, Any]],
        created_at: str,
    ) -> None:
        for m in messages:
            citations = m.get("citations") or []
            conn.execute(
                "INSERT INTO conversation_messages "
                "(conversation_id, role, content, citations, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    m["role"],
                    m["content"],
                    json.dumps(citations) if citations else None,
                    created_at,
                ),
            )

    def create(
        self,
        user_id: int,
        title: str,
        seed_messages: Sequence[dict[str, Any]],
    ) -> Conversation:
        now = _now_iso()
        conn = self._conn()
        cur = conn.execute(
            "INSERT INTO conversations (user_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, title, now, now),
        )
        conversation_id = int(cur.lastrowid or 0)
        self._insert_messages(conn, conversation_id, seed_messages, now)
        conn.commit()
        conv = self.get(conversation_id, user_id)
        assert conv is not None  # row was just inserted
        return conv

    def list(self, user_id: int) -> list[Conversation]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT c.id, c.user_id, c.title, c.created_at, c.updated_at, "
            "       COUNT(m.id) AS message_count "
            "FROM conversations c "
            "LEFT JOIN conversation_messages m ON m.conversation_id = c.id "
            "WHERE c.user_id = ? "
            "GROUP BY c.id "
            "ORDER BY c.updated_at DESC, c.id DESC",
            (user_id,),
        ).fetchall()
        return [
            Conversation(
                id=r["id"],
                user_id=r["user_id"],
                title=r["title"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                messages=[],
                message_count=r["message_count"],
            )
            for r in rows
        ]

    def get(self, conversation_id: int, user_id: int) -> Conversation | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT id, user_id, title, created_at, updated_at "
            "FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
        if row is None:
            return None
        msg_rows = conn.execute(
            "SELECT id, role, content, citations, created_at "
            "FROM conversation_messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
        messages = [_row_to_message(m) for m in msg_rows]
        return Conversation(
            id=row["id"],
            user_id=row["user_id"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            messages=messages,
            message_count=len(messages),
        )

    def add_messages(
        self,
        conversation_id: int,
        user_id: int,
        messages: Sequence[dict[str, Any]],
    ) -> list[ConversationMessage]:
        conn = self._conn()
        owner = conn.execute(
            "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
        if owner is None:
            raise ConversationNotFound(
                f"conversation {conversation_id} not found for user {user_id}"
            )
        before_max = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM conversation_messages "
            "WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()[0]
        now = _now_iso()
        self._insert_messages(conn, conversation_id, messages, now)
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )
        conn.commit()
        rows = conn.execute(
            "SELECT id, role, content, citations, created_at "
            "FROM conversation_messages "
            "WHERE conversation_id = ? AND id > ? ORDER BY id",
            (conversation_id, before_max),
        ).fetchall()
        return [_row_to_message(r) for r in rows]

    def delete(self, conversation_id: int, user_id: int) -> bool:
        conn = self._conn()
        cur = conn.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_db/test_conversation_repository.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/journal/db/conversation_repository.py tests/test_db/test_conversation_repository.py
git commit -m "feat(db): SQLiteConversationRepository (user-scoped CRUD)"
```

---

## Task 4: `Answerer.continue_conversation` (multi-turn)

**Files:**
- Modify: `src/journal/providers/answerer.py` (add `ConversationTurn`, extend `Answerer` Protocol, `NoopAnswerer`, `AnthropicAnswerer`; extract a shared passages-block helper)
- Test: `tests/test_providers/test_answerer.py` (append cases)

Keep the existing one-shot `answer(question, passages)` untouched in behavior. Refactor only by extracting the passage-line formatting into a shared static helper used by both paths.

- [ ] **Step 1: Write the failing test (append to `tests/test_providers/test_answerer.py`)**

```python
from journal.providers.answerer import ConversationTurn


def _history() -> list[ConversationTurn]:
    return [
        ConversationTurn(role="user", content="when did my back hurt?"),
        ConversationTurn(role="assistant", content="On 2026-02-14."),
        ConversationTurn(role="user", content="and when did it get better?"),
    ]


def test_continue_conversation_maps_history_to_messages() -> None:
    raw = (
        '{"answer": "Around 2026-03-01.", "answered": true,'
        ' "cited_entry_ids": [7]}'
    )
    answerer = _answerer(raw=raw)
    passages = [
        AnswerPassage(entry_id=7, entry_date="2026-03-01", text="Back better now."),
    ]
    result = answerer.continue_conversation(_history(), passages)
    assert result.answered is True
    assert result.cited_entry_ids == [7]
    sent = answerer._client.messages.calls[0]  # type: ignore[attr-defined]
    roles = [m["role"] for m in sent["messages"]]
    assert roles == ["user", "assistant", "user"]
    # Passages are appended to the FINAL user turn only.
    assert "entry_id=7" in sent["messages"][-1]["content"]
    assert "entry_id=7" not in sent["messages"][0]["content"]


def test_continue_conversation_filters_invented_ids() -> None:
    raw = (
        '{"answer": "x", "answered": true, "cited_entry_ids": [7, 999]}'
    )
    passages = [AnswerPassage(entry_id=7, entry_date="2026-03-01", text="t")]
    result = _answerer(raw=raw).continue_conversation(_history(), passages)
    assert result.cited_entry_ids == [7]


def test_continue_conversation_malformed_raises() -> None:
    passages = [AnswerPassage(entry_id=7, entry_date="2026-03-01", text="t")]
    with pytest.raises(AnswerUnavailable):
        _answerer(raw="not json").continue_conversation(_history(), passages)


def test_continue_conversation_api_error_raises() -> None:
    import anthropic
    from unittest.mock import MagicMock

    exc = anthropic.APIError("boom", request=MagicMock(), body=None)
    passages = [AnswerPassage(entry_id=7, entry_date="2026-03-01", text="t")]
    with pytest.raises(AnswerUnavailable):
        _answerer(exc=exc).continue_conversation(_history(), passages)


def test_noop_continue_conversation_is_not_answered() -> None:
    result = NoopAnswerer().continue_conversation(
        [ConversationTurn(role="user", content="hi")], []
    )
    assert result.answered is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_providers/test_answerer.py -q`
Expected: FAIL — `ImportError: cannot import name 'ConversationTurn'`.

- [ ] **Step 3: Extend `answerer.py`**

In `src/journal/providers/answerer.py`:

(a) Add the dataclass after `AnswerResult`:

```python
@dataclass(frozen=True)
class ConversationTurn:
    """One turn of a conversation handed to the multi-turn answerer."""

    role: str       # "user" | "assistant"
    content: str
```

(b) Add a conversation system prompt next to `_SYSTEM_PROMPT`:

```python
_CONVERSATION_SYSTEM_PROMPT = (
    "You are continuing a conversation about a person's private journal. "
    "You are given the conversation so far and a numbered list of dated "
    "passages retrieved from their journal for the latest message. Answer "
    "the latest user message ONLY from these passages and the conversation.\n\n"
    "Output a single JSON object with exactly this shape:\n"
    "  {\n"
    '    "answer": "<your answer, addressed to the journal author as \'you\'>",\n'
    '    "answered": <true|false>,\n'
    '    "cited_entry_ids": [<entry_id>, ...]\n'
    "  }\n\n"
    "Rules:\n"
    "- Ground every claim in the passages. Quote dates from the passages "
    "when relevant.\n"
    "- `cited_entry_ids` lists the entry ids of the passages you actually "
    "used, most relevant first. Never invent an id.\n"
    "- If the passages do not contain enough to answer the latest message, "
    'set "answered": false and "answer": "' + NO_MATCH_MESSAGE + '" and '
    "leave `cited_entry_ids` empty. Do NOT guess or use outside knowledge.\n"
    "- Output the JSON object only. No prose, no markdown."
)
```

(c) Extend the `Answerer` Protocol:

```python
@runtime_checkable
class Answerer(Protocol):
    """Protocol for question answerers."""

    def answer(
        self, question: str, passages: list[AnswerPassage]
    ) -> AnswerResult: ...

    def continue_conversation(
        self,
        history: list["ConversationTurn"],
        passages: list[AnswerPassage],
    ) -> AnswerResult: ...
```

(d) Add to `NoopAnswerer`:

```python
    def continue_conversation(
        self,
        history: list[ConversationTurn],
        passages: list[AnswerPassage],
    ) -> AnswerResult:
        return AnswerResult(
            answer="Answer synthesis is disabled.",
            answered=False,
            cited_entry_ids=[],
        )
```

(e) Extract the shared passage-line helper and refactor `_format_user_message` to use it, then add `continue_conversation` and a shared result-builder to `AnthropicAnswerer`:

```python
    @staticmethod
    def _passage_lines(passages: list[AnswerPassage]) -> list[str]:
        lines = ["Passages:"]
        for p in passages:
            text = p.text
            if len(text) > _MAX_PASSAGE_CHARS:
                text = text[: _MAX_PASSAGE_CHARS - 1] + "…"
            lines.append(f"[entry_id={p.entry_id} date={p.entry_date}] {text}")
        return lines

    @staticmethod
    def _format_user_message(
        question: str, passages: list[AnswerPassage]
    ) -> str:
        lines = [f"Question: {question}", ""]
        lines.extend(AnthropicAnswerer._passage_lines(passages))
        lines.append("")
        lines.append("Output the JSON object now.")
        return "\n".join(lines)

    def _result_from_response(
        self, response: object, valid_ids: set[int]
    ) -> AnswerResult:
        raw = self._first_text(response)
        parsed = self._parse_response(raw)
        if parsed is None:
            logger.warning(
                "AnthropicAnswerer returned malformed output. "
                "Raw (first 200 chars): %r",
                (raw or "")[:200],
            )
            raise AnswerUnavailable("malformed answerer output")
        cited: list[int] = []
        for eid in parsed["cited_entry_ids"]:
            try:
                eid_int = int(eid)
            except (TypeError, ValueError):
                continue
            if eid_int in valid_ids:
                cited.append(eid_int)
        return AnswerResult(
            answer=str(parsed["answer"]),
            answered=bool(parsed["answered"]),
            cited_entry_ids=cited,
        )

    def continue_conversation(
        self,
        history: list[ConversationTurn],
        passages: list[AnswerPassage],
    ) -> AnswerResult:
        if not history:
            return AnswerResult(answer=NO_MATCH_MESSAGE, answered=False)

        messages = [{"role": t.role, "content": t.content} for t in history]
        # Append the freshly-retrieved passages to the final user turn so
        # the model grounds the latest message against them.
        passage_block = "\n".join(
            [*self._passage_lines(passages), "", "Output the JSON object now."]
        )
        messages[-1]["content"] = (
            f"{messages[-1]['content']}\n\n{passage_block}"
        )
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                thinking={"type": "adaptive"},
                system=[
                    {
                        "type": "text",
                        "text": _CONVERSATION_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=messages,
            )
        except anthropic.APIError as e:
            logger.warning("AnthropicAnswerer continue call failed: %s", e)
            raise AnswerUnavailable(str(e)) from e

        return self._result_from_response(response, {p.entry_id for p in passages})
```

Optionally refactor the existing `answer()` to call `_result_from_response` too — but only if all existing `test_answerer.py` cases stay green. If unsure, leave `answer()` as-is; the helper is additive.

- [ ] **Step 4: Run tests to verify they pass (new + existing one-shot)**

Run: `uv run pytest tests/test_providers/test_answerer.py -q`
Expected: PASS — all prior one-shot tests plus the 5 new ones.

- [ ] **Step 5: Commit**

```bash
git add src/journal/providers/answerer.py tests/test_providers/test_answerer.py
git commit -m "feat(answerer): continue_conversation multi-turn grounding"
```

---

## Task 5: `ConversationService`

**Files:**
- Create: `src/journal/services/conversations.py`
- Test: `tests/test_services/test_conversations.py`

Injected with the repository, `QueryService` (retrieval), the `Answerer`, and config (`context_entries`, `passage_chars`, `model`). `reply()` re-retrieves with the combined query, builds history incl. the new user turn, calls `continue_conversation`, resolves citations against the retrieved results, and persists both turns only after the LLM succeeds. `AnswerUnavailable` propagates (nothing persisted).

- [ ] **Step 1: Write the failing test**

```python
"""ConversationService — start + reply (re-retrieve, persist on success)."""

from __future__ import annotations

from pathlib import Path

import pytest

from journal.db.conversation_repository import SQLiteConversationRepository
from journal.db.factory import ConnectionFactory
from journal.db.migrations import run_migrations
from journal.models import SearchResult
from journal.providers.answerer import (
    AnswerResult,
    AnswerUnavailable,
    ConversationTurn,
)
from journal.services.conversations import (
    ConversationNotFoundError,
    ConversationService,
)

USER_A = 1


def _repo(tmp_path: Path) -> SQLiteConversationRepository:
    factory = ConnectionFactory(tmp_path / "c.db")
    run_migrations(factory.get())
    return SQLiteConversationRepository(factory)


def _result(entry_id: int, date: str, text: str) -> SearchResult:
    return SearchResult(
        entry_id=entry_id, entry_date=date, text=text, score=1.0,
        matching_chunks=[], snippet=None,
    )


class _FakeQuery:
    def __init__(self, results: list[SearchResult]):
        self._results = results
        self.calls: list[dict] = []

    def search_entries(self, **kwargs):
        self.calls.append(kwargs)
        return self._results


class _FakeAnswerer:
    def __init__(self, result: AnswerResult | None = None, exc: Exception | None = None):
        self._result = result
        self._exc = exc
        self.history: list[ConversationTurn] | None = None

    def continue_conversation(self, history, passages):
        self.history = history
        if self._exc is not None:
            raise self._exc
        return self._result


def _service(repo, query, answerer, **kw) -> ConversationService:
    return ConversationService(
        repository=repo,
        query_service=query,
        answerer=answerer,
        model=kw.pop("model", "claude-sonnet-4-6"),
        context_entries=kw.pop("context_entries", 8),
        passage_chars=kw.pop("passage_chars", 800),
    )


def _seed(svc) -> int:
    conv = svc.start(
        USER_A,
        question="when did my back hurt?",
        answer="On 2026-02-14.",
        citations=[{"entry_id": 42, "entry_date": "2026-02-14", "snippet": "back"}],
    )
    return conv.id


def test_start_seeds_user_and_assistant_turns(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    svc = _service(repo, _FakeQuery([]), _FakeAnswerer())
    conv = svc.start(
        USER_A, question="x" * 200, answer="ans", citations=[],
    )
    assert len(conv.title) <= 120  # title trimmed
    assert [m.role for m in conv.messages] == ["user", "assistant"]
    assert conv.messages[1].content == "ans"


def test_reply_combines_query_and_persists_both_turns(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    query = _FakeQuery([_result(7, "2026-03-01", "Back better now.")])
    answerer = _FakeAnswerer(AnswerResult("Around 2026-03-01.", True, [7]))
    svc = _service(repo, query, answerer, context_entries=8)
    cid = _seed(svc)

    msg = svc.reply(USER_A, cid, "and when did it get better?")

    # combined query = original question + "\n" + follow-up
    assert query.calls[0]["query"] == (
        "when did my back hurt?\nand when did it get better?"
    )
    assert query.calls[0]["limit"] == 8
    assert query.calls[0]["user_id"] == USER_A
    # history passed to answerer ends with the new user turn
    assert answerer.history[-1].role == "user"
    assert answerer.history[-1].content == "and when did it get better?"
    # returned assistant message + resolved citation
    assert msg.role == "assistant"
    assert msg.content == "Around 2026-03-01."
    assert msg.citations[0]["entry_id"] == 7
    # both turns persisted
    reloaded = repo.get(cid, USER_A)
    assert [m.role for m in reloaded.messages] == [
        "user", "assistant", "user", "assistant"
    ]


def test_reply_drops_citations_not_in_results(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    query = _FakeQuery([_result(7, "2026-03-01", "t")])
    answerer = _FakeAnswerer(AnswerResult("x", True, [7, 999]))
    svc = _service(repo, query, answerer)
    cid = _seed(svc)
    msg = svc.reply(USER_A, cid, "follow up")
    assert [c["entry_id"] for c in msg.citations] == [7]


def test_reply_unavailable_persists_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    query = _FakeQuery([_result(7, "2026-03-01", "t")])
    answerer = _FakeAnswerer(exc=AnswerUnavailable("boom"))
    svc = _service(repo, query, answerer)
    cid = _seed(svc)
    with pytest.raises(AnswerUnavailable):
        svc.reply(USER_A, cid, "follow up")
    reloaded = repo.get(cid, USER_A)
    assert len(reloaded.messages) == 2  # unchanged — only the seed turns


def test_reply_missing_conversation_raises_not_found(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    svc = _service(repo, _FakeQuery([]), _FakeAnswerer())
    with pytest.raises(ConversationNotFoundError):
        svc.reply(USER_A, 99999, "hello?")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_services/test_conversations.py -q`
Expected: FAIL — module `services.conversations` does not exist.

- [ ] **Step 3: Write the service**

Create `src/journal/services/conversations.py`:

```python
"""Conversation service — start a thread from a Search answer, then chat.

A conversation is seeded from an answer the user already has (no second
synthesis call). Each ``reply`` re-grounds: it re-retrieves passages with
the combined ``original_question + "\\n" + follow-up`` query so new
specifics pull the right entries, hands the full thread to the multi-turn
answerer, and persists the user + assistant turns only after the LLM
succeeds — a failed reply leaves the thread consistent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from journal.db.conversation_repository import ConversationNotFound
from journal.providers.answerer import AnswerPassage, ConversationTurn

if TYPE_CHECKING:
    from journal.db.conversation_repository import SQLiteConversationRepository
    from journal.models import Conversation, ConversationMessage
    from journal.providers.answerer import Answerer
    from journal.services.query import QueryService

#: Conversation titles are the question text, trimmed.
_MAX_TITLE_CHARS = 120


class ConversationNotFoundError(Exception):  # noqa: N818
    """Raised when a conversation isn't found / not owned by the user.

    The route maps this to a 404.
    """


class ConversationService:
    def __init__(
        self,
        *,
        repository: SQLiteConversationRepository,
        query_service: QueryService,
        answerer: Answerer,
        model: str,
        context_entries: int = 8,
        passage_chars: int = 800,
    ) -> None:
        self._repo = repository
        self._query = query_service
        self._answerer = answerer
        self._model = model
        self._context_entries = context_entries
        self._passage_chars = passage_chars

    def start(
        self,
        user_id: int,
        question: str,
        answer: str,
        citations: list[dict],
    ) -> Conversation:
        title = question.strip()[:_MAX_TITLE_CHARS]
        seed = [
            {"role": "user", "content": question, "citations": []},
            {"role": "assistant", "content": answer, "citations": citations or []},
        ]
        return self._repo.create(user_id, title, seed)

    def reply(
        self,
        user_id: int,
        conversation_id: int,
        message: str,
    ) -> ConversationMessage:
        conv = self._repo.get(conversation_id, user_id)
        if conv is None:
            raise ConversationNotFoundError(
                f"conversation {conversation_id} not found"
            )

        original_question = conv.messages[0].content if conv.messages else message
        results = self._query.search_entries(
            query=f"{original_question}\n{message}",
            limit=self._context_entries,
            offset=0,
            user_id=user_id,
        )

        history = [
            ConversationTurn(role=m.role, content=m.content) for m in conv.messages
        ]
        history.append(ConversationTurn(role="user", content=message))

        passages = [
            AnswerPassage(
                entry_id=r.entry_id,
                entry_date=r.entry_date,
                text=r.text[: self._passage_chars],
            )
            for r in results
        ]
        # Propagates AnswerUnavailable; nothing is persisted on failure.
        result = self._answerer.continue_conversation(history, passages)

        by_id = {r.entry_id: r for r in results}
        citations = [
            {
                "entry_id": eid,
                "entry_date": by_id[eid].entry_date,
                "snippet": by_id[eid].text[:160].strip(),
            }
            for eid in result.cited_entry_ids
            if eid in by_id
        ]

        try:
            added = self._repo.add_messages(
                conversation_id,
                user_id,
                [
                    {"role": "user", "content": message, "citations": []},
                    {
                        "role": "assistant",
                        "content": result.answer,
                        "citations": citations,
                    },
                ],
            )
        except ConversationNotFound as e:  # raced delete between get and add
            raise ConversationNotFoundError(str(e)) from e

        return added[-1]  # the assistant turn

    def list(self, user_id: int) -> list[Conversation]:
        return self._repo.list(user_id)

    def get(self, user_id: int, conversation_id: int) -> Conversation | None:
        return self._repo.get(conversation_id, user_id)

    def delete(self, user_id: int, conversation_id: int) -> bool:
        return self._repo.delete(conversation_id, user_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_services/test_conversations.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/journal/services/conversations.py tests/test_services/test_conversations.py
git commit -m "feat(service): ConversationService (start + re-grounding reply)"
```

---

## Task 6: `api/conversations.py` REST routes

**Files:**
- Create: `src/journal/api/conversations.py`
- Modify: `src/journal/api/__init__.py`
- Test: `tests/test_api_conversations.py`

Routes (all bearer-auth via `get_authenticated_user`; all scoped to that user; not-found / other-user → `404 not_found`):

| Method & path | Body | Returns |
|---|---|---|
| `POST /api/conversations` | `{question, answer, citations?}` | `201` conversation (id, title, messages) |
| `GET /api/conversations` | — | `{conversations: [{id, title, updated_at, message_count}]}` |
| `GET /api/conversations/{id}` | — | conversation with `messages` |
| `POST /api/conversations/{id}/messages` | `{message}` | `201` the assistant `ConversationMessage` |
| `DELETE /api/conversations/{id}` | — | `204` |

Message JSON shape: `{id, role, content, citations:[{entry_id,entry_date,snippet}], created_at}`. Errors: `400 missing_question` / `400 missing_message`; `404 not_found`; `502 answer_unavailable`; `503` if the service isn't wired.

- [ ] **Step 1: Write the failing test**

```python
"""REST API tests for the conversations endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from journal.api.conversations import register_conversations_routes
from journal.auth import AuthenticatedUser, _current_user_id
from journal.db.conversation_repository import SQLiteConversationRepository
from journal.db.factory import ConnectionFactory
from journal.models import SearchResult
from journal.providers.answerer import AnswerResult, AnswerUnavailable
from journal.services.conversations import ConversationService

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

_TEST_USER_ID = 1


class _FakeAuthMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            scope["user"] = AuthenticatedUser(
                user_id=_TEST_USER_ID, email="t@example.com",
                display_name="T", is_admin=False, is_active=True,
                email_verified=True,
            )
            token = _current_user_id.set(_TEST_USER_ID)
            try:
                await self.app(scope, receive, send)
            finally:
                _current_user_id.reset(token)
        else:
            await self.app(scope, receive, send)


class _FakeQuery:
    def __init__(self, results: list[SearchResult]):
        self._results = results

    def search_entries(self, **kwargs):
        return self._results


class _FakeAnswerer:
    def __init__(self, result: AnswerResult | None = None, exc: Exception | None = None):
        self._result = result
        self._exc = exc

    def continue_conversation(self, history, passages):
        if self._exc is not None:
            raise self._exc
        return self._result


def _result(entry_id: int, date: str, text: str) -> SearchResult:
    return SearchResult(
        entry_id=entry_id, entry_date=date, text=text, score=1.0,
        matching_chunks=[], snippet=None,
    )


def _make_client(answerer) -> tuple[TestClient, ConversationService]:
    factory = ConnectionFactory(":memory:")
    from journal.db.migrations import run_migrations
    run_migrations(factory.get())
    repo = SQLiteConversationRepository(factory)
    svc = ConversationService(
        repository=repo,
        query_service=_FakeQuery([_result(7, "2026-03-01", "Better now.")]),
        answerer=answerer,
        model="claude-sonnet-4-6",
    )
    services: dict[str, Any] = {"conversation": svc}
    mcp = FastMCP("test-conversations")
    register_conversations_routes(mcp, lambda: services)
    app = mcp.streamable_http_app()
    app.add_middleware(_FakeAuthMiddleware)
    return TestClient(app), svc


@pytest.fixture
def client() -> Generator[TestClient]:
    c, _ = _make_client(_FakeAnswerer(AnswerResult("Around 2026-03-01.", True, [7])))
    yield c


def _create(client: TestClient) -> dict:
    resp = client.post(
        "/api/conversations",
        json={
            "question": "when did my back hurt?",
            "answer": "On 2026-02-14.",
            "citations": [
                {"entry_id": 42, "entry_date": "2026-02-14", "snippet": "back"}
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_returns_seeded_conversation(client: TestClient) -> None:
    body = _create(client)
    assert body["title"] == "when did my back hurt?"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]


def test_create_requires_question(client: TestClient) -> None:
    resp = client.post("/api/conversations", json={"question": "", "answer": "x"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "missing_question"


def test_list_returns_summaries(client: TestClient) -> None:
    _create(client)
    resp = client.get("/api/conversations")
    assert resp.status_code == 200
    convs = resp.json()["conversations"]
    assert convs[0]["message_count"] == 2
    assert "messages" not in convs[0]


def test_get_returns_messages(client: TestClient) -> None:
    cid = _create(client)["id"]
    resp = client.get(f"/api/conversations/{cid}")
    assert resp.status_code == 200
    assert len(resp.json()["messages"]) == 2


def test_get_other_id_is_404(client: TestClient) -> None:
    resp = client.get("/api/conversations/99999")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_reply_appends_assistant_turn(client: TestClient) -> None:
    cid = _create(client)["id"]
    resp = client.post(
        f"/api/conversations/{cid}/messages",
        json={"message": "and when did it get better?"},
    )
    assert resp.status_code == 201, resp.text
    msg = resp.json()
    assert msg["role"] == "assistant"
    assert msg["content"] == "Around 2026-03-01."
    assert msg["citations"][0]["entry_id"] == 7


def test_reply_requires_message(client: TestClient) -> None:
    cid = _create(client)["id"]
    resp = client.post(f"/api/conversations/{cid}/messages", json={"message": "  "})
    assert resp.status_code == 400
    assert resp.json()["error"] == "missing_message"


def test_reply_missing_conversation_is_404(client: TestClient) -> None:
    resp = client.post(
        "/api/conversations/99999/messages", json={"message": "hi?"}
    )
    assert resp.status_code == 404


def test_reply_unavailable_is_502() -> None:
    c, _ = _make_client(_FakeAnswerer(exc=AnswerUnavailable("boom")))
    create = c.post(
        "/api/conversations",
        json={"question": "q", "answer": "a", "citations": []},
    )
    cid = create.json()["id"]
    resp = c.post(f"/api/conversations/{cid}/messages", json={"message": "x"})
    assert resp.status_code == 502
    assert resp.json()["error"] == "answer_unavailable"


def test_delete_removes_conversation(client: TestClient) -> None:
    cid = _create(client)["id"]
    resp = client.delete(f"/api/conversations/{cid}")
    assert resp.status_code == 204
    assert client.get(f"/api/conversations/{cid}").status_code == 404


def test_delete_other_id_is_404(client: TestClient) -> None:
    resp = client.delete("/api/conversations/99999")
    assert resp.status_code == 404


def test_service_unwired_is_503() -> None:
    mcp = FastMCP("test-unwired")
    register_conversations_routes(mcp, lambda: {})
    app = mcp.streamable_http_app()
    app.add_middleware(_FakeAuthMiddleware)
    client = TestClient(app)
    resp = client.get("/api/conversations")
    assert resp.status_code == 503
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_conversations.py -q`
Expected: FAIL — module `api.conversations` does not exist.

- [ ] **Step 3: Write the route module**

Create `src/journal/api/conversations.py`:

```python
"""Conversations REST routes.

CRUD for persisted chat threads about a journal answer. Reply synthesis
re-grounds against the journal and is delegated to ``ConversationService``.
All routes are bearer-authenticated and user-scoped; another user's id is
indistinguishable from a missing one (``404 not_found``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse, Response

from journal.api._handler import handler
from journal.auth import get_authenticated_user
from journal.providers.answerer import AnswerUnavailable
from journal.services.conversations import ConversationNotFoundError

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.models import Conversation, ConversationMessage
    from journal.service_registry import ServicesDict

log = logging.getLogger(__name__)


def _message_dict(m: ConversationMessage) -> dict[str, Any]:
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "citations": m.citations,
        "created_at": m.created_at,
    }


def _conversation_dict(c: Conversation) -> dict[str, Any]:
    return {
        "id": c.id,
        "title": c.title,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
        "messages": [_message_dict(m) for m in c.messages],
    }


def register_conversations_routes(
    mcp: FastMCP,
    services_getter: Callable[[], ServicesDict | None],
) -> None:
    """Register the /api/conversations routes."""

    @mcp.custom_route(
        "/api/conversations", methods=["POST"], name="api_conversations_create"
    )
    @handler(services_getter, parse_json=True)
    def create_conversation(
        request: Request, services: ServicesDict, body: dict
    ) -> JSONResponse:
        svc = services.get("conversation")
        if svc is None:
            return JSONResponse(
                {"error": "service_unavailable", "message": "Conversations not configured."},
                status_code=503,
            )
        user_id = get_authenticated_user(request).user_id
        question = (body.get("question") or "").strip()
        if not question:
            return JSONResponse(
                {"error": "missing_question", "message": "'question' is required"},
                status_code=400,
            )
        answer = body.get("answer") or ""
        citations = body.get("citations") or []
        conv = svc.start(user_id, question, answer, citations)
        return JSONResponse(_conversation_dict(conv), status_code=201)

    @mcp.custom_route(
        "/api/conversations", methods=["GET"], name="api_conversations_list"
    )
    @handler(services_getter)
    def list_conversations(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        svc = services.get("conversation")
        if svc is None:
            return JSONResponse(
                {"error": "service_unavailable", "message": "Conversations not configured."},
                status_code=503,
            )
        user_id = get_authenticated_user(request).user_id
        convs = svc.list(user_id)
        return JSONResponse(
            {
                "conversations": [
                    {
                        "id": c.id,
                        "title": c.title,
                        "updated_at": c.updated_at,
                        "message_count": c.message_count,
                    }
                    for c in convs
                ]
            }
        )

    @mcp.custom_route(
        "/api/conversations/{conversation_id}",
        methods=["GET"],
        name="api_conversations_detail",
    )
    @handler(services_getter)
    def conversation_detail(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        svc = services.get("conversation")
        if svc is None:
            return JSONResponse(
                {"error": "service_unavailable", "message": "Conversations not configured."},
                status_code=503,
            )
        user_id = get_authenticated_user(request).user_id
        cid = int(request.path_params["conversation_id"])
        conv = svc.get(user_id, cid)
        if conv is None:
            return JSONResponse(
                {"error": "not_found", "message": "Conversation not found"},
                status_code=404,
            )
        return JSONResponse(_conversation_dict(conv))

    @mcp.custom_route(
        "/api/conversations/{conversation_id}/messages",
        methods=["POST"],
        name="api_conversations_reply",
    )
    @handler(services_getter, parse_json=True)
    def reply_conversation(
        request: Request, services: ServicesDict, body: dict
    ) -> JSONResponse:
        svc = services.get("conversation")
        if svc is None:
            return JSONResponse(
                {"error": "service_unavailable", "message": "Conversations not configured."},
                status_code=503,
            )
        user_id = get_authenticated_user(request).user_id
        cid = int(request.path_params["conversation_id"])
        message = (body.get("message") or "").strip()
        if not message:
            return JSONResponse(
                {"error": "missing_message", "message": "'message' is required"},
                status_code=400,
            )
        try:
            msg = svc.reply(user_id, cid, message)
        except ConversationNotFoundError:
            return JSONResponse(
                {"error": "not_found", "message": "Conversation not found"},
                status_code=404,
            )
        except AnswerUnavailable as e:
            log.info("conversation reply unavailable for %s: %s", cid, e)
            return JSONResponse(
                {
                    "error": "answer_unavailable",
                    "message": "Could not generate a reply right now.",
                },
                status_code=502,
            )
        return JSONResponse(_message_dict(msg), status_code=201)

    @mcp.custom_route(
        "/api/conversations/{conversation_id}",
        methods=["DELETE"],
        name="api_conversations_delete",
    )
    @handler(services_getter)
    def delete_conversation(
        request: Request, services: ServicesDict, body: None
    ) -> Response:
        svc = services.get("conversation")
        if svc is None:
            return JSONResponse(
                {"error": "service_unavailable", "message": "Conversations not configured."},
                status_code=503,
            )
        user_id = get_authenticated_user(request).user_id
        cid = int(request.path_params["conversation_id"])
        if not svc.delete(user_id, cid):
            return JSONResponse(
                {"error": "not_found", "message": "Conversation not found"},
                status_code=404,
            )
        return Response(status_code=204)
```

- [ ] **Step 4: Register the routes in `api/__init__.py`**

Add the import next to the other `from journal.api.* import register_*` lines:

```python
from journal.api.conversations import register_conversations_routes
```

And add the call inside `register_api_routes(...)` body (e.g. right after `register_search_routes(mcp, services_getter)`):

```python
    register_conversations_routes(mcp, services_getter)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_conversations.py -q`
Expected: PASS (12 tests).

- [ ] **Step 6: Commit**

```bash
git add src/journal/api/conversations.py src/journal/api/__init__.py tests/test_api_conversations.py
git commit -m "feat(api): /api/conversations CRUD + reply routes"
```

---

## Task 7: Wiring + config (`service_registry` + `bootstrap`)

**Files:**
- Modify: `src/journal/service_registry.py`
- Modify: `src/journal/mcp_server/bootstrap.py`
- Test: `tests/test_bootstrap_conversations.py`

- [ ] **Step 1: Write the failing test**

```python
"""Bootstrap wires the conversation service into the services dict."""

from __future__ import annotations

from pathlib import Path


def test_bootstrap_wires_conversation_service(tmp_path: Path, monkeypatch) -> None:
    # Run the real service init with answer synthesis disabled so no API
    # keys are needed; the conversation service must still be wired with
    # the NoopAnswerer.
    monkeypatch.setenv("ANSWER_PROVIDER", "none")
    monkeypatch.setenv("JOURNAL_DB_PATH", str(tmp_path / "boot.db"))
    monkeypatch.setenv("CHROMADB_HOST", "localhost")

    from journal.config import Config
    from journal.mcp_server.bootstrap import _init_services

    services = _init_services(Config())
    try:
        assert services.get("conversation") is not None
        assert services.get("conversation_repository") is not None
    finally:
        runner = services.get("job_runner")
        if runner is not None:
            runner.shutdown(wait=True, cancel_futures=False)
```

> Note: if `_init_services` requires extra env or a different entry signature, mirror the closest existing bootstrap test (search `tests/` for `_init_services`); the assertions on the two new keys are the point of this test. Keep the ThreadPoolExecutor teardown in `finally` to avoid CI segfaults.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_bootstrap_conversations.py -q`
Expected: FAIL — `services.get("conversation")` is `None`.

- [ ] **Step 3: Add the registry keys**

In `src/journal/service_registry.py`, add to the `TYPE_CHECKING` imports:

```python
    from journal.db.conversation_repository import SQLiteConversationRepository
    from journal.services.conversations import ConversationService
```

And add to `class ServicesDict` (near `answer: AnswerService`):

```python
    conversation: ConversationService
    conversation_repository: SQLiteConversationRepository
```

- [ ] **Step 4: Construct + register in `bootstrap.py`**

Add the import near the other service imports:

```python
from journal.db.conversation_repository import SQLiteConversationRepository
from journal.services.conversations import ConversationService
```

Immediately after the `answer_service = AnswerService(...)` block (around line 765-771), add:

```python
    conversation_repository = SQLiteConversationRepository(db_factory)
    conversation_service = ConversationService(
        repository=conversation_repository,
        query_service=query_service,
        answerer=answerer,
        model=config.answer_model,
        context_entries=config.answer_context_entries,
    )
```

And add both to the `_services = { ... }` dict (next to `"answer": answer_service,`):

```python
        "conversation": conversation_service,
        "conversation_repository": conversation_repository,
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_bootstrap_conversations.py -q`
Expected: PASS.

- [ ] **Step 6: Run the full server unit suite + lint**

Run: `uv run pytest -m "not integration" -q && uv run ruff check src/ tests/`
Expected: all green, ruff clean.

- [ ] **Step 7: Commit**

```bash
git add src/journal/service_registry.py src/journal/mcp_server/bootstrap.py tests/test_bootstrap_conversations.py
git commit -m "feat(bootstrap): wire ConversationService + repository"
```

---

## Task 8: Server docs + journal

**Files:**
- Create: `docs/conversations.md`
- Modify: `docs/search.md` (link from the answer section)
- Create: `journal/260617-conversations.md`

- [ ] **Step 1: Write `docs/conversations.md`**

Cover: the data model (two tables, user-scoping, cascade), the five endpoints with request/response shapes (copy the table from the spec), the re-grounding behavior (combined query, persist-on-success), reuse of the answerer + answer config (`ANSWER_MODEL` / `ANSWER_CONTEXT_ENTRIES`, no new env vars), and the out-of-scope list (no streaming, no LLM titles, no branching). Keep it concise.

- [ ] **Step 2: Link from `docs/search.md`**

In the answer-synthesis section of `docs/search.md`, add a sentence + link: "Answers can be continued as a chat — see [conversations.md](conversations.md)."

- [ ] **Step 3: Write the journal entry `journal/260617-conversations.md`**

Capture: the decisions (seed from existing answer, re-search per reply, user-scoped persistence, persist-on-success), the new files, and that the answerer/QueryService/answer config were reused rather than adding new knobs.

- [ ] **Step 4: Commit**

```bash
git add docs/conversations.md docs/search.md journal/260617-conversations.md
git commit -m "docs(conversations): data model, endpoints, grounding + journal"
```

---

## Task 9: Webapp — types (`types/conversation.ts`)

> From here on, all commands run inside `webapp/`. First create the branch:
> `git checkout -b feat/conversations` (from `main`).

**Files:**
- Create: `src/types/conversation.ts`

- [ ] **Step 1: Create the types** (no test needed — pure types)

```typescript
import type { AnswerCitation } from './search'

export interface ConversationMessage {
  id: number
  role: 'user' | 'assistant'
  content: string
  citations: AnswerCitation[]
  created_at: string
}

export interface ConversationSummary {
  id: number
  title: string
  updated_at: string
  message_count: number
}

export interface Conversation {
  id: number
  title: string
  created_at: string
  updated_at: string
  messages: ConversationMessage[]
}

export interface StartConversationParams {
  question: string
  answer: string
  citations: AnswerCitation[]
}
```

- [ ] **Step 2: Type-check**

Run: `npx vue-tsc --noEmit` (or rely on the build step in a later task).
Expected: no new type errors.

- [ ] **Step 3: Commit**

```bash
git add src/types/conversation.ts
git commit -m "feat(types): conversation types"
```

---

## Task 10: Webapp — API client (`api/conversations.ts`)

**Files:**
- Create: `src/api/conversations.ts`
- Test: `src/api/__tests__/conversations.test.ts`

- [ ] **Step 1: Write the failing test**

```typescript
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('@/api/client', () => ({
  apiFetch: vi.fn(),
}))

import { apiFetch } from '@/api/client'
import {
  createConversation,
  listConversations,
  getConversation,
  sendMessage,
  deleteConversation,
} from '../conversations'

const mockFetch = vi.mocked(apiFetch)

describe('conversations api client', () => {
  beforeEach(() => vi.clearAllMocks())

  it('createConversation POSTs the seed', async () => {
    mockFetch.mockResolvedValue({ id: 1, title: 'q', messages: [] } as never)
    await createConversation({ question: 'q', answer: 'a', citations: [] })
    expect(mockFetch).toHaveBeenCalledWith('/api/conversations', {
      method: 'POST',
      body: JSON.stringify({ question: 'q', answer: 'a', citations: [] }),
    })
  })

  it('listConversations GETs the list', async () => {
    mockFetch.mockResolvedValue({ conversations: [] } as never)
    await listConversations()
    expect(mockFetch).toHaveBeenCalledWith('/api/conversations')
  })

  it('getConversation GETs by id', async () => {
    mockFetch.mockResolvedValue({ id: 3, messages: [] } as never)
    await getConversation(3)
    expect(mockFetch).toHaveBeenCalledWith('/api/conversations/3')
  })

  it('sendMessage POSTs the message', async () => {
    mockFetch.mockResolvedValue({ id: 9, role: 'assistant' } as never)
    await sendMessage(3, 'hi?')
    expect(mockFetch).toHaveBeenCalledWith('/api/conversations/3/messages', {
      method: 'POST',
      body: JSON.stringify({ message: 'hi?' }),
    })
  })

  it('deleteConversation DELETEs by id', async () => {
    mockFetch.mockResolvedValue(undefined as never)
    await deleteConversation(3)
    expect(mockFetch).toHaveBeenCalledWith('/api/conversations/3', {
      method: 'DELETE',
    })
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test:unit -- src/api/__tests__/conversations.test.ts`
Expected: FAIL — module `../conversations` not found.

- [ ] **Step 3: Write the client**

```typescript
import type {
  Conversation,
  ConversationMessage,
  ConversationSummary,
  StartConversationParams,
} from '@/types/conversation'
import { apiFetch } from './client'

export function createConversation(
  params: StartConversationParams,
): Promise<Conversation> {
  return apiFetch<Conversation>('/api/conversations', {
    method: 'POST',
    body: JSON.stringify({
      question: params.question,
      answer: params.answer,
      citations: params.citations,
    }),
  })
}

export function listConversations(): Promise<{
  conversations: ConversationSummary[]
}> {
  return apiFetch<{ conversations: ConversationSummary[] }>(
    '/api/conversations',
  )
}

export function getConversation(id: number): Promise<Conversation> {
  return apiFetch<Conversation>(`/api/conversations/${id}`)
}

export function sendMessage(
  id: number,
  message: string,
): Promise<ConversationMessage> {
  return apiFetch<ConversationMessage>(`/api/conversations/${id}/messages`, {
    method: 'POST',
    body: JSON.stringify({ message }),
  })
}

export function deleteConversation(id: number): Promise<void> {
  return apiFetch<void>(`/api/conversations/${id}`, { method: 'DELETE' })
}
```

> Verify the exact `apiFetch` call shape against `src/api/search.ts` before finalizing (POST builds `{ method, body: JSON.stringify(...) }`; GET passes only the path). Adjust if the house helper differs.

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test:unit -- src/api/__tests__/conversations.test.ts`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/api/conversations.ts src/api/__tests__/conversations.test.ts
git commit -m "feat(api): conversations client"
```

---

## Task 11: Webapp — Pinia store (`stores/conversations.ts`)

**Files:**
- Create: `src/stores/conversations.ts`
- Test: `src/stores/__tests__/conversations.test.ts`

State: current conversation (`conversation`, `messages`, `sending`, `error`) + list (`summaries`, `listLoading`). Actions: `open(id)`, `loadList()`, `start(seed)` (returns new id), `reply(message)`, `remove(id)`. Mirror the error-handling pattern in `stores/search.ts` (ApiRequestError 5xx → friendly message; 4xx → server message).

- [ ] **Step 1: Write the failing test**

```typescript
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('@/api/conversations', () => ({
  createConversation: vi.fn(),
  listConversations: vi.fn(),
  getConversation: vi.fn(),
  sendMessage: vi.fn(),
  deleteConversation: vi.fn(),
}))

import {
  createConversation,
  listConversations,
  getConversation,
  sendMessage,
  deleteConversation,
} from '@/api/conversations'
import { useConversationsStore } from '../conversations'

const mCreate = vi.mocked(createConversation)
const mList = vi.mocked(listConversations)
const mGet = vi.mocked(getConversation)
const mSend = vi.mocked(sendMessage)
const mDelete = vi.mocked(deleteConversation)

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

it('start creates a conversation and returns its id', async () => {
  mCreate.mockResolvedValue({
    id: 5, title: 'q', created_at: 't', updated_at: 't',
    messages: [{ id: 1, role: 'user', content: 'q', citations: [], created_at: 't' }],
  })
  const store = useConversationsStore()
  const id = await store.start({ question: 'q', answer: 'a', citations: [] })
  expect(id).toBe(5)
  expect(store.messages).toHaveLength(1)
})

it('open loads a conversation', async () => {
  mGet.mockResolvedValue({
    id: 5, title: 'q', created_at: 't', updated_at: 't',
    messages: [
      { id: 1, role: 'user', content: 'q', citations: [], created_at: 't' },
      { id: 2, role: 'assistant', content: 'a', citations: [], created_at: 't' },
    ],
  })
  const store = useConversationsStore()
  await store.open(5)
  expect(store.conversation?.id).toBe(5)
  expect(store.messages).toHaveLength(2)
})

it('reply appends the assistant turn', async () => {
  mGet.mockResolvedValue({
    id: 5, title: 'q', created_at: 't', updated_at: 't',
    messages: [{ id: 1, role: 'user', content: 'q', citations: [], created_at: 't' }],
  })
  mSend.mockResolvedValue({
    id: 9, role: 'assistant', content: 'reply', citations: [], created_at: 't',
  })
  const store = useConversationsStore()
  await store.open(5)
  await store.reply('follow up')
  // optimistic user turn + assistant turn appended
  const roles = store.messages.map((m) => m.role)
  expect(roles).toEqual(['user', 'user', 'assistant'])
  expect(store.messages.at(-1)?.content).toBe('reply')
  expect(store.sending).toBe(false)
})

it('reply surfaces an error and clears sending', async () => {
  mGet.mockResolvedValue({
    id: 5, title: 'q', created_at: 't', updated_at: 't', messages: [],
  })
  mSend.mockRejectedValue(new Error('boom'))
  const store = useConversationsStore()
  await store.open(5)
  await store.reply('x')
  expect(store.error).toBeTruthy()
  expect(store.sending).toBe(false)
})

it('loadList populates summaries', async () => {
  mList.mockResolvedValue({
    conversations: [{ id: 1, title: 't', updated_at: 't', message_count: 2 }],
  })
  const store = useConversationsStore()
  await store.loadList()
  expect(store.summaries).toHaveLength(1)
})

it('remove deletes and drops the summary', async () => {
  mList.mockResolvedValue({
    conversations: [{ id: 1, title: 't', updated_at: 't', message_count: 2 }],
  })
  mDelete.mockResolvedValue()
  const store = useConversationsStore()
  await store.loadList()
  await store.remove(1)
  expect(store.summaries).toHaveLength(0)
  expect(mDelete).toHaveBeenCalledWith(1)
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test:unit -- src/stores/__tests__/conversations.test.ts`
Expected: FAIL — store module not found.

- [ ] **Step 3: Write the store**

```typescript
import { defineStore } from 'pinia'
import { ref } from 'vue'
import type {
  Conversation,
  ConversationMessage,
  ConversationSummary,
  StartConversationParams,
} from '@/types/conversation'
import {
  createConversation,
  deleteConversation,
  getConversation,
  listConversations,
  sendMessage,
} from '@/api/conversations'
import { ApiRequestError } from '@/api/client'

function friendlyError(e: unknown, fallback: string): string {
  if (e instanceof ApiRequestError) {
    return e.status >= 500
      ? 'The assistant is temporarily unavailable — please try again.'
      : e.message
  }
  if (e instanceof Error) return e.message
  return fallback
}

export const useConversationsStore = defineStore('conversations', () => {
  const conversation = ref<Conversation | null>(null)
  const messages = ref<ConversationMessage[]>([])
  const sending = ref(false)
  const error = ref<string | null>(null)

  const summaries = ref<ConversationSummary[]>([])
  const listLoading = ref(false)

  async function start(seed: StartConversationParams): Promise<number> {
    const conv = await createConversation(seed)
    conversation.value = conv
    messages.value = conv.messages
    return conv.id
  }

  async function open(id: number): Promise<void> {
    error.value = null
    const conv = await getConversation(id)
    conversation.value = conv
    messages.value = conv.messages
  }

  async function reply(message: string): Promise<void> {
    if (!conversation.value) return
    error.value = null
    sending.value = true
    // Optimistic user turn so the thread updates immediately.
    messages.value.push({
      id: -Date.now(),
      role: 'user',
      content: message,
      citations: [],
      created_at: new Date().toISOString(),
    })
    try {
      const assistant = await sendMessage(conversation.value.id, message)
      messages.value.push(assistant)
    } catch (e) {
      error.value = friendlyError(e, 'Failed to send message.')
    } finally {
      sending.value = false
    }
  }

  async function loadList(): Promise<void> {
    listLoading.value = true
    try {
      const res = await listConversations()
      summaries.value = res.conversations
    } finally {
      listLoading.value = false
    }
  }

  async function remove(id: number): Promise<void> {
    await deleteConversation(id)
    summaries.value = summaries.value.filter((c) => c.id !== id)
  }

  return {
    conversation,
    messages,
    sending,
    error,
    summaries,
    listLoading,
    start,
    open,
    reply,
    loadList,
    remove,
  }
})
```

> Verify `ApiRequestError` is exported from `@/api/client` (recon confirmed it is). Match `friendlyError` wording to `stores/search.ts` if it has an established phrase.

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test:unit -- src/stores/__tests__/conversations.test.ts`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/stores/conversations.ts src/stores/__tests__/conversations.test.ts
git commit -m "feat(store): conversations store"
```

---

## Task 12: Webapp — router + sidebar nav

**Files:**
- Modify: `src/router/index.ts`
- Modify: `src/components/layout/AppSidebar.vue`
- Test: `src/router/__tests__/conversations-routes.test.ts` (light assertion that the routes resolve)

- [ ] **Step 1: Write the failing test**

```typescript
import { describe, it, expect } from 'vitest'
import { router } from '@/router'

describe('conversations routes', () => {
  it('resolves the list route', () => {
    const r = router.resolve('/conversations')
    expect(r.name).toBe('conversations')
  })
  it('resolves the detail route', () => {
    const r = router.resolve('/conversations/5')
    expect(r.name).toBe('conversation')
    expect(r.params.id).toBe('5')
  })
})
```

> If `src/router/index.ts` does not export `router` as a named export, import the default and assert `router.resolve` the same way (check the existing file).

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test:unit -- src/router/__tests__/conversations-routes.test.ts`
Expected: FAIL — route name `conversations` is undefined.

- [ ] **Step 3: Add the routes**

In `src/router/index.ts`, add to the routes array (alongside the other authenticated routes, e.g. near `/search`):

```typescript
  {
    path: '/conversations',
    name: 'conversations',
    component: () => import('@/views/ConversationListView.vue'),
  },
  {
    path: '/conversations/:id',
    name: 'conversation',
    component: () => import('@/views/ConversationView.vue'),
  },
```

- [ ] **Step 4: Add the sidebar nav item**

In `src/components/layout/AppSidebar.vue`, copy the existing Search `RouterLink` block (custom slot pattern) and adapt for Conversations — `to="/conversations"`, `data-testid="sidebar-conversations-link"`, label `Conversations`, and a chat-bubble icon. Place it next to the Search item:

```vue
<RouterLink v-slot="{ href, navigate, isActive }" to="/conversations" custom>
  <li
    class="pl-4 pr-3 py-2 rounded-lg mb-0.5 last:mb-0 bg-linear-to-r"
    :class="isActive && 'from-violet-500/[0.12] dark:from-violet-500/[0.24] to-violet-500/[0.04]'"
  >
    <a
      :href="href"
      class="block truncate transition"
      data-testid="sidebar-conversations-link"
      :class="isActive ? 'text-gray-900 dark:text-white' : 'text-gray-800 dark:text-gray-100 hover:text-gray-900 dark:hover:text-white'"
      @click="navigate"
    >
      <div class="flex items-center">
        <svg class="shrink-0 fill-current" width="16" height="16" viewBox="0 0 16 16">
          <path d="M8 0C3.6 0 0 3.1 0 7c0 1.9.9 3.6 2.3 4.9L1 16l4.6-1.4C6.4 14.9 7.2 15 8 15c4.4 0 8-3.1 8-7s-3.6-8-8-8Z" />
        </svg>
        <span class="text-sm font-medium ml-4 lg:opacity-0 lg:sidebar-expanded:opacity-100 2xl:opacity-100 duration-200">
          Conversations
        </span>
      </div>
    </a>
  </li>
</RouterLink>
```

> Match the exact wrapper element + classes of the sibling Search item in the actual file; the icon path above is a placeholder chat bubble — keep `viewBox`/sizing consistent with neighbors.

- [ ] **Step 5: Run test to verify it passes**

Run: `npm run test:unit -- src/router/__tests__/conversations-routes.test.ts`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/router/index.ts src/components/layout/AppSidebar.vue src/router/__tests__/conversations-routes.test.ts
git commit -m "feat(nav): conversations routes + sidebar item"
```

---

## Task 13: Webapp — `ConversationView`

**Files:**
- Create: `src/views/ConversationView.vue`
- Test: `src/views/__tests__/ConversationView.test.ts`

Header (title + back to Search), message list — user bubbles (right), assistant bubbles (left) with citation chips (`RouterLink` to `/entries/:id`), a "Thinking…" placeholder while `sending`, sticky input + Send (Enter submits; disabled while sending or empty). Reads the route `id` param on mount and calls `store.open(id)`.

- [ ] **Step 1: Write the failing test**

```typescript
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises, enableAutoUnmount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'
import ConversationView from '../ConversationView.vue'

vi.mock('@/api/conversations', () => ({
  getConversation: vi.fn(),
  sendMessage: vi.fn(),
  createConversation: vi.fn(),
  listConversations: vi.fn(),
  deleteConversation: vi.fn(),
}))

import { getConversation, sendMessage } from '@/api/conversations'
const mGet = vi.mocked(getConversation)
const mSend = vi.mocked(sendMessage)

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/conversations/:id', name: 'conversation', component: ConversationView },
    { path: '/search', name: 'search', component: { template: '<div/>' } },
    { path: '/entries/:id', name: 'entry-detail', component: { template: '<div/>' } },
  ],
})

async function mountAt(id: number) {
  router.push(`/conversations/${id}`)
  await router.isReady()
  return mount(ConversationView, { global: { plugins: [createPinia(), router] } })
}

describe('ConversationView', () => {
  enableAutoUnmount(beforeEach)
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders the turns with citation chips', async () => {
    mGet.mockResolvedValue({
      id: 5, title: 'when did my back hurt?', created_at: 't', updated_at: 't',
      messages: [
        { id: 1, role: 'user', content: 'when did my back hurt?', citations: [], created_at: 't' },
        {
          id: 2, role: 'assistant', content: 'On 2026-02-14.',
          citations: [{ entry_id: 42, entry_date: '2026-02-14', snippet: 'back' }],
          created_at: 't',
        },
      ],
    })
    const wrapper = await mountAt(5)
    await flushPromises()
    expect(wrapper.text()).toContain('On 2026-02-14.')
    const chip = wrapper.find('[data-testid="message-citation"]')
    expect(chip.exists()).toBe(true)
    expect(chip.attributes('href')).toContain('/entries/42')
  })

  it('sending a message calls reply', async () => {
    mGet.mockResolvedValue({
      id: 5, title: 'q', created_at: 't', updated_at: 't', messages: [],
    })
    mSend.mockResolvedValue({
      id: 9, role: 'assistant', content: 'reply', citations: [], created_at: 't',
    })
    const wrapper = await mountAt(5)
    await flushPromises()
    await wrapper.find('[data-testid="conversation-input"]').setValue('follow up')
    await wrapper.find('[data-testid="conversation-form"]').trigger('submit')
    await flushPromises()
    expect(mSend).toHaveBeenCalledWith(5, 'follow up')
    expect(wrapper.text()).toContain('reply')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test:unit -- src/views/__tests__/ConversationView.test.ts`
Expected: FAIL — view not found.

- [ ] **Step 3: Write the view**

```vue
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, RouterLink } from 'vue-router'
import { useConversationsStore } from '@/stores/conversations'

const route = useRoute()
const store = useConversationsStore()
const draft = ref('')

const conversationId = computed(() => Number(route.params.id))

async function load() {
  await store.open(conversationId.value)
}

onMounted(load)
watch(conversationId, load)

async function submit() {
  const text = draft.value.trim()
  if (!text || store.sending) return
  draft.value = ''
  await store.reply(text)
}
</script>

<template>
  <div class="max-w-3xl mx-auto px-4 py-6 flex flex-col h-full" data-testid="conversation-view">
    <header class="mb-4 flex items-center justify-between">
      <h1 class="text-lg font-semibold text-gray-900 dark:text-white truncate">
        {{ store.conversation?.title ?? 'Conversation' }}
      </h1>
      <RouterLink to="/search" class="text-sm text-violet-600 dark:text-violet-300">
        ← Back to Search
      </RouterLink>
    </header>

    <div class="flex-1 space-y-3 overflow-y-auto" data-testid="conversation-messages">
      <div
        v-for="m in store.messages"
        :key="m.id"
        class="flex"
        :class="m.role === 'user' ? 'justify-end' : 'justify-start'"
      >
        <div
          class="max-w-[80%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap"
          :class="m.role === 'user'
            ? 'bg-violet-600 text-white'
            : 'bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-gray-100'"
        >
          <p>{{ m.content }}</p>
          <div v-if="m.citations.length" class="mt-2 flex flex-wrap gap-1.5">
            <RouterLink
              v-for="c in m.citations"
              :key="c.entry_id"
              :to="`/entries/${c.entry_id}`"
              class="text-xs px-2 py-0.5 rounded bg-violet-50 dark:bg-violet-900/30 text-violet-700 dark:text-violet-300"
              data-testid="message-citation"
            >
              {{ c.entry_date }}
            </RouterLink>
          </div>
        </div>
      </div>
      <div v-if="store.sending" class="text-sm text-gray-500" data-testid="thinking">
        Thinking…
      </div>
      <p v-if="store.error" class="text-sm text-red-600 dark:text-red-400" data-testid="conversation-error">
        {{ store.error }}
      </p>
    </div>

    <form class="mt-4 flex gap-2" data-testid="conversation-form" @submit.prevent="submit">
      <input
        v-model="draft"
        type="text"
        placeholder="Ask a follow-up…"
        class="flex-1 rounded border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-sm"
        data-testid="conversation-input"
      />
      <button
        type="submit"
        class="rounded bg-violet-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
        :disabled="store.sending || !draft.trim()"
      >
        Send
      </button>
    </form>
  </div>
</template>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test:unit -- src/views/__tests__/ConversationView.test.ts`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/views/ConversationView.vue src/views/__tests__/ConversationView.test.ts
git commit -m "feat(view): ConversationView chat UI"
```

---

## Task 14: Webapp — `ConversationListView`

**Files:**
- Create: `src/views/ConversationListView.vue`
- Test: `src/views/__tests__/ConversationListView.test.ts`

Rows of title + relative updated time + message count; click opens; a delete control per row (confirm). Inline a `relativeFromNow(iso)` helper (matches the existing inlined pattern in `EntityListView.vue`).

- [ ] **Step 1: Write the failing test**

```typescript
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises, enableAutoUnmount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'
import ConversationListView from '../ConversationListView.vue'

vi.mock('@/api/conversations', () => ({
  listConversations: vi.fn(),
  deleteConversation: vi.fn(),
  getConversation: vi.fn(),
  sendMessage: vi.fn(),
  createConversation: vi.fn(),
}))

import { listConversations, deleteConversation } from '@/api/conversations'
const mList = vi.mocked(listConversations)
const mDelete = vi.mocked(deleteConversation)

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/conversations', name: 'conversations', component: ConversationListView },
    { path: '/conversations/:id', name: 'conversation', component: { template: '<div/>' } },
  ],
})

async function mountView() {
  router.push('/conversations')
  await router.isReady()
  return mount(ConversationListView, { global: { plugins: [createPinia(), router] } })
}

describe('ConversationListView', () => {
  enableAutoUnmount(beforeEach)
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders rows', async () => {
    mList.mockResolvedValue({
      conversations: [
        { id: 1, title: 'first thread', updated_at: '2026-06-17T00:00:00Z', message_count: 4 },
      ],
    })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('first thread')
    expect(wrapper.find('[data-testid="conversation-row"]').exists()).toBe(true)
  })

  it('delete removes the row after confirm', async () => {
    mList.mockResolvedValue({
      conversations: [
        { id: 1, title: 'x', updated_at: '2026-06-17T00:00:00Z', message_count: 2 },
      ],
    })
    mDelete.mockResolvedValue()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const wrapper = await mountView()
    await flushPromises()
    await wrapper.find('[data-testid="delete-conversation"]').trigger('click')
    await flushPromises()
    expect(mDelete).toHaveBeenCalledWith(1)
    expect(wrapper.find('[data-testid="conversation-row"]').exists()).toBe(false)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test:unit -- src/views/__tests__/ConversationListView.test.ts`
Expected: FAIL — view not found.

- [ ] **Step 3: Write the view**

```vue
<script setup lang="ts">
import { onMounted } from 'vue'
import { RouterLink } from 'vue-router'
import { useConversationsStore } from '@/stores/conversations'

const store = useConversationsStore()

onMounted(() => store.loadList())

function relativeFromNow(iso: string): string {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  const diffSec = Math.round((Date.now() - t) / 1000)
  if (diffSec < 60) return 'just now'
  const diffMin = Math.round(diffSec / 60)
  if (diffMin < 60) return `${diffMin} minute${diffMin === 1 ? '' : 's'} ago`
  const diffHr = Math.round(diffMin / 60)
  if (diffHr < 24) return `${diffHr} hour${diffHr === 1 ? '' : 's'} ago`
  const diffDay = Math.round(diffHr / 24)
  if (diffDay < 30) return `${diffDay} day${diffDay === 1 ? '' : 's'} ago`
  const diffMo = Math.round(diffDay / 30)
  if (diffMo < 12) return `${diffMo} month${diffMo === 1 ? '' : 's'} ago`
  return `${Math.round(diffMo / 12)} year${Math.round(diffMo / 12) === 1 ? '' : 's'} ago`
}

async function onDelete(id: number) {
  if (!window.confirm('Delete this conversation?')) return
  await store.remove(id)
}
</script>

<template>
  <div class="max-w-3xl mx-auto px-4 py-6" data-testid="conversation-list-view">
    <h1 class="text-lg font-semibold text-gray-900 dark:text-white mb-4">Conversations</h1>

    <p v-if="!store.listLoading && store.summaries.length === 0" class="text-sm text-gray-500">
      No conversations yet. Start one from a Search answer.
    </p>

    <ul class="divide-y divide-gray-200 dark:divide-gray-800">
      <li
        v-for="c in store.summaries"
        :key="c.id"
        class="flex items-center justify-between py-3"
        data-testid="conversation-row"
      >
        <RouterLink :to="`/conversations/${c.id}`" class="min-w-0 flex-1">
          <span class="block truncate text-sm font-medium text-gray-900 dark:text-white">
            {{ c.title }}
          </span>
          <span class="text-xs text-gray-500">
            {{ relativeFromNow(c.updated_at) }} · {{ c.message_count }} messages
          </span>
        </RouterLink>
        <button
          class="ml-3 text-xs text-red-600 dark:text-red-400"
          data-testid="delete-conversation"
          @click="onDelete(c.id)"
        >
          Delete
        </button>
      </li>
    </ul>
  </div>
</template>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test:unit -- src/views/__tests__/ConversationListView.test.ts`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/views/ConversationListView.vue src/views/__tests__/ConversationListView.test.ts
git commit -m "feat(view): ConversationListView history"
```

---

## Task 15: Webapp — Search integration + star removal

**Files:**
- Modify: `src/views/SearchView.vue`
- Test: `src/views/__tests__/SearchView.test.ts` (add 2 cases)

Add "Continue this conversation →" to the answer tile — calls `store.start({ question: searchStore.lastRunQuery, answer: searchStore.answer, citations: searchStore.answerCitations })` then routes to `/conversations/:id`. Shown only when an answer is present (`store.answered` / non-empty `store.answer`). Remove the `<span aria-hidden="true">✨</span>` from the answer tile header (keep the "Answer" label).

- [ ] **Step 1: Write the failing tests (append to `SearchView.test.ts`)**

```typescript
import { useConversationsStore } from '@/stores/conversations'

// (ensure the conversations API is mocked at the top of the file alongside
//  the search API mock:)
// vi.mock('@/api/conversations', () => ({
//   createConversation: vi.fn(), listConversations: vi.fn(),
//   getConversation: vi.fn(), sendMessage: vi.fn(), deleteConversation: vi.fn(),
// }))

it('answer tile no longer shows the ✨ star', async () => {
  mockSearch.mockResolvedValue({
    query: 'when', reranker: 'r', limit: 20, offset: 0, sort: 'relevance', items: [],
  })
  mockAnswer.mockResolvedValue({
    question: 'when?', answer: 'On 2026-02-14.', answered: true,
    is_question: true,
    citations: [{ entry_id: 42, entry_date: '2026-02-14', snippet: 'back' }],
    model: 'm',
  })
  const wrapper = mountView()
  await wrapper.find('[data-testid="search-query-input"]').setValue('when did my back hurt?')
  await wrapper.find('[data-testid="search-form"]').trigger('submit')
  await flushPromises()
  const panel = wrapper.find('[data-testid="answer-panel"]')
  expect(panel.exists()).toBe(true)
  expect(panel.text()).not.toContain('✨')
  expect(panel.text()).toContain('Answer')
})

it('"Continue this conversation" starts a conversation and navigates', async () => {
  mockSearch.mockResolvedValue({
    query: 'when', reranker: 'r', limit: 20, offset: 0, sort: 'relevance', items: [],
  })
  mockAnswer.mockResolvedValue({
    question: 'when?', answer: 'On 2026-02-14.', answered: true,
    is_question: true,
    citations: [{ entry_id: 42, entry_date: '2026-02-14', snippet: 'back' }],
    model: 'm',
  })
  const wrapper = mountView()
  await wrapper.find('[data-testid="search-query-input"]').setValue('when did my back hurt?')
  await wrapper.find('[data-testid="search-form"]').trigger('submit')
  await flushPromises()

  const convStore = useConversationsStore()
  const startSpy = vi.spyOn(convStore, 'start').mockResolvedValue(7)
  const pushSpy = vi.spyOn(router, 'push')

  await wrapper.find('[data-testid="continue-conversation"]').trigger('click')
  await flushPromises()

  expect(startSpy).toHaveBeenCalledWith({
    question: 'when did my back hurt?',
    answer: 'On 2026-02-14.',
    citations: [{ entry_id: 42, entry_date: '2026-02-14', snippet: 'back' }],
  })
  expect(pushSpy).toHaveBeenCalledWith('/conversations/7')
})
```

> The existing `SearchView.test.ts` already builds `router` + mounts via `mountView()`. Reuse those. If `lastRunQuery` differs from the typed query (recon shows `store.lastRunQuery` is the submitted query), the `start` arg uses `lastRunQuery`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `npm run test:unit -- src/views/__tests__/SearchView.test.ts`
Expected: FAIL — `continue-conversation` element not found / star still present.

- [ ] **Step 3: Edit `SearchView.vue`**

(a) Remove the star line from the answer tile header — delete exactly:

```vue
    <span aria-hidden="true">✨</span>
```

(leaving the surrounding `<div ...>Answer</div>` header intact).

(b) Add the continue control + handler. In the answer-tile `<template v-else>` block, after the citations `<div>`, add:

```vue
    <div v-if="searchStore.answer" class="mt-3">
      <button
        type="button"
        class="text-sm font-medium text-violet-700 dark:text-violet-300 hover:underline"
        data-testid="continue-conversation"
        @click="continueConversation"
      >
        Continue this conversation →
      </button>
    </div>
```

In `<script setup>`, add the imports + handler (using the existing search store reference — recon shows it is `store`; if so, use `store` instead of `searchStore` consistently):

```typescript
import { useRouter } from 'vue-router'
import { useConversationsStore } from '@/stores/conversations'

const router = useRouter()
const conversationsStore = useConversationsStore()

async function continueConversation() {
  const id = await conversationsStore.start({
    question: store.lastRunQuery,
    answer: store.answer,
    citations: store.answerCitations,
  })
  router.push(`/conversations/${id}`)
}
```

> Use whatever local name the file already binds the search store to (recon shows `const store = useSearchStore()`). Keep the template references consistent with that name.

- [ ] **Step 4: Run tests to verify they pass**

Run: `npm run test:unit -- src/views/__tests__/SearchView.test.ts`
Expected: PASS — existing cases plus the 2 new ones.

- [ ] **Step 5: Commit**

```bash
git add src/views/SearchView.vue src/views/__tests__/SearchView.test.ts
git commit -m "feat(search): continue-conversation CTA; remove answer-tile star"
```

---

## Task 16: Webapp — docs + journal + full gate

**Files:**
- Modify: a search/dev doc under `webapp/docs/` (add a conversations note)
- Create: `journal/260617-conversations.md`

- [ ] **Step 1: Add a docs note**

In the webapp search/dev doc that documents the answer tile, add a short "Conversations" note: the two routes, that a conversation is seeded from the current answer via `POST /api/conversations`, replies re-ground server-side, and the history lives at `/conversations`. Keep it concise; link to the server `docs/conversations.md` if cross-repo links are used elsewhere.

- [ ] **Step 2: Write the journal entry**

`journal/260617-conversations.md` — decisions (dedicated full-page chat, seed from existing answer, optimistic user turn, re-ground per reply server-side), new files, and the star removal.

- [ ] **Step 3: Run the full webapp gate**

Run: `npm run format:check && npm run lint && npm run test:coverage && npm run build`
Expected: all pass; coverage ≥85% on statements/branches/functions/lines. If coverage dips, add store/view cases until green (do not lower thresholds).

- [ ] **Step 4: Commit**

```bash
git add docs/ journal/260617-conversations.md
git commit -m "docs(conversations): webapp note + journal"
```

---

## Final verification before shipping

- [ ] **Server:** `cd server && uv run pytest -m "not integration" -q && uv run ruff check src/ tests/` — all green, ruff clean.
- [ ] **Webapp:** `cd webapp && npm run format:check && npm run lint && npm run test:coverage && npm run build` — all green, coverage ≥85%.
- [ ] **Push + PRs:** push each branch; open a PR per repo cross-referencing the other; `gh run watch` each to green.
- [ ] **Merge + deploy:** merge both PRs to `main`; confirm main CI builds + publishes the ghcr images; then `ssh media && cd /srv/media && docker compose pull journal-server journal-webapp && docker compose up -d journal-server journal-webapp`; confirm a clean boot in `docker logs journal-server`.

---

## Self-review notes (spec coverage)

- Migration 0030, both tables, CHECK, cascade → Task 1. ✅
- Repository (create/list/get/add/delete + user-scoping + JSON citations) → Task 3. ✅
- Answerer `continue_conversation` (history→messages, passages on final user turn, citation filtering, raise on malformed/API error, Noop) → Task 4. ✅
- `ConversationService` (start seeds; reply re-retrieves with combined query, resolves citations, persists both turns only on success, propagates `AnswerUnavailable`) → Task 5. ✅
- Routes (create/list/get/reply/delete; 400/404/502/503) → Task 6. ✅
- Wiring + config reuse (no new env vars) → Task 7. ✅
- Webapp types/client/store/routes/nav/views + Search CTA + star removal → Tasks 9–15. ✅
- Docs + journals both repos → Tasks 8, 16. ✅
- Out-of-scope (streaming, LLM titles, branching, per-turn rewrite) — intentionally omitted. ✅
```
