"""Tests for SQLiteStorylineRepository + the dated-entity-excerpts query.

Covers W3 of the storylines plan:

* CRUD on the storylines table (create / get / list / update / delete).
* Panel upsert + read (curation and narrative kinds).
* The `_MentionsMixin.get_dated_entity_excerpts` query that powers the
  generation service's source-corpus fetch.
* The segments helpers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from journal.db.storyline_repository import SQLiteStorylineRepository
from journal.entitystore.store import SQLiteEntityStore
from journal.services.storylines.segments import (
    citation_segment,
    collect_source_entry_ids,
    count_citations,
    is_valid_segment,
    text_segment,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from journal.db.factory import ConnectionFactory


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def seed_user(factory: ConnectionFactory) -> int:
    """Insert a minimal user row and return its id."""
    conn = factory.get()
    cursor = conn.execute(
        "INSERT INTO users (email, password_hash, display_name)"
        " VALUES (?, ?, ?)",
        ("test@example.com", "x", "Test User"),
    )
    conn.commit()
    user_id = cursor.lastrowid
    assert user_id is not None
    return user_id


@pytest.fixture
def seed_entity(
    factory: ConnectionFactory, seed_user: int,
) -> int:
    """Insert one 'activity' entity (Running) and return its id."""
    store = SQLiteEntityStore(factory)
    entity = store.create_entity(
        entity_type="activity",
        canonical_name="Running",
        description="The activity of running",
        first_seen="2026-02-15",
        user_id=seed_user,
    )
    return entity.id


@pytest.fixture
def storyline_repo(
    factory: ConnectionFactory,
) -> Generator[SQLiteStorylineRepository]:
    yield SQLiteStorylineRepository(factory)


# ── Storyline CRUD ───────────────────────────────────────────────


class TestStorylineCRUD:
    def test_create_returns_populated_storyline(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = storyline_repo.create_storyline(
            user_id=seed_user,
            entity_id=seed_entity,
            name="Running",
            description="My running thread",
        )
        assert s.id > 0
        assert s.user_id == seed_user
        assert s.entity_id == seed_entity
        assert s.name == "Running"
        assert s.description == "My running thread"
        assert s.status == "active"
        assert s.created_at != ""
        assert s.last_generated_at is None

    def test_get_storyline_with_user_filter(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = storyline_repo.create_storyline(
            user_id=seed_user, entity_id=seed_entity, name="A",
        )
        # Found for the right user
        fetched = storyline_repo.get_storyline(s.id, user_id=seed_user)
        assert fetched is not None
        assert fetched.id == s.id
        # Not found for a wrong user
        assert storyline_repo.get_storyline(s.id, user_id=seed_user + 999) is None
        # Found without user filter
        assert storyline_repo.get_storyline(s.id, user_id=None) is not None

    def test_unique_constraint_on_user_entity_name(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        import sqlite3
        storyline_repo.create_storyline(
            user_id=seed_user, entity_id=seed_entity, name="Running",
        )
        with pytest.raises(sqlite3.IntegrityError):
            storyline_repo.create_storyline(
                user_id=seed_user, entity_id=seed_entity, name="Running",
            )

    def test_find_by_entity_returns_match_or_none(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        assert storyline_repo.find_by_entity(seed_user, seed_entity) is None
        s = storyline_repo.create_storyline(
            user_id=seed_user, entity_id=seed_entity, name="A",
        )
        found = storyline_repo.find_by_entity(seed_user, seed_entity)
        assert found is not None and found.id == s.id

    def test_list_filters_and_pagination(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        factory: ConnectionFactory,
    ) -> None:
        store = SQLiteEntityStore(factory)
        # Three storylines on three different entities so the UNIQUE
        # constraint doesn't fight us.
        ids: list[int] = []
        for i in range(3):
            ent = store.create_entity(
                entity_type="activity",
                canonical_name=f"Activity-{i}",
                description="",
                first_seen="2026-01-01",
                user_id=seed_user,
            )
            s = storyline_repo.create_storyline(
                user_id=seed_user,
                entity_id=ent.id,
                name=f"Storyline-{i}",
            )
            ids.append(s.id)

        all_list = storyline_repo.list_storylines(seed_user)
        assert len(all_list) == 3
        assert storyline_repo.count_storylines(seed_user) == 3

        # Archive one
        storyline_repo.update_storyline_status(ids[0], "archived", seed_user)
        active = storyline_repo.list_storylines(seed_user, status="active")
        archived = storyline_repo.list_storylines(seed_user, status="archived")
        assert len(active) == 2
        assert len(archived) == 1
        assert storyline_repo.count_storylines(seed_user, status="active") == 2

        # Pagination
        page = storyline_repo.list_storylines(seed_user, limit=2, offset=0)
        assert len(page) == 2
        page2 = storyline_repo.list_storylines(seed_user, limit=2, offset=2)
        assert len(page2) == 1

    def test_delete_only_succeeds_for_owner(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = storyline_repo.create_storyline(
            user_id=seed_user, entity_id=seed_entity, name="A",
        )
        # Wrong user cannot delete
        assert storyline_repo.delete_storyline(s.id, user_id=seed_user + 999) is False
        assert storyline_repo.get_storyline(s.id) is not None
        # Owner deletes
        assert storyline_repo.delete_storyline(s.id, user_id=seed_user) is True
        assert storyline_repo.get_storyline(s.id) is None

    def test_record_generation_complete_updates_timestamp(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = storyline_repo.create_storyline(
            user_id=seed_user, entity_id=seed_entity, name="A",
        )
        assert s.last_generated_at is None
        storyline_repo.record_generation_complete(s.id)
        refreshed = storyline_repo.get_storyline(s.id)
        assert refreshed is not None
        assert refreshed.last_generated_at is not None
        assert refreshed.last_generated_at.startswith("20")

    def test_summary_embedding_roundtrip(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = storyline_repo.create_storyline(
            user_id=seed_user, entity_id=seed_entity, name="A",
        )
        assert s.summary_embedding is None
        storyline_repo.update_summary_embedding(s.id, [0.1, 0.2, 0.3])
        refreshed = storyline_repo.get_storyline(s.id)
        assert refreshed is not None
        assert refreshed.summary_embedding == [0.1, 0.2, 0.3]
        # Clearing
        storyline_repo.update_summary_embedding(s.id, None)
        cleared = storyline_repo.get_storyline(s.id)
        assert cleared is not None
        assert cleared.summary_embedding is None


# ── Panels ───────────────────────────────────────────────────────


class TestStorylinePanels:
    def test_upsert_curation_panel(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = storyline_repo.create_storyline(
            user_id=seed_user, entity_id=seed_entity, name="A",
        )
        segs = [
            text_segment("On the 1st:"),
            citation_segment(101, "I ran 5km today"),
            text_segment("Three days later:"),
            citation_segment(102, "I ran 8km, felt great"),
        ]
        panel = storyline_repo.upsert_panel(
            storyline_id=s.id,
            panel_kind="curation",
            segments=segs,
            source_entry_ids=[101, 102],
            citation_count=2,
            model_used="claude-haiku-4-5",
        )
        assert panel.storyline_id == s.id
        assert panel.panel_kind == "curation"
        assert panel.segments == segs
        assert panel.source_entry_ids == [101, 102]
        assert panel.citation_count == 2
        assert panel.model_used == "claude-haiku-4-5"

    def test_upsert_is_idempotent_per_kind(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = storyline_repo.create_storyline(
            user_id=seed_user, entity_id=seed_entity, name="A",
        )
        v1 = [text_segment("First version")]
        v2 = [text_segment("Second version")]
        storyline_repo.upsert_panel(
            s.id, "narrative", v1, [], 0, "claude-opus-4-7",
        )
        storyline_repo.upsert_panel(
            s.id, "narrative", v2, [], 0, "claude-opus-4-7",
        )
        panel = storyline_repo.get_panel(s.id, "narrative")
        assert panel is not None
        assert panel.segments == v2

    def test_list_panels_returns_both_kinds(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = storyline_repo.create_storyline(
            user_id=seed_user, entity_id=seed_entity, name="A",
        )
        storyline_repo.upsert_panel(s.id, "curation", [], [], 0, "haiku")
        storyline_repo.upsert_panel(s.id, "narrative", [], [], 0, "opus")
        panels = storyline_repo.list_panels(s.id)
        kinds = sorted(p.panel_kind for p in panels)
        assert kinds == ["curation", "narrative"]

    def test_delete_storyline_cascades_to_panels(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
        factory: ConnectionFactory,
    ) -> None:
        s = storyline_repo.create_storyline(
            user_id=seed_user, entity_id=seed_entity, name="A",
        )
        storyline_repo.upsert_panel(s.id, "curation", [], [], 0, "haiku")
        # FK cascade requires PRAGMA foreign_keys=ON for SQLite. Ensure it.
        factory.get().execute("PRAGMA foreign_keys=ON")
        storyline_repo.delete_storyline(s.id, user_id=seed_user)
        row = factory.get().execute(
            "SELECT COUNT(*) AS cnt FROM storyline_panels"
            " WHERE storyline_id = ?",
            (s.id,),
        ).fetchone()
        assert int(row["cnt"]) == 0


# ── get_dated_entity_excerpts ────────────────────────────────────


class TestDatedEntityExcerpts:
    def _seed_entries_and_mentions(
        self,
        factory: ConnectionFactory,
        user_id: int,
        entity_id: int,
    ) -> list[int]:
        """Seed three entries with running mentions across the 3-month window.
        Returns the entry ids in the order they were created."""
        conn = factory.get()
        entry_ids: list[int] = []
        rows = [
            ("2026-02-20", "text", "I ran 5km today", "I ran 5km today"),
            ("2026-03-15", "text", "Long Saturday run.", "Long Saturday run."),
            ("2026-04-25", "text", "I ran 11km yesterday.", "I ran 11km yesterday."),
        ]
        for entry_date, source_type, raw, final in rows:
            cur = conn.execute(
                "INSERT INTO entries"
                " (entry_date, source_type, raw_text, final_text,"
                "  word_count, user_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    entry_date, source_type, raw, final,
                    len(final.split()), user_id,
                ),
            )
            assert cur.lastrowid is not None
            entry_ids.append(cur.lastrowid)
        for entry_id, (_, _, _, final) in zip(entry_ids, rows, strict=True):
            conn.execute(
                "INSERT INTO entity_mentions"
                " (entity_id, entry_id, quote, confidence, extraction_run_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (entity_id, entry_id, final, 0.95, "run-1"),
            )
        conn.commit()
        return entry_ids

    def test_chronological_order_with_date_range(
        self,
        factory: ConnectionFactory,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        self._seed_entries_and_mentions(factory, seed_user, seed_entity)
        store = SQLiteEntityStore(factory)
        excerpts = store.get_dated_entity_excerpts(
            entity_id=seed_entity,
            user_id=seed_user,
            start_date="2026-02-12",
            end_date="2026-05-12",
        )
        dates = [e.entry_date for e in excerpts]
        assert dates == sorted(dates)
        assert dates == ["2026-02-20", "2026-03-15", "2026-04-25"]
        # Each excerpt carries the matching quote
        assert all(len(e.quotes) >= 1 for e in excerpts)

    def test_date_range_filters(
        self,
        factory: ConnectionFactory,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        self._seed_entries_and_mentions(factory, seed_user, seed_entity)
        store = SQLiteEntityStore(factory)
        excerpts = store.get_dated_entity_excerpts(
            entity_id=seed_entity,
            user_id=seed_user,
            start_date="2026-03-01",
            end_date="2026-04-01",
        )
        assert len(excerpts) == 1
        assert excerpts[0].entry_date == "2026-03-15"

    def test_user_isolation(
        self,
        factory: ConnectionFactory,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        self._seed_entries_and_mentions(factory, seed_user, seed_entity)
        store = SQLiteEntityStore(factory)
        excerpts = store.get_dated_entity_excerpts(
            entity_id=seed_entity, user_id=seed_user + 999,
        )
        assert excerpts == []

    def test_multiple_quotes_per_entry_aggregate(
        self,
        factory: ConnectionFactory,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        conn = factory.get()
        cur = conn.execute(
            "INSERT INTO entries"
            " (entry_date, source_type, raw_text, final_text,"
            "  word_count, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                "2026-04-25", "text",
                "I ran. Then ran again.", "I ran. Then ran again.",
                5, seed_user,
            ),
        )
        entry_id = cur.lastrowid
        for quote in ("I ran.", "Then ran again."):
            conn.execute(
                "INSERT INTO entity_mentions"
                " (entity_id, entry_id, quote, confidence, extraction_run_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (seed_entity, entry_id, quote, 0.95, "run-1"),
            )
        conn.commit()
        store = SQLiteEntityStore(factory)
        excerpts = store.get_dated_entity_excerpts(
            entity_id=seed_entity, user_id=seed_user,
        )
        assert len(excerpts) == 1
        assert excerpts[0].quotes == ["I ran.", "Then ran again."]


# ── Segments helpers ─────────────────────────────────────────────


class TestSegments:
    def test_text_segment_shape(self) -> None:
        assert text_segment("hi") == {"kind": "text", "text": "hi"}

    def test_citation_segment_shape(self) -> None:
        assert citation_segment(42, "quoted") == {
            "kind": "citation",
            "entry_id": 42,
            "quote": "quoted",
        }

    def test_collect_source_entry_ids_dedupes_preserving_order(self) -> None:
        segs = [
            text_segment("a"),
            citation_segment(3, "x"),
            text_segment("b"),
            citation_segment(7, "y"),
            citation_segment(3, "x2"),  # dup
        ]
        assert collect_source_entry_ids(segs) == [3, 7]

    def test_count_citations_does_not_dedupe(self) -> None:
        segs = [
            citation_segment(1, "x"),
            citation_segment(1, "x"),
            text_segment("z"),
        ]
        assert count_citations(segs) == 2

    @pytest.mark.parametrize(
        "value,expected",
        [
            ({"kind": "text", "text": "ok"}, True),
            ({"kind": "citation", "entry_id": 1, "quote": "q"}, True),
            ({"kind": "citation", "entry_id": "1", "quote": "q"}, False),
            ({"kind": "other"}, False),
            ("plain string", False),
        ],
    )
    def test_is_valid_segment(self, value: object, expected: bool) -> None:  # noqa: ANN401
        assert is_valid_segment(value) is expected
