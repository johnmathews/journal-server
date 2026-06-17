"""SQLiteConversationRepository — create/list/get/add/delete + user scoping."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from journal.db.conversation_repository import (
    ConversationNotFound,
    SQLiteConversationRepository,
)
from journal.db.factory import ConnectionFactory
from journal.db.migrations import run_migrations

# USER_A is the admin user that migration 0011 seeds at id=1 (the same
# convention the storylines API tests rely on). USER_B is seeded below.
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
