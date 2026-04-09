"""Tests for database migrations."""


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
