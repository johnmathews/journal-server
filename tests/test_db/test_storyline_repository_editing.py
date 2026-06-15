"""Tests for chapter-editing helpers in storyline_repository.

Phase A, Task 1: date helper functions and the _shift_seqs resequencer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from journal.db.storyline_repository import (
    SQLiteStorylineRepository,
    _day_after,
    _day_before,
)
from journal.entitystore.store import SQLiteEntityStore

if TYPE_CHECKING:
    from collections.abc import Generator

    from journal.db.factory import ConnectionFactory
    from journal.models import Storyline


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def repo(factory: ConnectionFactory) -> Generator[SQLiteStorylineRepository]:
    yield SQLiteStorylineRepository(factory)


@pytest.fixture
def storyline(factory: ConnectionFactory, repo: SQLiteStorylineRepository) -> Storyline:
    """Create a minimal user + entity, then return a storyline anchored on that entity."""
    conn = factory.get()
    cursor = conn.execute(
        "INSERT INTO users (email, password_hash, display_name) VALUES (?, ?, ?)",
        ("edit_test@example.com", "x", "Edit Test User"),
    )
    conn.commit()
    user_id = cursor.lastrowid
    assert user_id is not None

    store = SQLiteEntityStore(factory)
    entity = store.create_entity(
        entity_type="activity",
        canonical_name="Running",
        description="The activity of running",
        first_seen="2026-01-01",
        user_id=user_id,
    )

    return repo.create_storyline(
        user_id=user_id,
        entity_ids=[entity.id],
        name="Test Storyline",
    )


# ── Date helpers ──────────────────────────────────────────────────


def test_day_before_and_after() -> None:
    assert _day_before("2026-03-01") == "2026-02-28"
    assert _day_after("2026-02-28") == "2026-03-01"
    # leap year
    assert _day_after("2024-02-28") == "2024-02-29"
    # year rollover
    assert _day_before("2026-01-01") == "2025-12-31"


# ── _shift_seqs ───────────────────────────────────────────────────


def test_shift_seqs_positive_inserts_gap(
    repo: SQLiteStorylineRepository, storyline: Storyline
) -> None:
    repo.create_chapter(
        storyline.id, seq=1, title="A", state="closed",
        start_date="2026-01-01", end_date="2026-02-28",
    )
    repo.create_chapter(
        storyline.id, seq=2, title="B", state="closed",
        start_date="2026-03-01", end_date="2026-04-30",
    )
    repo.create_chapter(
        storyline.id, seq=3, title="C", state="open",
        start_date="2026-05-01", end_date=None,
    )
    conn = repo._conn()
    repo._shift_seqs(conn, storyline.id, from_seq=2, delta=1)
    conn.commit()
    seqs = {c.title: c.seq for c in repo.list_chapters(storyline.id)}
    assert seqs == {"A": 1, "B": 3, "C": 4}


def test_shift_seqs_negative_closes_gap(
    repo: SQLiteStorylineRepository, storyline: Storyline
) -> None:
    # Simulate a delete: row at seq 2 already removed, tail at 3 shifts to 2.
    repo.create_chapter(
        storyline.id, seq=1, title="A", state="closed",
        start_date="2026-01-01", end_date="2026-02-28",
    )
    repo.create_chapter(
        storyline.id, seq=3, title="C", state="open",
        start_date="2026-05-01", end_date=None,
    )
    conn = repo._conn()
    repo._shift_seqs(conn, storyline.id, from_seq=3, delta=-1)
    conn.commit()
    seqs = {c.title: c.seq for c in repo.list_chapters(storyline.id)}
    assert seqs == {"A": 1, "C": 2}


def test_shift_seqs_zero_delta_raises(
    repo: SQLiteStorylineRepository, storyline: Storyline
) -> None:
    repo.create_chapter(
        storyline.id, seq=1, title="A", state="open",
        start_date="2026-01-01", end_date=None,
    )
    conn = repo._conn()
    with pytest.raises(AssertionError):
        repo._shift_seqs(conn, storyline.id, from_seq=1, delta=0)


def test_shift_seqs_only_affects_target_storyline(
    factory: ConnectionFactory,
    repo: SQLiteStorylineRepository,
    storyline: Storyline,
) -> None:
    """Chapters belonging to another storyline must not be touched."""
    conn = factory.get()
    cursor = conn.execute(
        "INSERT INTO users (email, password_hash, display_name) VALUES (?, ?, ?)",
        ("other@example.com", "x", "Other User"),
    )
    conn.commit()
    other_user_id = cursor.lastrowid
    assert other_user_id is not None

    store = SQLiteEntityStore(factory)
    other_entity = store.create_entity(
        entity_type="activity",
        canonical_name="Cycling",
        description="The activity of cycling",
        first_seen="2026-01-01",
        user_id=other_user_id,
    )
    other_storyline = repo.create_storyline(
        user_id=other_user_id,
        entity_ids=[other_entity.id],
        name="Other Storyline",
    )

    repo.create_chapter(
        storyline.id, seq=1, title="A", state="closed",
        start_date="2026-01-01", end_date="2026-02-28",
    )
    repo.create_chapter(
        storyline.id, seq=2, title="B", state="open",
        start_date="2026-03-01", end_date=None,
    )
    repo.create_chapter(
        other_storyline.id, seq=1, title="X", state="open",
        start_date="2026-01-01", end_date=None,
    )

    conn2 = repo._conn()
    repo._shift_seqs(conn2, storyline.id, from_seq=1, delta=1)
    conn2.commit()

    # Target storyline: both chapters shifted by 1
    seqs = {c.title: c.seq for c in repo.list_chapters(storyline.id)}
    assert seqs == {"A": 2, "B": 3}

    # Other storyline: untouched
    other_seqs = {c.title: c.seq for c in repo.list_chapters(other_storyline.id)}
    assert other_seqs == {"X": 1}
