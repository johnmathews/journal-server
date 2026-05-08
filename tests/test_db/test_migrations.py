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


class TestEntityPairDecisionsMigration:
    """Migration 0021 adds the entity_pair_decisions table for
    persistent "not a duplicate" memory across extraction runs."""

    def test_table_exists(self, db_conn):
        row = db_conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='entity_pair_decisions'"
        ).fetchone()
        assert row is not None

    def test_has_expected_columns(self, db_conn):
        columns = db_conn.execute(
            "PRAGMA table_info(entity_pair_decisions)"
        ).fetchall()
        names = {col["name"] for col in columns}
        assert {
            "id",
            "user_id",
            "entity_id_lo",
            "entity_id_hi",
            "decision",
            "decided_at",
        } <= names

    def test_check_lo_lt_hi(self, db_conn):
        # Seed an entity so the FK is satisfied.
        db_conn.execute(
            "INSERT INTO entities (user_id, entity_type, canonical_name)"
            " VALUES (1, 'person', 'A')"
        )
        eid = db_conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        db_conn.commit()

        import sqlite3 as _sqlite3
        with pytest.raises(_sqlite3.IntegrityError):
            db_conn.execute(
                "INSERT INTO entity_pair_decisions"
                " (user_id, entity_id_lo, entity_id_hi, decision)"
                " VALUES (1, ?, ?, 'rejected')",
                (eid, eid),  # equal — violates lo < hi
            )
            db_conn.commit()
        db_conn.rollback()

    def test_unique_per_user_pair(self, db_conn):
        db_conn.execute(
            "INSERT INTO entities (user_id, entity_type, canonical_name)"
            " VALUES (1, 'person', 'A'), (1, 'person', 'B')"
        )
        rows = db_conn.execute(
            "SELECT id FROM entities ORDER BY id DESC LIMIT 2"
        ).fetchall()
        hi = max(r["id"] for r in rows)
        lo = min(r["id"] for r in rows)
        db_conn.execute(
            "INSERT INTO entity_pair_decisions"
            " (user_id, entity_id_lo, entity_id_hi, decision)"
            " VALUES (1, ?, ?, 'rejected')",
            (lo, hi),
        )
        db_conn.commit()

        import sqlite3 as _sqlite3
        with pytest.raises(_sqlite3.IntegrityError):
            db_conn.execute(
                "INSERT INTO entity_pair_decisions"
                " (user_id, entity_id_lo, entity_id_hi, decision)"
                " VALUES (1, ?, ?, 'rejected')",
                (lo, hi),
            )
            db_conn.commit()
        db_conn.rollback()


class TestMergeCandidatesPairUniqueMigration:
    """Migration 0022 rebuilds entity_merge_candidates with per-pair
    UNIQUE and a CHECK(entity_id_a < entity_id_b)."""

    @staticmethod
    def _run_migrations_up_to(conn, max_version: int) -> None:
        """Apply migration files with version <= max_version.

        Mirrors ``run_migrations`` but stops early so we can drop
        custom state in before the next migration runs — needed to
        reproduce prod conditions for migration 0022.
        """
        from journal.db.migrations import (
            get_current_version,
            get_migration_files,
        )
        current = get_current_version(conn)
        for path in get_migration_files():
            file_version = int(path.stem.split("_")[0])
            if file_version <= current:
                continue
            if file_version > max_version:
                break
            conn.executescript(path.read_text())
            conn.execute(f"PRAGMA user_version = {file_version}")
            current = file_version

    @staticmethod
    def _run_migration_file(conn, version: int) -> None:
        """Apply a single migration file by version number."""
        from journal.db.migrations import get_migration_files
        for path in get_migration_files():
            if int(path.stem.split("_")[0]) == version:
                conn.executescript(path.read_text())
                conn.execute(f"PRAGMA user_version = {version}")
                return
        raise AssertionError(f"Migration {version} not found")

    def test_runs_against_orphan_source_rows(self, tmp_db_path):
        """Regression for prod failure: migration 0022 used to crash with
        ``FOREIGN KEY constraint failed`` when the source table contained
        a row referencing an entity that had been deleted out from under
        it. Real prod data had exactly one such orphan."""
        from journal.db.connection import get_connection

        conn = get_connection(tmp_db_path)
        self._run_migrations_up_to(conn, 21)

        # Seed two valid entities (A,B) and a candidate referencing them,
        # plus one orphan candidate where entity_id_b points at id=999
        # (no such entity). FK is toggled off for the orphan insert
        # because the source table itself enforces the FK; orphans only
        # exist in real DBs because something bypassed FK in the past.
        conn.execute(
            "INSERT INTO entities (id, user_id, entity_type, canonical_name)"
            " VALUES (1, 1, 'person', 'A'), (2, 1, 'person', 'B'),"
            "        (3, 1, 'person', 'C')"
        )
        conn.execute(
            "INSERT INTO entity_merge_candidates"
            " (entity_id_a, entity_id_b, similarity, extraction_run_id)"
            " VALUES (1, 2, 0.9, 'run-1'),"
            "        (1, 3, 0.85, 'run-1')"
        )
        conn.commit()
        # PRAGMA foreign_keys is a no-op inside a transaction, so commit
        # first, then toggle, insert the orphan, and toggle back.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO entity_merge_candidates"
            " (entity_id_a, entity_id_b, similarity, extraction_run_id)"
            " VALUES (1, 999, 0.7, 'run-1')"
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")

        # Sanity-check we wrote 3 rows including the orphan.
        n_before = conn.execute(
            "SELECT COUNT(*) FROM entity_merge_candidates"
        ).fetchone()[0]
        assert n_before == 3

        self._run_migration_file(conn, 22)

        # Orphan dropped, valid pairs preserved (one row per pair).
        rows = conn.execute(
            "SELECT entity_id_a, entity_id_b, similarity"
            " FROM entity_merge_candidates"
            " ORDER BY entity_id_a, entity_id_b"
        ).fetchall()
        assert len(rows) == 2
        assert (rows[0]["entity_id_a"], rows[0]["entity_id_b"]) == (1, 2)
        assert (rows[1]["entity_id_a"], rows[1]["entity_id_b"]) == (1, 3)

        # Sanity-check schema version + new constraints in place.
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 22
        conn.close()

    def test_idempotent_after_partial_failure(self, tmp_db_path):
        """Regression for prod failure: when 0022 crashed mid-script
        (orphan FK violation), the freshly-created
        ``entity_merge_candidates_new`` table survived. Re-running the
        migration must drop the leftover table cleanly."""
        from journal.db.connection import get_connection

        conn = get_connection(tmp_db_path)
        self._run_migrations_up_to(conn, 21)

        # Simulate the post-crash state: a leftover ``_new`` table.
        conn.execute(
            "CREATE TABLE entity_merge_candidates_new ("
            " id INTEGER PRIMARY KEY,"
            " entity_id_a INTEGER, entity_id_b INTEGER,"
            " similarity REAL, status TEXT, extraction_run_id TEXT,"
            " created_at TEXT, updated_at TEXT, resolved_at TEXT)"
        )
        conn.commit()

        # Migration must succeed despite the leftover.
        self._run_migration_file(conn, 22)

        # Final table is the renamed-from-new one; the leftover is gone.
        tables = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name LIKE 'entity_merge_candidates%'"
            ).fetchall()
        }
        assert tables == {"entity_merge_candidates"}
        conn.close()

    def test_unique_per_pair(self, db_conn):
        db_conn.execute(
            "INSERT INTO entities (user_id, entity_type, canonical_name)"
            " VALUES (1, 'person', 'A'), (1, 'person', 'B')"
        )
        rows = db_conn.execute(
            "SELECT id FROM entities ORDER BY id DESC LIMIT 2"
        ).fetchall()
        hi = max(r["id"] for r in rows)
        lo = min(r["id"] for r in rows)
        db_conn.execute(
            "INSERT INTO entity_merge_candidates"
            " (entity_id_a, entity_id_b, similarity, extraction_run_id)"
            " VALUES (?, ?, 0.9, 'run-1')",
            (lo, hi),
        )
        db_conn.commit()

        # Same pair with a different run id used to be allowed; now rejected.
        import sqlite3 as _sqlite3
        with pytest.raises(_sqlite3.IntegrityError):
            db_conn.execute(
                "INSERT INTO entity_merge_candidates"
                " (entity_id_a, entity_id_b, similarity, extraction_run_id)"
                " VALUES (?, ?, 0.9, 'run-2')",
                (lo, hi),
            )
            db_conn.commit()
        db_conn.rollback()

    def test_check_a_lt_b(self, db_conn):
        db_conn.execute(
            "INSERT INTO entities (user_id, entity_type, canonical_name)"
            " VALUES (1, 'person', 'A'), (1, 'person', 'B')"
        )
        rows = db_conn.execute(
            "SELECT id FROM entities ORDER BY id DESC LIMIT 2"
        ).fetchall()
        hi = max(r["id"] for r in rows)
        lo = min(r["id"] for r in rows)

        # Inserting with reversed order violates CHECK(a<b)
        import sqlite3 as _sqlite3
        with pytest.raises(_sqlite3.IntegrityError):
            db_conn.execute(
                "INSERT INTO entity_merge_candidates"
                " (entity_id_a, entity_id_b, similarity, extraction_run_id)"
                " VALUES (?, ?, 0.9, 'run-1')",
                (hi, lo),
            )
            db_conn.commit()
        db_conn.rollback()


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
