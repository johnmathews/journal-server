"""Migration 0031: chapter sectioning columns (locks + word count)."""

from __future__ import annotations

# Runtime import (not type-checking only): the helpers below take a live
# `sqlite3.Connection` and call its methods, so the module is used at runtime.
import sqlite3  # noqa: TC003
from typing import TYPE_CHECKING

from journal.db.connection import get_connection
from journal.db.migrations import (
    _executescript_idempotent,
    get_migration_files,
    run_migrations,
)

if TYPE_CHECKING:
    from pathlib import Path

    from journal.db.factory import ConnectionFactory


_MIGRATION_0031 = "0031_storyline_chapter_sectioning.sql"
_NEW_COLUMNS = {"title_locked", "boundary_locked", "narrative_word_count"}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _run_migrations_up_to(conn: sqlite3.Connection, target_version: int) -> None:
    """Apply every migration up to and including ``target_version``."""
    for migration_file in get_migration_files():
        version = int(migration_file.stem.split("_")[0])
        if version > target_version:
            break
        _executescript_idempotent(
            conn, migration_file.read_text(), migration_file.name,
        )
        conn.execute(f"PRAGMA user_version = {version}")


def test_migration_0031_file_exists() -> None:
    files = {f.name for f in get_migration_files()}
    assert _MIGRATION_0031 in files


def test_new_columns_exist_with_default_zero(factory: ConnectionFactory) -> None:
    """On a fully-migrated DB the three columns exist and default to 0."""
    conn = factory.get()
    cols = _columns(conn, "storyline_chapters")
    assert cols >= _NEW_COLUMNS

    # Seed a user + storyline + chapter the plain SQL way and confirm the
    # three columns default to 0 when not supplied on insert.
    user_id = conn.execute(
        "INSERT INTO users (email, password_hash, display_name)"
        " VALUES ('m@n.o', 'x', 'M')",
    ).lastrowid
    storyline_id = conn.execute(
        "INSERT INTO storylines (user_id, name) VALUES (?, 'S')",
        (user_id,),
    ).lastrowid
    conn.execute(
        "INSERT INTO storyline_chapters (storyline_id, seq, state)"
        " VALUES (?, 1, 'open')",
        (storyline_id,),
    )
    conn.commit()
    row = conn.execute(
        "SELECT title_locked, boundary_locked, narrative_word_count"
        " FROM storyline_chapters WHERE storyline_id = ?",
        (storyline_id,),
    ).fetchone()
    assert (row[0], row[1], row[2]) == (0, 0, 0)


def test_migration_0031_applies_cleanly_on_fresh_db(tmp_path: Path) -> None:
    """A fresh DB taken to version 30 then migrated gains the three columns."""
    db_path = tmp_path / "migration-0031.db"
    conn = get_connection(db_path)
    _run_migrations_up_to(conn, target_version=30)
    pre_cols = _columns(conn, "storyline_chapters")
    assert not (_NEW_COLUMNS & pre_cols), "columns should not exist pre-0031"

    run_migrations(conn)

    post_cols = _columns(conn, "storyline_chapters")
    assert post_cols >= _NEW_COLUMNS


def test_migration_0031_is_rerunnable(factory: ConnectionFactory) -> None:
    """A forward-only re-run version-skips 0031 and is a clean no-op.

    Also assert the additive ALTERs are individually safe by replaying the
    raw script through the idempotent executor on an already-migrated DB:
    the duplicate-column errors must be swallowed, not raised.
    """
    conn = factory.get()
    # Forward-only re-run: version-skips, must not raise.
    run_migrations(conn)
    assert _columns(conn, "storyline_chapters") >= _NEW_COLUMNS

    # Replay the raw 0031 script: each ADD COLUMN duplicates, which the
    # idempotent executor treats as a no-op rather than propagating.
    files = {f.name: f for f in get_migration_files()}
    sql = files[_MIGRATION_0031].read_text()
    _executescript_idempotent(conn, sql, _MIGRATION_0031)
    assert _columns(conn, "storyline_chapters") >= _NEW_COLUMNS
