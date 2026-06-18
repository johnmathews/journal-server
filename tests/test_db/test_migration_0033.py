"""Migration 0033 adds content_start_char and content_end_char columns
to the entries table (half-open [start, end) window into raw_text;
NULL = whole text, same convention as entry_uncertain_spans in 0005).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from journal.db.factory import ConnectionFactory
from journal.db.migrations import run_migrations

if TYPE_CHECKING:
    from pathlib import Path


def _migrated(tmp_path: Path) -> ConnectionFactory:
    factory = ConnectionFactory(tmp_path / "m.db")
    run_migrations(factory.get())
    return factory


def test_entries_has_content_window_columns(tmp_path: Path) -> None:
    conn = _migrated(tmp_path).get()
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(entries)")}
    assert "content_start_char" in cols
    assert "content_end_char" in cols


def test_content_window_columns_default_null(tmp_path: Path) -> None:
    conn = _migrated(tmp_path).get()
    # Insert a minimal entry; omit the new columns so they default to NULL.
    conn.execute(
        "INSERT INTO entries (user_id, entry_date, source_type, raw_text, "
        "final_text, word_count) VALUES (1, '2026-01-01', 'photo', 'hi', 'hi', 1)"
    )
    row = conn.execute(
        "SELECT content_start_char, content_end_char FROM entries"
    ).fetchone()
    assert row["content_start_char"] is None
    assert row["content_end_char"] is None


def test_content_window_columns_accept_integer_values(tmp_path: Path) -> None:
    conn = _migrated(tmp_path).get()
    conn.execute(
        "INSERT INTO entries (user_id, entry_date, source_type, raw_text, "
        "final_text, word_count, content_start_char, content_end_char) "
        "VALUES (1, '2026-01-01', 'photo', 'hello world', 'hello world', 2, 0, 5)"
    )
    row = conn.execute(
        "SELECT content_start_char, content_end_char FROM entries"
    ).fetchone()
    assert row["content_start_char"] == 0
    assert row["content_end_char"] == 5
