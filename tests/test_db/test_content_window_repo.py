"""Tests for content window persistence (content_start_char / content_end_char)."""

import pytest

from journal.db.repository import SQLiteEntryRepository


@pytest.fixture
def repo(factory):
    return SQLiteEntryRepository(factory)


def test_create_entry_persists_window(repo):
    e = repo.create_entry(
        "2026-01-01", "photo", "tail\nbody\nnext", 3,
        final_text="body", content_start_char=5, content_end_char=9,
    )
    got = repo.get_entry(e.id)
    assert got.content_start_char == 5
    assert got.content_end_char == 9


def test_create_entry_window_defaults_null(repo):
    e = repo.create_entry("2026-01-01", "photo", "body", 1)
    got = repo.get_entry(e.id)
    assert got.content_start_char is None
    assert got.content_end_char is None


def test_set_content_window_updates_and_clears(repo):
    e = repo.create_entry("2026-01-01", "photo", "tail body next", 3)
    repo.set_content_window(e.id, 5, 9)
    entry = repo.get_entry(e.id)
    assert entry.content_start_char == 5
    assert entry.content_end_char == 9
    repo.set_content_window(e.id, None, None)
    assert repo.get_entry(e.id).content_start_char is None
    assert repo.get_entry(e.id).content_end_char is None


def test_set_content_window_respects_user_id_scoping(repo):
    """Verify set_content_window respects user_id scoping and does not update rows
    if user_id does not match."""
    e = repo.create_entry("2026-01-01", "photo", "tail body next", 3, user_id=1)
    # Try to set window with a different user_id (999) — should not update
    result = repo.set_content_window(e.id, 5, 9, user_id=999)
    assert result is None
    # Verify the window was NOT updated
    entry = repo.get_entry(e.id)
    assert entry.content_start_char is None
    assert entry.content_end_char is None
