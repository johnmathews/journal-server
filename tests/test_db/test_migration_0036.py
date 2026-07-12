"""Migration 0036: draft/published reshape of the storylines vertical."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

import pytest

from journal.db.migrations import (
    _executescript_idempotent,
    get_current_version,
    get_migration_files,
    run_migrations,
)

if TYPE_CHECKING:
    from pathlib import Path


def _migrate_to(conn: sqlite3.Connection, version: int) -> None:
    """Apply every migration up to and including ``version``.

    ``run_migrations`` has no "stop early" parameter (it always applies
    every pending file), so building a pre-0036 fixture mirrors it but
    stops once ``version`` is reached — the same pattern used by the
    (now-superseded) migration 0030 chapters test.
    """
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
def pre_0036(tmp_path: Path) -> sqlite3.Connection:
    """A DB at version 0035 with prod-shaped storyline data."""
    conn = sqlite3.connect(tmp_path / "m.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate_to(conn, 35)
    conn.execute(
        "INSERT INTO users (email, password_hash, display_name)"
        " VALUES ('u@x.dev', 'h', 'U')"
    )
    conn.execute(
        "INSERT INTO entries (entry_date, source_type, raw_text, final_text,"
        " word_count, user_id) VALUES ('2026-01-05', 'text', 'a', 'a', 1, 1),"
        " ('2026-02-10', 'text', 'b', 'b', 1, 1)"
    )
    conn.execute(
        "INSERT INTO storylines (user_id, name, description, status,"
        " last_generated_at) VALUES (1, 'Running', '', 'active',"
        " '2026-02-11T08:00:00Z')"
    )
    # entity row for the FK — must exist before the join row that
    # references it; entities.user_id is NOT NULL with no default.
    conn.execute(
        "INSERT INTO entities (user_id, entity_type, canonical_name)"
        " VALUES (1, 'activity', 'Running')"
    ) if _has_no_entity(conn) else None
    conn.execute("INSERT INTO storyline_entities (storyline_id, entity_id) VALUES (1, 1)")
    conn.execute(
        "INSERT INTO storyline_chapters (storyline_id, seq, title, start_date,"
        " end_date, state, last_generated_at) VALUES"
        " (1, 1, 'Winter', '2026-01-01', '2026-01-31', 'closed', '2026-02-01T00:00:00Z'),"
        " (1, 2, '', '2026-02-01', NULL, 'open', '2026-02-11T08:00:00Z')"
    )
    conn.execute(
        "INSERT INTO storyline_panels (chapter_id, panel_kind, segments_json,"
        " source_entry_ids_json, citation_count, model_used, generated_at)"
        # Chapter 1's panel generated_at ('2026-01-20T09:30:00Z') is
        # deliberately different from chapter 1's last_generated_at
        # ('2026-02-01T00:00:00Z') below, so the migration test can tell
        # apart which column each folded field is sourced from.
        " VALUES (1, 'narrative', ?, '[1]', 1, 'm', '2026-01-20T09:30:00Z'),"
        "        (1, 'curation',  '[]', '[]', 0, 'm', '2026-02-01T00:00:00Z'),"
        "        (2, 'narrative', ?, '[2]', 1, 'm', '2026-02-11T08:00:00Z')",
        (
            json.dumps([{"kind": "text", "text": "Jan prose"},
                        {"kind": "citation", "entry_id": 1, "quote": "a"}]),
            json.dumps([{"kind": "text", "text": "Feb prose"}]),
        ),
    )
    conn.commit()
    return conn


def _has_no_entity(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0


class TestMigration0036:
    def test_states_mapped_and_content_folded_in(self, pre_0036: sqlite3.Connection) -> None:
        run_migrations(pre_0036)
        rows = pre_0036.execute(
            "SELECT id, seq, state, title, segments_json, generated_at,"
            " published_at, read_at FROM storyline_chapters ORDER BY seq"
        ).fetchall()
        assert [r["state"] for r in rows] == ["published", "draft"]
        # generated_at is folded from the narrative panel, not the chapter's
        # last_generated_at (its four sibling folded columns all come from
        # the panel too); published_at still comes from last_generated_at.
        assert rows[0]["generated_at"] == "2026-01-20T09:30:00Z"  # from panel.generated_at
        assert rows[0]["published_at"] == "2026-02-01T00:00:00Z"  # from last_generated_at
        assert rows[0]["read_at"] is not None  # pre-read: no fake unread badges
        assert rows[1]["published_at"] is None
        assert json.loads(rows[0]["segments_json"])[0]["text"] == "Jan prose"

    def test_membership_backfilled_from_narrative_source_ids(
        self, pre_0036: sqlite3.Connection,
    ) -> None:
        run_migrations(pre_0036)
        rows = pre_0036.execute(
            "SELECT chapter_id, entry_id FROM storyline_chapter_entries ORDER BY chapter_id"
        ).fetchall()
        assert [(r["chapter_id"], r["entry_id"]) for r in rows] == [(1, 1), (2, 2)]

    def test_panels_preserved_as_legacy_and_dropped(
        self, pre_0036: sqlite3.Connection,
    ) -> None:
        run_migrations(pre_0036)
        names = {r[0] for r in pre_0036.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "storyline_panels_legacy" in names
        assert "storyline_panels" not in names
        assert pre_0036.execute(
            "SELECT COUNT(*) FROM storyline_panels_legacy"
        ).fetchone()[0] == 3

    def test_one_draft_partial_index_enforced(self, pre_0036: sqlite3.Connection) -> None:
        run_migrations(pre_0036)
        with pytest.raises(sqlite3.IntegrityError):
            pre_0036.execute(
                "INSERT INTO storyline_chapters (storyline_id, seq, state)"
                " VALUES (1, 3, 'draft')"
            )

    def test_storyline_columns_dropped(self, pre_0036: sqlite3.Connection) -> None:
        run_migrations(pre_0036)
        cols = {r["name"] for r in pre_0036.execute(
            "PRAGMA table_info(storylines)"
        )}
        assert "start_date" not in cols
        assert "end_date" not in cols
        assert "summary_embedding_json" not in cols
        assert "last_generated_at" not in cols
        assert "last_extension_check_at" in cols

    def test_storyline_without_open_chapter_gets_empty_draft(
        self, pre_0036: sqlite3.Connection,
    ) -> None:
        # Prod anomaly guard: a storyline whose only chapter is closed.
        pre_0036.execute(
            "INSERT INTO storylines (user_id, name, status) VALUES (1, 'Odd', 'active')"
        )
        pre_0036.execute(
            "INSERT INTO storyline_chapters (storyline_id, seq, title, start_date,"
            " end_date, state) VALUES (2, 1, 'Only', '2026-01-01', '2026-01-31', 'closed')"
        )
        pre_0036.commit()
        run_migrations(pre_0036)
        states = [r[0] for r in pre_0036.execute(
            "SELECT state FROM storyline_chapters WHERE storyline_id = 2 ORDER BY seq"
        )]
        assert states == ["published", "draft"]

    def test_rerunnable_after_partial_failure(self, pre_0036: sqlite3.Connection) -> None:
        run_migrations(pre_0036)
        # Simulate a re-run: reset user_version and run again — must not raise.
        pre_0036.execute("PRAGMA user_version = 35")
        run_migrations(pre_0036)
        # The re-run must land in the same end state, not duplicate or
        # corrupt chapters: still one published + one draft per storyline,
        # and the one-draft-per-storyline invariant still enforced.
        states = [r[0] for r in pre_0036.execute(
            "SELECT state FROM storyline_chapters WHERE storyline_id = 1 ORDER BY seq"
        )]
        assert states == ["published", "draft"]
        with pytest.raises(sqlite3.IntegrityError):
            pre_0036.execute(
                "INSERT INTO storyline_chapters (storyline_id, seq, state)"
                " VALUES (1, 99, 'draft')"
            )
