"""Migration 0037: entries.date_confirmed quarantine flag."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from journal.db.migrations import (
    _executescript_idempotent,
    get_current_version,
    get_migration_files,
)

if TYPE_CHECKING:
    from pathlib import Path


def _migrate_to(conn: sqlite3.Connection, version: int) -> None:
    """Apply every migration up to and including ``version`` (same
    pattern as test_migration_0036.py)."""
    current = get_current_version(conn)
    for migration_file in get_migration_files():
        file_version = int(migration_file.stem.split("_")[0])
        if file_version <= current:
            continue
        if file_version > version:
            break
        _executescript_idempotent(conn, migration_file.read_text(), migration_file.name)
        conn.execute(f"PRAGMA user_version = {file_version}")
        current = file_version


@pytest.fixture
def pre_0037(tmp_path: Path) -> sqlite3.Connection:
    """A DB at version 0036 with a prod-shaped entry row."""
    conn = sqlite3.connect(tmp_path / "m.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate_to(conn, 36)
    conn.execute(
        "INSERT INTO users (email, password_hash, display_name)"
        " VALUES ('u@x.dev', 'h', 'U')"
    )
    conn.execute(
        "INSERT INTO entries (entry_date, source_type, raw_text, final_text,"
        " word_count, user_id) VALUES ('2026-07-01', 'photo', 'body', 'body', 1, 1)"
    )
    conn.commit()
    return conn


def test_0037_adds_date_confirmed_default_confirmed(
    pre_0037: sqlite3.Connection,
) -> None:
    conn = pre_0037
    _migrate_to(conn, 37)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(entries)")}
    assert "date_confirmed" in cols

    row = conn.execute("SELECT date_confirmed FROM entries").fetchone()
    assert row[0] == 1  # pre-existing rows are confirmed

    # New rows default to confirmed unless explicitly quarantined.
    conn.execute(
        "INSERT INTO entries (entry_date, source_type, raw_text, final_text,"
        " word_count, user_id) VALUES ('2026-07-02', 'photo', 'x', 'x', 1, 1)"
    )
    row = conn.execute(
        "SELECT date_confirmed FROM entries WHERE entry_date = '2026-07-02'"
    ).fetchone()
    assert row[0] == 1
