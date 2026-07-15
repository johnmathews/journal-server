"""Migration 0038: UNIQUE(entry_id, dimension) on mood_scores.

State-transforming migration, so the tests include a DIRTY fixture: duplicate
(entry_id, dimension) rows seeded BEFORE the unique index exists. The migration
must collapse them to the newest row per pair, add the index, reject further
duplicates, and be a clean no-op on re-run.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from journal.db.factory import ConnectionFactory
from journal.db.migrations import (
    _executescript_idempotent,
    get_current_version,
    get_migration_files,
    run_migrations,
)
from journal.db.repository import SQLiteEntryRepository

if TYPE_CHECKING:
    from pathlib import Path


_MIGRATION_0038 = "0038_mood_scores_unique_dimension.sql"
_INDEX = "idx_mood_entry_dimension"


def _migrate_to(conn: sqlite3.Connection, version: int) -> None:
    """Apply every migration up to and including ``version`` (same forward-only
    pattern as test_migration_0037.py — version-skips already-applied files)."""
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


def _index_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
        (_INDEX,),
    ).fetchone()
    return row is not None


def _seed_entry(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT INTO users (email, password_hash, display_name)"
        " VALUES ('u@x.dev', 'h', 'U')"
    )
    entry_id = conn.execute(
        "INSERT INTO entries (entry_date, source_type, raw_text, final_text,"
        " word_count, user_id) VALUES ('2026-07-01', 'photo', 'body', 'body', 1, 1)"
    ).lastrowid
    conn.commit()
    assert entry_id is not None
    return entry_id


@pytest.fixture
def pre_0038(tmp_path: Path) -> tuple[sqlite3.Connection, int]:
    """A DB at version 0037 with an entry and DUPLICATE mood_scores rows.

    Two rows share (entry_id, 'overall') and two share (entry_id, 'energy') —
    the exact dirty state the unique index would forbid but which nothing
    prevented before 0038. Returns ``(conn, entry_id)``.
    """
    conn = sqlite3.connect(tmp_path / "m.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate_to(conn, 37)
    entry_id = _seed_entry(conn)

    # Seed duplicates. The newest row (largest id) per pair carries the score
    # we expect to survive dedup.
    conn.executemany(
        "INSERT INTO mood_scores (entry_id, dimension, score) VALUES (?, ?, ?)",
        [
            (entry_id, "overall", 0.10),  # stale
            (entry_id, "overall", 0.90),  # newest -> survives
            (entry_id, "energy", -0.50),  # stale
            (entry_id, "energy", 0.25),   # newest -> survives
        ],
    )
    conn.commit()
    # Sanity: the index truly does not exist yet, and dupes are present.
    assert not _index_exists(conn)
    assert conn.execute("SELECT COUNT(*) FROM mood_scores").fetchone()[0] == 4
    return conn, entry_id


def test_0038_file_is_discovered() -> None:
    assert _MIGRATION_0038 in {f.name for f in get_migration_files()}


def test_0038_collapses_dupes_to_newest_and_adds_index(
    pre_0038: tuple[sqlite3.Connection, int],
) -> None:
    conn, entry_id = pre_0038

    _migrate_to(conn, 38)

    # (b) the index now exists
    assert _index_exists(conn)
    assert get_current_version(conn) == 38

    # (a) dupes collapse to the newest row per (entry_id, dimension)
    rows = conn.execute(
        "SELECT dimension, score FROM mood_scores WHERE entry_id = ?"
        " ORDER BY dimension",
        (entry_id,),
    ).fetchall()
    assert [(r["dimension"], r["score"]) for r in rows] == [
        ("energy", 0.25),
        ("overall", 0.90),
    ]


def test_0038_rejects_subsequent_duplicate_insert(
    pre_0038: tuple[sqlite3.Connection, int],
) -> None:
    conn, entry_id = pre_0038
    _migrate_to(conn, 38)

    # (c) a duplicate (entry_id, dimension) insert now raises.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO mood_scores (entry_id, dimension, score)"
            " VALUES (?, 'overall', 0.5)",
            (entry_id,),
        )


def test_0038_is_rerunnable(pre_0038: tuple[sqlite3.Connection, int]) -> None:
    """(d) Running the migration twice is a clean no-op — via both the
    forward-only runner (version-skip) and a raw replay of the script."""
    conn, _entry_id = pre_0038
    _migrate_to(conn, 38)
    baseline = conn.execute("SELECT COUNT(*) FROM mood_scores").fetchone()[0]

    # Forward-only re-run: version-skips 0038, must not raise or change data.
    _migrate_to(conn, 38)
    assert _index_exists(conn)
    assert conn.execute("SELECT COUNT(*) FROM mood_scores").fetchone()[0] == baseline

    # Raw replay of the 0038 script on an already-migrated DB: the dedup DELETE
    # is a no-op and CREATE UNIQUE INDEX IF NOT EXISTS is a no-op.
    files = {f.name: f for f in get_migration_files()}
    _executescript_idempotent(
        conn, files[_MIGRATION_0038].read_text(), _MIGRATION_0038,
    )
    assert _index_exists(conn)
    assert conn.execute("SELECT COUNT(*) FROM mood_scores").fetchone()[0] == baseline


def test_replace_mood_scores_still_works_after_0038(tmp_path: Path) -> None:
    """The repository's delete-then-insert path is unaffected by the new
    constraint — a full migrate (through 0038) then a real repo round-trip."""
    factory = ConnectionFactory(tmp_path / "repo.db")
    try:
        conn = factory.get()
        run_migrations(conn)
        assert _index_exists(conn)
        entry_id = _seed_entry(conn)

        repo = SQLiteEntryRepository(factory)
        repo.replace_mood_scores(
            entry_id,
            [
                ("overall", 0.7, 0.9, "up"),
                ("energy", 0.2, None, None),
            ],
        )
        scored = {s.dimension: s.score for s in repo.get_mood_scores(entry_id)}
        assert scored == {"overall": 0.7, "energy": 0.2}

        # Re-writing the same dimensions (delete-then-insert) must not trip the
        # unique index — one row per dimension survives.
        repo.replace_mood_scores(entry_id, [("overall", -0.3, None, None)])
        scored = {s.dimension: s.score for s in repo.get_mood_scores(entry_id)}
        assert scored == {"overall": -0.3, "energy": 0.2}
    finally:
        factory.close_current()
