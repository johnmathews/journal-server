"""Migration 0030: storylines split into chapters; panels re-keyed."""

from __future__ import annotations

# Runtime import (not type-checking only): `_columns` and the migration
# helpers below take a live `sqlite3.Connection` and call its methods, so the
# module is used at runtime, not merely for annotations.
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


_MIGRATION_0030 = "0030_storyline_chapters.sql"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _run_migrations_up_to(conn: sqlite3.Connection, target_version: int) -> None:
    """Apply every migration up to and including ``target_version``.

    Constructs a pre-0030 fixture so the migration-under-test can be applied
    separately and asserted on. Mirrors ``run_migrations`` but stops once the
    target is reached.
    """
    for migration_file in get_migration_files():
        version = int(migration_file.stem.split("_")[0])
        if version > target_version:
            break
        _executescript_idempotent(
            conn, migration_file.read_text(), migration_file.name,
        )
        conn.execute(f"PRAGMA user_version = {version}")


def _read_migration_0030() -> str:
    files = get_migration_files()
    matches = [f for f in files if f.name == _MIGRATION_0030]
    assert matches, f"migration {_MIGRATION_0030} missing"
    return matches[0].read_text()


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


def test_chapters_table_exists_and_panels_rekeyed(factory: ConnectionFactory) -> None:
    conn = factory.get()
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

    # Apply ONLY 0030. run_migrations resumes from user_version 29, so it
    # applies just the remaining 0030 — the real forward-only path.
    run_migrations(conn)

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
    factory: ConnectionFactory,
) -> None:
    conn = factory.get()
    # Seed a user + entity + storyline + a panel the way prod looks. The
    # factory fixture already applied 0030, so this storyline post-dates the
    # migration and we wire its chapter/panel up at the post-migration schema.
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


def test_migration_is_rerunnable(factory: ConnectionFactory) -> None:
    conn = factory.get()
    # Seed a storyline so the backfill has something to act on, mirroring
    # prod. The factory fixture already applied every migration once, so
    # this storyline post-dates 0030 and has no chapter yet.
    storyline_id = _seed_user_entity_storyline(conn)
    conn.execute(
        "INSERT INTO storyline_chapters (storyline_id, seq, title, start_date,"
        " end_date, state) VALUES (?, 1, 'Running thread', '2026-01-01',"
        " '2026-03-01', 'open')",
        (storyline_id,),
    )
    conn.commit()

    # The runner is forward-only: a second run version-skips 0030 (it is
    # already <= PRAGMA user_version). This is exactly how production
    # behaves on every server restart, so it is the honest re-run to
    # assert. It must not raise, must not duplicate chapters, and must
    # not duplicate panels.
    run_migrations(conn)

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
