"""Tests for database migrations."""

import pytest

from journal.db.connection import get_connection
from journal.db.migrations import get_current_version, run_migrations


def test_initial_version_is_zero(tmp_db_path):
    conn = get_connection(tmp_db_path)
    assert get_current_version(conn) == 0
    conn.close()


def test_migrations_apply(db_conn):
    version = get_current_version(db_conn)
    assert version >= 1


def test_migrations_are_idempotent(db_conn):
    version_before = get_current_version(db_conn)
    run_migrations(db_conn)
    version_after = get_current_version(db_conn)
    assert version_before == version_after


def test_entries_table_exists(db_conn):
    tables = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='entries'"
    ).fetchone()
    assert tables is not None


def test_fts_table_exists(db_conn):
    # FTS5 virtual tables appear in sqlite_master
    result = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='entries_fts'"
    ).fetchone()
    assert result is not None


def test_foreign_keys_enabled(db_conn):
    result = db_conn.execute("PRAGMA foreign_keys").fetchone()
    assert result[0] == 1


def test_entry_pages_table_exists(db_conn):
    tables = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='entry_pages'"
    ).fetchone()
    assert tables is not None


def test_entries_has_final_text_column(db_conn):
    columns = db_conn.execute("PRAGMA table_info(entries)").fetchall()
    column_names = [col["name"] for col in columns]
    assert "final_text" in column_names
    assert "chunk_count" in column_names


def test_migration_version_is_at_least_2(db_conn):
    version = get_current_version(db_conn)
    assert version >= 2


def test_entry_chunks_table_exists(db_conn):
    row = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='entry_chunks'"
    ).fetchone()
    assert row is not None


def test_entry_chunks_has_expected_columns(db_conn):
    columns = db_conn.execute("PRAGMA table_info(entry_chunks)").fetchall()
    column_names = {col["name"] for col in columns}
    assert {
        "id",
        "entry_id",
        "chunk_index",
        "chunk_text",
        "char_start",
        "char_end",
        "token_count",
        "created_at",
    } <= column_names


def test_entry_chunks_cascade_delete(db_conn):
    db_conn.execute(
        "INSERT INTO entries (user_id, entry_date, source_type, raw_text, word_count)"
        " VALUES (1, '2026-03-22', 'ocr', 'x', 1)"
    )
    entry_id = db_conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    db_conn.execute(
        "INSERT INTO entry_chunks "
        "(entry_id, chunk_index, chunk_text, char_start, char_end, token_count)"
        " VALUES (?, 0, 'x', 0, 1, 1)",
        (entry_id,),
    )
    db_conn.commit()
    db_conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    db_conn.commit()
    count = db_conn.execute(
        "SELECT COUNT(*) AS n FROM entry_chunks WHERE entry_id = ?", (entry_id,)
    ).fetchone()["n"]
    assert count == 0


def test_migration_version_is_at_least_3(db_conn):
    version = get_current_version(db_conn)
    assert version >= 3


def test_migration_version_is_at_least_5(db_conn):
    version = get_current_version(db_conn)
    assert version >= 5


def test_entry_uncertain_spans_table_exists(db_conn):
    row = db_conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='entry_uncertain_spans'"
    ).fetchone()
    assert row is not None


def test_entry_uncertain_spans_has_expected_columns(db_conn):
    columns = db_conn.execute(
        "PRAGMA table_info(entry_uncertain_spans)"
    ).fetchall()
    column_names = {col["name"] for col in columns}
    assert {
        "id",
        "entry_id",
        "char_start",
        "char_end",
        "created_at",
    } <= column_names


def test_entry_uncertain_spans_index_exists(db_conn):
    row = db_conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_uncertain_spans_entry_id'"
    ).fetchone()
    assert row is not None


def test_entry_uncertain_spans_cascade_delete(db_conn):
    db_conn.execute(
        "INSERT INTO entries (user_id, entry_date, source_type, raw_text, word_count)"
        " VALUES (1, '2026-03-22', 'ocr', 'hello world', 2)"
    )
    entry_id = db_conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    db_conn.execute(
        "INSERT INTO entry_uncertain_spans (entry_id, char_start, char_end)"
        " VALUES (?, 0, 5)",
        (entry_id,),
    )
    db_conn.commit()
    db_conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    db_conn.commit()
    count = db_conn.execute(
        "SELECT COUNT(*) AS n FROM entry_uncertain_spans WHERE entry_id = ?",
        (entry_id,),
    ).fetchone()["n"]
    assert count == 0


class TestEntityQuarantineMigration:
    """Migration 0018 adds quarantine columns to the entities table."""

    def test_entities_has_quarantine_columns(self, db_conn):
        columns = db_conn.execute("PRAGMA table_info(entities)").fetchall()
        names = {col["name"] for col in columns}
        assert {"is_quarantined", "quarantine_reason", "quarantined_at"} <= names

    def test_quarantine_defaults(self, db_conn):
        """Existing rows + new rows default to is_quarantined = 0 with
        empty reason and timestamp strings."""
        db_conn.execute(
            "INSERT INTO entities (user_id, entity_type, canonical_name)"
            " VALUES (1, 'person', 'TestSeed')"
        )
        db_conn.commit()
        row = db_conn.execute(
            "SELECT is_quarantined, quarantine_reason, quarantined_at"
            " FROM entities WHERE canonical_name = 'TestSeed'"
        ).fetchone()
        assert row["is_quarantined"] == 0
        assert row["quarantine_reason"] == ""
        assert row["quarantined_at"] == ""

    def test_quarantine_index_exists(self, db_conn):
        row = db_conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_entities_quarantined'"
        ).fetchone()
        assert row is not None

    def test_migration_version_at_least_18(self, db_conn):
        from journal.db.migrations import get_current_version
        assert get_current_version(db_conn) >= 18


def test_entry_uncertain_spans_check_constraints(db_conn):
    db_conn.execute(
        "INSERT INTO entries (user_id, entry_date, source_type, raw_text, word_count)"
        " VALUES (1, '2026-03-22', 'ocr', 'hello world', 2)"
    )
    entry_id = db_conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    db_conn.commit()

    # char_start must be >= 0
    import sqlite3 as _sqlite3
    with pytest.raises(_sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO entry_uncertain_spans (entry_id, char_start, char_end)"
            " VALUES (?, -1, 5)",
            (entry_id,),
        )
        db_conn.commit()
    db_conn.rollback()

    # char_end must be > char_start
    with pytest.raises(_sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO entry_uncertain_spans (entry_id, char_start, char_end)"
            " VALUES (?, 5, 5)",
            (entry_id,),
        )
        db_conn.commit()
    db_conn.rollback()
