"""Migration 0032 creates the conversations tables."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from journal.db.factory import ConnectionFactory
from journal.db.migrations import run_migrations

if TYPE_CHECKING:
    from pathlib import Path


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
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO conversation_messages "
            "(conversation_id, role, content, created_at) "
            "VALUES (?, 'system', 'x', '2026-06-17T00:00:00Z')",
            (cid,),
        )


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
