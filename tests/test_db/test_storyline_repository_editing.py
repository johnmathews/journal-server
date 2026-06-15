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


# ── split_chapter ────────────────────────────────────────────────


def test_split_closed_chapter_yields_two_contiguous_closed(
    repo: SQLiteStorylineRepository, storyline: Storyline
) -> None:
    ch = repo.create_chapter(storyline.id, seq=1, title="All",
                             start_date="2026-01-01", end_date="2026-06-30",
                             state="closed")
    left, right = repo.split_chapter(ch.id, "2026-04-01")
    assert (left.start_date, left.end_date) == ("2026-01-01", "2026-03-31")
    assert (right.start_date, right.end_date) == ("2026-04-01", "2026-06-30")
    assert left.seq == 1 and right.seq == 2
    assert left.state == "closed" and right.state == "closed"


def test_split_open_chapter_keeps_right_half_open(
    repo: SQLiteStorylineRepository, storyline: Storyline
) -> None:
    ch = repo.create_chapter(storyline.id, seq=1, title="Live",
                             start_date="2026-01-01", end_date=None, state="open")
    left, right = repo.split_chapter(ch.id, "2026-04-01")
    assert left.state == "closed" and left.end_date == "2026-03-31"
    assert right.state == "open" and right.end_date is None
    opens = [c for c in repo.list_chapters(storyline.id) if c.state == "open"]
    assert len(opens) == 1 and opens[0].id == right.id


def test_split_shifts_later_chapters_up(
    repo: SQLiteStorylineRepository, storyline: Storyline
) -> None:
    a = repo.create_chapter(storyline.id, seq=1, title="A", start_date="2026-01-01",
                            end_date="2026-06-30", state="closed")
    repo.create_chapter(storyline.id, seq=2, title="B", start_date="2026-07-01",
                        end_date=None, state="open")
    repo.split_chapter(a.id, "2026-04-01")
    seqs = {c.title: c.seq for c in repo.list_chapters(storyline.id)}
    assert seqs["A"] == 1
    assert seqs["B"] == 3


def test_split_rejects_date_outside_window(
    repo: SQLiteStorylineRepository, storyline: Storyline
) -> None:
    ch = repo.create_chapter(storyline.id, seq=1, title="X", start_date="2026-01-01",
                             end_date="2026-06-30", state="closed")
    with pytest.raises(ValueError):
        repo.split_chapter(ch.id, "2026-01-01")
    with pytest.raises(ValueError):
        repo.split_chapter(ch.id, "2026-07-01")


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


# ── merge_chapters ────────────────────────────────────────────────


def test_merge_adjacent_unions_window(
    repo: SQLiteStorylineRepository, storyline: Storyline
) -> None:
    a = repo.create_chapter(storyline.id, seq=1, title="A", start_date="2026-01-01",
                            end_date="2026-03-31", state="closed")
    b = repo.create_chapter(storyline.id, seq=2, title="B", start_date="2026-04-01",
                            end_date="2026-06-30", state="closed")
    merged = repo.merge_chapters([a.id, b.id])
    assert merged.id == a.id
    assert (merged.start_date, merged.end_date) == ("2026-01-01", "2026-06-30")
    assert merged.state == "closed"
    assert len(repo.list_chapters(storyline.id)) == 1


def test_merge_with_open_stays_open(repo: SQLiteStorylineRepository, storyline: Storyline) -> None:
    a = repo.create_chapter(storyline.id, seq=1, title="A", start_date="2026-01-01",
                            end_date="2026-03-31", state="closed")
    b = repo.create_chapter(storyline.id, seq=2, title="B", start_date="2026-04-01",
                            end_date=None, state="open")
    merged = repo.merge_chapters([a.id, b.id])
    assert merged.state == "open" and merged.end_date is None


def test_merge_shifts_tail_down(repo: SQLiteStorylineRepository, storyline: Storyline) -> None:
    a = repo.create_chapter(storyline.id, seq=1, title="A", start_date="2026-01-01",
                            end_date="2026-03-31", state="closed")
    b = repo.create_chapter(storyline.id, seq=2, title="B", start_date="2026-04-01",
                            end_date="2026-06-30", state="closed")
    repo.create_chapter(storyline.id, seq=3, title="C", start_date="2026-07-01",
                        end_date=None, state="open")
    repo.merge_chapters([a.id, b.id])
    seqs = {ch.title: ch.seq for ch in repo.list_chapters(storyline.id)}
    assert seqs["A"] == 1 and seqs["C"] == 2


def test_merge_rejects_non_adjacent(repo: SQLiteStorylineRepository, storyline: Storyline) -> None:
    a = repo.create_chapter(storyline.id, seq=1, title="A", start_date="2026-01-01",
                            end_date="2026-03-31", state="closed")
    repo.create_chapter(storyline.id, seq=2, title="B", start_date="2026-04-01",
                        end_date="2026-06-30", state="closed")
    c = repo.create_chapter(storyline.id, seq=3, title="C", start_date="2026-07-01",
                            end_date=None, state="open")
    with pytest.raises(ValueError):
        repo.merge_chapters([a.id, c.id])
    with pytest.raises(ValueError):
        repo.merge_chapters([a.id])


def _make_storyline(
    factory: ConnectionFactory,
    repo: SQLiteStorylineRepository,
    email: str,
    entity_name: str = "Cycling",
) -> Storyline:
    """Helper: insert a user + entity and return a fresh storyline anchored on it."""
    conn = factory.get()
    cursor = conn.execute(
        "INSERT INTO users (email, password_hash, display_name) VALUES (?, ?, ?)",
        (email, "x", email),
    )
    conn.commit()
    user_id = cursor.lastrowid
    assert user_id is not None

    store = SQLiteEntityStore(factory)
    entity = store.create_entity(
        entity_type="activity",
        canonical_name=entity_name,
        description=f"The activity of {entity_name.lower()}",
        first_seen="2026-01-01",
        user_id=user_id,
    )
    return repo.create_storyline(
        user_id=user_id,
        entity_ids=[entity.id],
        name=f"{entity_name} Storyline",
    )


def test_merge_rejects_different_storylines(
    factory: ConnectionFactory,
    repo: SQLiteStorylineRepository,
    storyline: Storyline,
) -> None:
    """Chapters from two different storylines must not be merged."""
    other = _make_storyline(factory, repo, "other_merge@example.com", "Swimming")

    ch_s1 = repo.create_chapter(storyline.id, seq=1, title="S1-A",
                                 start_date="2026-01-01", end_date="2026-03-31",
                                 state="closed")
    ch_s2 = repo.create_chapter(other.id, seq=1, title="S2-A",
                                 start_date="2026-01-01", end_date="2026-03-31",
                                 state="closed")

    with pytest.raises(ValueError):
        repo.merge_chapters([ch_s1.id, ch_s2.id])


def test_merge_rejects_missing_chapter(
    repo: SQLiteStorylineRepository,
    storyline: Storyline,
) -> None:
    """Passing a chapter id that doesn't exist must raise ValueError."""
    a = repo.create_chapter(storyline.id, seq=1, title="A",
                            start_date="2026-01-01", end_date=None, state="open")
    with pytest.raises(ValueError):
        repo.merge_chapters([a.id, 999999])


# ── add_chapter ───────────────────────────────────────────────────


def test_add_new_latest_closes_open_and_opens_fresh(
    repo: SQLiteStorylineRepository, storyline: Storyline
) -> None:
    old = repo.create_chapter(storyline.id, seq=1, title="Old",
                              start_date="2026-01-01", end_date=None, state="open")
    new = repo.add_chapter(storyline.id, start_date="2026-04-01")
    closed = repo.get_chapter(old.id)
    assert closed is not None
    assert closed.state == "closed" and closed.end_date == "2026-03-31"
    assert new.state == "open" and new.start_date == "2026-04-01"
    assert new.end_date is None and new.seq == 2
    opens = [c for c in repo.list_chapters(storyline.id) if c.state == "open"]
    assert len(opens) == 1 and opens[0].id == new.id


def test_add_ranged_into_gap_is_closed_and_ordered(
    repo: SQLiteStorylineRepository, storyline: Storyline
) -> None:
    repo.create_chapter(storyline.id, seq=1, title="A", start_date="2026-01-01",
                        end_date="2026-03-31", state="closed")
    repo.create_chapter(storyline.id, seq=2, title="B", start_date="2026-07-01",
                        end_date=None, state="open")
    added = repo.add_chapter(storyline.id, start_date="2026-04-01", end_date="2026-05-31")
    assert added.state == "closed"
    assert added.seq == 2
    titles = [c.title for c in repo.list_chapters(storyline.id)]
    assert titles == ["A", "", "B"]


def test_add_ranged_rejects_overlap(
    repo: SQLiteStorylineRepository, storyline: Storyline
) -> None:
    repo.create_chapter(storyline.id, seq=1, title="A", start_date="2026-01-01",
                        end_date="2026-06-30", state="closed")
    with pytest.raises(ValueError):
        repo.add_chapter(storyline.id, start_date="2026-03-01", end_date="2026-09-30")


def test_add_ranged_rejects_end_before_start(
    repo: SQLiteStorylineRepository, storyline: Storyline
) -> None:
    with pytest.raises(ValueError):
        repo.add_chapter(storyline.id, start_date="2026-05-01", end_date="2026-04-01")


def test_add_new_latest_rejects_start_not_after_open(
    repo: SQLiteStorylineRepository, storyline: Storyline
) -> None:
    repo.create_chapter(storyline.id, seq=1, title="Open", state="open",
                        start_date="2026-04-01", end_date=None)
    with pytest.raises(ValueError):
        repo.add_chapter(storyline.id, start_date="2026-04-01")  # equal == rejected


def test_add_ranged_rejects_touching_boundary(
    repo: SQLiteStorylineRepository, storyline: Storyline
) -> None:
    repo.create_chapter(storyline.id, seq=1, title="A", state="closed",
                        start_date="2026-01-01", end_date="2026-03-31")
    repo.create_chapter(storyline.id, seq=2, title="B", state="open",
                        start_date="2026-07-01", end_date=None)
    with pytest.raises(ValueError):
        # touches A's end exactly -> overlap
        repo.add_chapter(storyline.id, start_date="2026-03-31", end_date="2026-05-31")


def test_add_ranged_at_tail_when_no_later_chapters(
    repo: SQLiteStorylineRepository, storyline: Storyline
) -> None:
    # Only a closed chapter; add a ranged closed chapter after it.
    # No open chapter means no +∞ end, so there's nothing "later" than the new range.
    repo.create_chapter(storyline.id, seq=1, title="A", state="closed",
                        start_date="2026-01-01", end_date="2026-03-31")
    added = repo.add_chapter(storyline.id, start_date="2026-04-01", end_date="2026-05-31")
    assert added.seq == 2 and added.state == "closed"
