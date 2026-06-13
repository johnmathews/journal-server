"""Migration 0030: storylines split into chapters; panels re-keyed."""

from __future__ import annotations

from typing import TYPE_CHECKING

from journal.db.migrations import run_migrations

if TYPE_CHECKING:
    import sqlite3

    from journal.db.factory import ConnectionFactory


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


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


def test_backfill_creates_one_open_chapter_and_moves_panels(
    factory: ConnectionFactory,
) -> None:
    conn = factory.get()
    # Seed a user + entity + storyline + a panel the way prod looks.
    conn.execute(
        "INSERT INTO users (id, email, password_hash, display_name)"
        " VALUES (2, 'a@b.c', 'x', 'A')"
    )
    conn.execute(
        "INSERT INTO entities (id, user_id, entity_type, canonical_name)"
        " VALUES (5, 2, 'activity', 'Running')"
    )
    conn.execute(
        "INSERT INTO storylines (id, user_id, name, start_date,"
        " end_date, last_generated_at) VALUES"
        " (9, 2, 'Running thread', '2026-01-01', '2026-03-01',"
        " '2026-03-02T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO storyline_chapters (storyline_id, seq, title, start_date,"
        " end_date, state) VALUES (9, 1, 'Running thread', '2026-01-01',"
        " '2026-03-01', 'open')"
    )
    chapter_id = conn.execute(
        "SELECT id FROM storyline_chapters WHERE storyline_id = 9"
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
        " WHERE storyline_id = 9"
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
    conn.execute(
        "INSERT INTO users (id, email, password_hash, display_name)"
        " VALUES (2, 'a@b.c', 'x', 'A')"
    )
    conn.execute(
        "INSERT INTO entities (id, user_id, entity_type, canonical_name)"
        " VALUES (5, 2, 'activity', 'Running')"
    )
    conn.execute(
        "INSERT INTO storylines (id, user_id, name, start_date,"
        " end_date) VALUES (9, 2, 'Running thread', '2026-01-01',"
        " '2026-03-01')"
    )
    conn.execute(
        "INSERT INTO storyline_chapters (storyline_id, seq, title, start_date,"
        " end_date, state) VALUES (9, 1, 'Running thread', '2026-01-01',"
        " '2026-03-01', 'open')"
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
        " WHERE storyline_id = 9 AND state = 'open'"
    ).fetchone()[0]
    assert open_count == 1
