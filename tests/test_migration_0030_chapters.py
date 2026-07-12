"""Migration 0030: storylines split into chapters; panels re-keyed.

Pinned to the schema exactly as it stood right after 0030 landed. Migration
0036 (storylines-redesign) later rebuilt both ``storyline_chapters`` and
``storyline_panels`` again (open/closed -> draft/published, panels folded
into the chapter row, ``storyline_panels`` dropped) — see
``tests/test_db/test_migration_0036.py`` for that end-state. These tests
build a database that stops at version 30 rather than running the full
migration chain, so they keep asserting the 0030 end-state instead of
drifting as later migrations reshape the same tables.
"""

from __future__ import annotations

# Runtime import (not type-checking only): `_columns` and the migration
# helpers below take a live `sqlite3.Connection` and call its methods, so the
# module is used at runtime, not merely for annotations.
import sqlite3  # noqa: TC003
from typing import TYPE_CHECKING

from journal.db.connection import get_connection
from journal.db.migrations import (
    _executescript_idempotent,
    get_current_version,
    get_migration_files,
)

if TYPE_CHECKING:
    from pathlib import Path


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _run_migrations_up_to(conn: sqlite3.Connection, target_version: int) -> None:
    """Apply every PENDING migration up to and including ``target_version``.

    Pins a fixture at ``target_version`` so the migration-under-test can be
    applied and asserted on in isolation, without later migrations (0036 in
    particular) reshaping the same tables out from under the assertions.
    Mirrors ``run_migrations``'s own forward-only skip logic (a migration
    whose version is <= the connection's current ``PRAGMA user_version`` is
    never re-executed) but stops once ``target_version`` is reached instead
    of running to the latest file — so calling this twice at the same
    target is a genuine no-op re-run, exactly like the real runner.
    """
    current = get_current_version(conn)
    for migration_file in get_migration_files():
        version = int(migration_file.stem.split("_")[0])
        if version <= current:
            continue
        if version > target_version:
            break
        _executescript_idempotent(
            conn, migration_file.read_text(), migration_file.name,
        )
        conn.execute(f"PRAGMA user_version = {version}")
        current = version


def _conn_at_0030(tmp_path: Path, name: str = "m.db") -> sqlite3.Connection:
    """A fresh DB migrated up to and including 0030, no further."""
    conn = get_connection(tmp_path / name)
    _run_migrations_up_to(conn, target_version=30)
    return conn


def _seed_user_entity_storyline(
    conn: sqlite3.Connection,
) -> int:
    """Insert a minimal user + entity + storyline (post-0028 schema, no
    ``entity_id`` column on storylines). Returns the storyline id."""
    user_id = conn.execute(
        "INSERT INTO users (email, password_hash, display_name)"
        " VALUES ('a@b.c', 'x', 'A')",
    ).lastrowid
    conn.execute(
        "INSERT INTO entities (user_id, entity_type, canonical_name)"
        " VALUES (?, 'activity', 'Running')",
        (user_id,),
    )
    storyline_id = conn.execute(
        "INSERT INTO storylines (user_id, name, start_date, end_date,"
        " last_generated_at) VALUES (?, 'Running thread', '2026-01-01',"
        " '2026-03-01', '2026-03-02T00:00:00Z')",
        (user_id,),
    ).lastrowid
    assert storyline_id is not None
    return storyline_id


def test_chapters_table_exists_and_panels_rekeyed(tmp_path: Path) -> None:
    conn = _conn_at_0030(tmp_path)
    assert "storyline_chapters" in {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    cols = _columns(conn, "storyline_panels")
    assert "chapter_id" in cols
    assert "storyline_id" not in cols  # FK moved


def test_migration_moves_old_storyline_keyed_panels_to_chapter(
    tmp_path: Path,
) -> None:
    """Exercise the REAL data-migration path on prod-shaped state.

    The ``factory`` fixture has already applied 0030, so a panel inserted
    through it is keyed on ``chapter_id`` and never travels the migration's
    JOIN. This test instead builds the database at the pre-0030 schema,
    inserts prod-shaped rows in the OLD layout (``storyline_panels`` keyed on
    ``storyline_id``, no ``chapter_id``), then applies 0030 and asserts the
    JOIN (``... JOIN storyline_chapters c ON c.storyline_id = p.storyline_id
    AND c.seq = 1``) moved both panels onto the new chapter. If that JOIN were
    wrong, prod panel data would be silently dropped — this test fails loudly
    in that case.
    """
    db_path = tmp_path / "migration-0030.db"
    conn = get_connection(db_path)
    # Pre-0030 schema: storylines + panels exist, chapters do not.
    _run_migrations_up_to(conn, target_version=29)

    # Sanity: we are genuinely at the OLD panel schema (keyed on
    # storyline_id), which is what the migration JOIN reads from.
    pre_cols = _columns(conn, "storyline_panels")
    assert "storyline_id" in pre_cols, (
        "expected pre-0030 storyline_panels to be keyed on storyline_id"
    )
    assert "chapter_id" not in pre_cols
    assert "storyline_chapters" not in {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    storyline_id = _seed_user_entity_storyline(conn)
    # Two prod-shaped panels in the OLD layout, keyed on storyline_id.
    conn.execute(
        "INSERT INTO storyline_panels (storyline_id, panel_kind,"
        " segments_json, source_entry_ids_json, citation_count, model_used)"
        " VALUES (?, 'curation', '[]', '[]', 0, 'm')",
        (storyline_id,),
    )
    conn.execute(
        "INSERT INTO storyline_panels (storyline_id, panel_kind,"
        " segments_json, source_entry_ids_json, citation_count, model_used)"
        " VALUES (?, 'narrative', '[]', '[]', 0, 'm')",
        (storyline_id,),
    )
    conn.commit()

    # Apply ONLY 0030 — pin to target_version=30 rather than calling
    # run_migrations (which would also apply 0031-0036 and reshape these
    # same tables again before we get to assert on the 0030 end-state).
    _run_migrations_up_to(conn, target_version=30)

    # Exactly one chapter for the storyline: seq 1, state 'open'.
    chapters = conn.execute(
        "SELECT id, seq, state FROM storyline_chapters WHERE storyline_id = ?",
        (storyline_id,),
    ).fetchall()
    assert len(chapters) == 1
    chapter_id, seq, state = chapters[0]
    assert (seq, state) == (1, "open")

    # The old storyline_id column is gone; panels are now keyed on chapter_id.
    post_cols = _columns(conn, "storyline_panels")
    assert "chapter_id" in post_cols
    assert "storyline_id" not in post_cols

    # Both panels survived the JOIN and are reachable by the chapter's id.
    moved = conn.execute(
        "SELECT panel_kind FROM storyline_panels WHERE chapter_id = ?"
        " ORDER BY panel_kind",
        (chapter_id,),
    ).fetchall()
    assert [r[0] for r in moved] == ["curation", "narrative"]
    # No panels were orphaned onto some other chapter_id.
    total = conn.execute("SELECT COUNT(*) FROM storyline_panels").fetchone()[0]
    assert total == 2


def test_backfill_creates_one_open_chapter_and_moves_panels(
    tmp_path: Path,
) -> None:
    conn = _conn_at_0030(tmp_path)
    # Seed a user + entity + storyline + a panel the way prod looks, at the
    # post-0030 schema.
    storyline_id = _seed_user_entity_storyline(conn)
    conn.execute(
        "INSERT INTO storyline_chapters (storyline_id, seq, title, start_date,"
        " end_date, state) VALUES (?, 1, 'Running thread', '2026-01-01',"
        " '2026-03-01', 'open')",
        (storyline_id,),
    )
    chapter_id = conn.execute(
        "SELECT id FROM storyline_chapters WHERE storyline_id = ?",
        (storyline_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO storyline_panels (chapter_id, panel_kind, segments_json,"
        " source_entry_ids_json, citation_count, model_used) VALUES"
        " (?, 'narrative', '[]', '[]', 0, 'm')",
        (chapter_id,),
    )
    conn.commit()

    # Exactly one open chapter, seq 1, dates copied verbatim.
    row = conn.execute(
        "SELECT seq, state, start_date, end_date FROM storyline_chapters"
        " WHERE storyline_id = ?",
        (storyline_id,),
    ).fetchone()
    assert (row[0], row[1], row[2], row[3]) == (1, "open", "2026-01-01", "2026-03-01")
    # Panel is reachable by chapter_id.
    cnt = conn.execute(
        "SELECT COUNT(*) FROM storyline_panels WHERE chapter_id = ?",
        (chapter_id,),
    ).fetchone()[0]
    assert cnt == 1


def test_migration_is_rerunnable(tmp_path: Path) -> None:
    conn = _conn_at_0030(tmp_path)
    # Seed a storyline so the backfill has something to act on, mirroring
    # prod. Pinned at 0030, so this storyline post-dates 0030 and has no
    # chapter yet.
    storyline_id = _seed_user_entity_storyline(conn)
    conn.execute(
        "INSERT INTO storyline_chapters (storyline_id, seq, title, start_date,"
        " end_date, state) VALUES (?, 1, 'Running thread', '2026-01-01',"
        " '2026-03-01', 'open')",
        (storyline_id,),
    )
    conn.commit()

    # The pinned helper mirrors the real runner's forward-only skip logic:
    # a second call at the same target_version is a genuine no-op (it
    # skips 0030 since PRAGMA user_version is already 30) rather than
    # replaying the migration's SQL — the panel rebuild in 0030 reads a
    # pre-migration column that no longer exists, so raw-script replay is
    # not itself idempotent (see the 0030 file's own header comment).
    _run_migrations_up_to(conn, target_version=30)

    dupes = conn.execute(
        "SELECT storyline_id, COUNT(*) c FROM storyline_chapters"
        " GROUP BY storyline_id HAVING c > 1"
    ).fetchall()
    assert dupes == []

    # Exactly one open chapter for the seeded storyline.
    open_count = conn.execute(
        "SELECT COUNT(*) FROM storyline_chapters"
        " WHERE storyline_id = ? AND state = 'open'",
        (storyline_id,),
    ).fetchone()[0]
    assert open_count == 1
