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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from journal.models import Conversation, ConversationMessage

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

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
    ) -> list[int]:
        """Insert messages; return the new row ids in insertion order."""
        inserted_ids: list[int] = []
        for m in messages:
            citations = m.get("citations") or []
            cur = conn.execute(
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
            inserted_ids.append(int(cur.lastrowid or 0))
        return inserted_ids

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
        now = _now_iso()
        # Capture the exact ids we insert so the return set can never pick
        # up a row written by a concurrent caller (rather than re-querying
        # by a captured MAX(id), which isn't race-safe).
        new_ids = self._insert_messages(conn, conversation_id, messages, now)
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )
        conn.commit()
        if not new_ids:
            return []
        placeholders = ",".join("?" for _ in new_ids)
        rows = conn.execute(
            "SELECT id, role, content, citations, created_at "
            "FROM conversation_messages "
            f"WHERE id IN ({placeholders}) ORDER BY id",
            new_ids,
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
