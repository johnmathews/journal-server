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
            entity_ids=[seed_entity],
            name="Running",
            description="My running thread",
        )
        assert s.id > 0
        assert s.user_id == seed_user
        assert s.name == "Running"
        assert s.description == "My running thread"
        assert s.status == "active"
        assert s.created_at != ""
        assert s.last_generated_at is None
        # Single-anchor create populates storyline_entities.
        assert storyline_repo.list_anchors(s.id) == [seed_entity]

    def test_create_with_multiple_anchors(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        factory: ConnectionFactory,
    ) -> None:
        store = SQLiteEntityStore(factory)
        ents = [
            store.create_entity(
                entity_type="person",
                canonical_name=f"Person-{i}",
                description="",
                first_seen="2026-01-01",
                user_id=seed_user,
            )
            for i in range(3)
        ]
        s = storyline_repo.create_storyline(
            user_id=seed_user,
            entity_ids=[ents[2].id, ents[0].id, ents[1].id],  # unsorted input
            name="Trio",
        )
        # Anchors stored, returned sorted ASC.
        assert storyline_repo.list_anchors(s.id) == sorted(e.id for e in ents)

    def test_create_dedupes_repeated_entity_ids(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = storyline_repo.create_storyline(
            user_id=seed_user,
            entity_ids=[seed_entity, seed_entity, seed_entity],
            name="Solo",
        )
        assert storyline_repo.list_anchors(s.id) == [seed_entity]

    def test_create_rejects_empty_entity_ids(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
    ) -> None:
        with pytest.raises(ValueError):
            storyline_repo.create_storyline(
                user_id=seed_user, entity_ids=[], name="Empty",
            )

    def test_get_storyline_with_user_filter(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[seed_entity], name="A",
        )
        # Found for the right user
        fetched = storyline_repo.get_storyline(s.id, user_id=seed_user)
        assert fetched is not None
        assert fetched.id == s.id
        # Not found for a wrong user
        assert storyline_repo.get_storyline(s.id, user_id=seed_user + 999) is None
        # Found without user filter
        assert storyline_repo.get_storyline(s.id, user_id=None) is not None

    def test_find_by_anchor_set_exact_match_only(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        factory: ConnectionFactory,
    ) -> None:
        store = SQLiteEntityStore(factory)
        a = store.create_entity(
            entity_type="person", canonical_name="A",
            description="", first_seen="2026-01-01", user_id=seed_user,
        )
        b = store.create_entity(
            entity_type="person", canonical_name="B",
            description="", first_seen="2026-01-01", user_id=seed_user,
        )
        c = store.create_entity(
            entity_type="person", canonical_name="C",
            description="", first_seen="2026-01-01", user_id=seed_user,
        )
        s_ab = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[a.id, b.id], name="Pair",
        )
        # Exact match — order-insensitive.
        assert storyline_repo.find_by_anchor_set(
            seed_user, [b.id, a.id], "Pair",
        ).id == s_ab.id
        # Subset = no match.
        assert storyline_repo.find_by_anchor_set(
            seed_user, [a.id], "Pair",
        ) is None
        # Superset = no match.
        assert storyline_repo.find_by_anchor_set(
            seed_user, [a.id, b.id, c.id], "Pair",
        ) is None
        # Different name = no match.
        assert storyline_repo.find_by_anchor_set(
            seed_user, [a.id, b.id], "Different",
        ) is None
        # Different user = no match.
        assert storyline_repo.find_by_anchor_set(
            seed_user + 999, [a.id, b.id], "Pair",
        ) is None

    def test_list_filters_and_pagination(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        factory: ConnectionFactory,
    ) -> None:
        store = SQLiteEntityStore(factory)
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
                entity_ids=[ent.id],
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
            user_id=seed_user, entity_ids=[seed_entity], name="A",
        )
        # Wrong user cannot delete
        assert storyline_repo.delete_storyline(s.id, user_id=seed_user + 999) is False
        assert storyline_repo.get_storyline(s.id) is not None
        # Owner deletes
        assert storyline_repo.delete_storyline(s.id, user_id=seed_user) is True
        assert storyline_repo.get_storyline(s.id) is None

    def test_update_storyline_name_renames_and_trims(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[seed_entity], name="Old name",
        )
        updated = storyline_repo.update_storyline_name(
            s.id, "  New name  ", user_id=seed_user,
        )
        assert updated is not None
        assert updated.name == "New name"
        # Persisted, not just echoed.
        refreshed = storyline_repo.get_storyline(s.id, user_id=seed_user)
        assert refreshed is not None
        assert refreshed.name == "New name"
        # updated_at is bumped on rename.
        assert refreshed.updated_at != ""

    def test_update_storyline_name_only_for_owner(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[seed_entity], name="Mine",
        )
        # Wrong user: no row updated, returns None, name unchanged.
        assert (
            storyline_repo.update_storyline_name(
                s.id, "Hijacked", user_id=seed_user + 999,
            )
            is None
        )
        refreshed = storyline_repo.get_storyline(s.id)
        assert refreshed is not None
        assert refreshed.name == "Mine"

    def test_record_generation_complete_updates_timestamp(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[seed_entity], name="A",
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
            user_id=seed_user, entity_ids=[seed_entity], name="A",
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


# ── Anchors ──────────────────────────────────────────────────────


class TestAnchors:
    @pytest.fixture
    def three_entities(
        self, factory: ConnectionFactory, seed_user: int,
    ) -> list[int]:
        store = SQLiteEntityStore(factory)
        return [
            store.create_entity(
                entity_type="person",
                canonical_name=f"P{i}",
                description="",
                first_seen="2026-01-01",
                user_id=seed_user,
            ).id
            for i in range(3)
        ]

    def test_set_anchors_replaces_atomically(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        three_entities: list[int],
    ) -> None:
        a, b, c = three_entities
        s = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[a, b], name="AB",
        )
        result = storyline_repo.set_anchors(s.id, [b, c])
        assert result == sorted([b, c])
        assert storyline_repo.list_anchors(s.id) == sorted([b, c])
        # No leftover join rows for the dropped anchor.
        assert a not in storyline_repo.list_anchors(s.id)

    def test_set_anchors_rejects_empty(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[seed_entity], name="A",
        )
        with pytest.raises(ValueError):
            storyline_repo.set_anchors(s.id, [])

    def test_add_anchor_is_idempotent(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        three_entities: list[int],
    ) -> None:
        a, b, c = three_entities
        s = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[a], name="A",
        )
        storyline_repo.add_anchor(s.id, b)
        storyline_repo.add_anchor(s.id, b)  # re-add, no duplicate
        assert storyline_repo.list_anchors(s.id) == sorted([a, b])

    def test_remove_anchor_returns_deletion_flag(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        three_entities: list[int],
    ) -> None:
        a, b, _ = three_entities
        s = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[a, b], name="AB",
        )
        assert storyline_repo.remove_anchor(s.id, a) is True
        # Re-removing returns False — already gone.
        assert storyline_repo.remove_anchor(s.id, a) is False
        assert storyline_repo.list_anchors(s.id) == [b]

    def test_list_storylines_with_anchor_returns_all_relevant(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        three_entities: list[int],
    ) -> None:
        a, b, c = three_entities
        s_ab = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[a, b], name="AB",
        )
        s_bc = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[b, c], name="BC",
        )
        s_a = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[a], name="A-only",
        )
        # b is in both s_ab and s_bc, not in s_a.
        rows = storyline_repo.list_storylines_with_anchor(seed_user, b)
        assert {r.id for r in rows} == {s_ab.id, s_bc.id}
        # a is in s_ab and s_a, not s_bc.
        rows = storyline_repo.list_storylines_with_anchor(seed_user, a)
        assert {r.id for r in rows} == {s_ab.id, s_a.id}

    def test_list_storylines_with_anchor_filters_by_status(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        three_entities: list[int],
    ) -> None:
        a, b, _ = three_entities
        s_active = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[a, b], name="active",
        )
        s_arch = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[a, b], name="archived",
        )
        storyline_repo.update_storyline_status(s_arch.id, "archived", seed_user)

        active_only = storyline_repo.list_storylines_with_anchor(
            seed_user, a, status="active",
        )
        assert [r.id for r in active_only] == [s_active.id]

    def test_delete_storyline_cascades_to_anchors(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        three_entities: list[int],
        factory: ConnectionFactory,
    ) -> None:
        a, b, _ = three_entities
        s = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[a, b], name="AB",
        )
        factory.get().execute("PRAGMA foreign_keys=ON")
        storyline_repo.delete_storyline(s.id, user_id=seed_user)
        row = factory.get().execute(
            "SELECT COUNT(*) AS cnt FROM storyline_entities"
            " WHERE storyline_id = ?",
            (s.id,),
        ).fetchone()
        assert int(row["cnt"]) == 0


# ── Panels ───────────────────────────────────────────────────────


class TestStorylinePanels:
    def test_upsert_curation_panel(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[seed_entity], name="A",
        )
        ch = storyline_repo.create_chapter(storyline_id=s.id, seq=1, title="Ch1")
        segs = [
            text_segment("On the 1st:"),
            citation_segment(101, "I ran 5km today"),
            text_segment("Three days later:"),
            citation_segment(102, "I ran 8km, felt great"),
        ]
        panel = storyline_repo.upsert_panel(
            chapter_id=ch.id,
            panel_kind="curation",
            segments=segs,
            source_entry_ids=[101, 102],
            citation_count=2,
            model_used="claude-haiku-4-5",
        )
        assert panel.chapter_id == ch.id
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
            user_id=seed_user, entity_ids=[seed_entity], name="A",
        )
        ch = storyline_repo.create_chapter(storyline_id=s.id, seq=1, title="Ch1")
        v1 = [text_segment("First version")]
        v2 = [text_segment("Second version")]
        storyline_repo.upsert_panel(
            ch.id, "narrative", v1, [], 0, "claude-opus-4-7",
        )
        storyline_repo.upsert_panel(
            ch.id, "narrative", v2, [], 0, "claude-opus-4-7",
        )
        panel = storyline_repo.get_panel(ch.id, "narrative")
        assert panel is not None
        assert panel.segments == v2

    def test_list_panels_returns_both_kinds(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[seed_entity], name="A",
        )
        ch = storyline_repo.create_chapter(storyline_id=s.id, seq=1, title="Ch1")
        storyline_repo.upsert_panel(ch.id, "curation", [], [], 0, "haiku")
        storyline_repo.upsert_panel(ch.id, "narrative", [], [], 0, "opus")
        panels = storyline_repo.list_panels(ch.id)
        kinds = sorted(p.panel_kind for p in panels)
        assert kinds == ["curation", "narrative"]

    def test_upsert_and_get_panel_by_chapter(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        sl = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[seed_entity], name="X",
        )
        ch = storyline_repo.create_chapter(storyline_id=sl.id, seq=1, title="Ch1")
        panel = storyline_repo.upsert_panel(
            chapter_id=ch.id,
            panel_kind="narrative",
            segments=[{"kind": "text", "text": "hi"}],
            source_entry_ids=[1],
            citation_count=0,
            model_used="m",
        )
        assert panel.chapter_id == ch.id
        got = storyline_repo.get_panel(ch.id, "narrative")
        assert got is not None and got.segments[0]["text"] == "hi"
        assert [p.panel_kind for p in storyline_repo.list_panels(ch.id)] == [
            "narrative"
        ]

    def test_delete_storyline_cascades_to_panels(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
        factory: ConnectionFactory,
    ) -> None:
        s = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[seed_entity], name="A",
        )
        ch = storyline_repo.create_chapter(storyline_id=s.id, seq=1, title="Ch1")
        storyline_repo.upsert_panel(ch.id, "curation", [], [], 0, "haiku")
        # FK cascade requires PRAGMA foreign_keys=ON for SQLite. Ensure it.
        # Deleting the storyline cascades to its chapters, which cascades to
        # the chapter's panels.
        factory.get().execute("PRAGMA foreign_keys=ON")
        storyline_repo.delete_storyline(s.id, user_id=seed_user)
        row = factory.get().execute(
            "SELECT COUNT(*) AS cnt FROM storyline_panels"
            " WHERE chapter_id = ?",
            (ch.id,),
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


# ── Chapter CRUD ─────────────────────────────────────────────────


class TestChapterCRUD:
    def test_create_and_list_chapters(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        sl = storyline_repo.create_storyline(
            user_id=seed_user,
            entity_ids=[seed_entity],
            name="Run thread",
            start_date="2026-01-01",
            end_date="2026-03-01",
        )
        # create_storyline does not auto-create a chapter; create it explicitly.
        ch = storyline_repo.create_chapter(
            storyline_id=sl.id,
            seq=1,
            title="Ch 1",
            start_date="2026-01-01",
            end_date="2026-03-01",
            state="open",
        )
        assert ch.seq == 1
        assert ch.state == "open"
        assert ch.start_date == "2026-01-01"
        assert ch.end_date == "2026-03-01"
        assert ch.created_at != ""
        chapters = storyline_repo.list_chapters(sl.id)
        assert [c.id for c in chapters] == [ch.id]
        open_chapter = storyline_repo.get_open_chapter(sl.id)
        assert open_chapter is not None
        assert open_chapter.id == ch.id

    def test_get_open_chapter(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        sl = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[seed_entity], name="X",
        )
        # No chapters yet → no open chapter.
        assert storyline_repo.get_open_chapter(sl.id) is None
        closed = storyline_repo.create_chapter(
            storyline_id=sl.id, seq=1, title="Old", state="closed",
        )
        # Still no open chapter while only a closed one exists.
        assert storyline_repo.get_open_chapter(sl.id) is None
        open_ch = storyline_repo.create_chapter(
            storyline_id=sl.id, seq=2, title="New", state="open",
        )
        got = storyline_repo.get_open_chapter(sl.id)
        assert got is not None
        assert got.id == open_ch.id
        assert got.id != closed.id

    def test_rename_chapter(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        sl = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[seed_entity], name="X",
        )
        ch = storyline_repo.create_chapter(storyline_id=sl.id, seq=1, title="Old")
        updated = storyline_repo.rename_chapter(ch.id, "New Title")
        assert updated is not None
        assert updated.title == "New Title"
        # Unknown chapter id returns None.
        assert storyline_repo.rename_chapter(999_999, "Nope") is None

    def test_record_chapter_generation_complete(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        sl = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[seed_entity], name="X",
        )
        ch = storyline_repo.create_chapter(storyline_id=sl.id, seq=1)
        assert ch.last_generated_at is None
        assert storyline_repo.get_storyline(sl.id).last_generated_at is None
        storyline_repo.record_chapter_generation_complete(ch.id)
        refreshed = storyline_repo.get_chapter(ch.id)
        assert refreshed is not None
        assert refreshed.last_generated_at is not None
        # The parent storyline's timestamp is bumped too, so the UI's
        # "last generated" column reflects the fresh chapter content
        # rather than showing a stale date forever.
        parent = storyline_repo.get_storyline(sl.id)
        assert parent is not None
        assert parent.last_generated_at is not None

    def test_update_chapter_summary_embedding(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        sl = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[seed_entity], name="X",
        )
        ch = storyline_repo.create_chapter(storyline_id=sl.id, seq=1)
        assert ch.summary_embedding is None
        storyline_repo.update_chapter_summary_embedding(ch.id, [0.1, 0.2, 0.3])
        refreshed = storyline_repo.get_chapter(ch.id)
        assert refreshed is not None
        assert refreshed.summary_embedding == [0.1, 0.2, 0.3]
        # Clearing it back to None.
        storyline_repo.update_chapter_summary_embedding(ch.id, None)
        cleared = storyline_repo.get_chapter(ch.id)
        assert cleared is not None
        assert cleared.summary_embedding is None

    def test_get_chapter_unknown_returns_none(
        self,
        storyline_repo: SQLiteStorylineRepository,
    ) -> None:
        assert storyline_repo.get_chapter(999_999) is None


class TestChapterSectioning:
    """W1: title/boundary locks + cached narrative word count."""

    def _storyline(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> int:
        sl = storyline_repo.create_storyline(
            user_id=seed_user, entity_ids=[seed_entity], name="X",
        )
        return sl.id

    def test_new_fields_default_false_and_zero(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        sid = self._storyline(storyline_repo, seed_user, seed_entity)
        # A plain create_chapter is NOT a hand-paint, so it does not lock
        # the boundary — only the dedicated "add chapter" op does.
        ch = storyline_repo.create_chapter(storyline_id=sid, seq=1)
        assert ch.title_locked is False
        assert ch.boundary_locked is False
        assert ch.narrative_word_count == 0
        refreshed = storyline_repo.get_chapter(ch.id)
        assert refreshed is not None
        assert refreshed.title_locked is False
        assert refreshed.boundary_locked is False
        assert refreshed.narrative_word_count == 0

    def test_rename_chapter_sets_title_locked(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        sid = self._storyline(storyline_repo, seed_user, seed_entity)
        ch = storyline_repo.create_chapter(storyline_id=sid, seq=1, title="Old")
        assert ch.title_locked is False
        updated = storyline_repo.rename_chapter(ch.id, "New Title")
        assert updated is not None
        assert updated.title == "New Title"
        assert updated.title_locked is True

    def test_add_chapter_sets_boundary_locked(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        sid = self._storyline(storyline_repo, seed_user, seed_entity)
        # Seed an open chapter, then hand-paint a later one via add_chapter.
        storyline_repo.create_chapter(
            storyline_id=sid, seq=1, start_date="2026-01-01", state="open",
        )
        added = storyline_repo.add_chapter(sid, start_date="2026-02-01")
        assert added.boundary_locked is True

    def test_split_chapter_locks_both_boundaries(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        sid = self._storyline(storyline_repo, seed_user, seed_entity)
        ch = storyline_repo.create_chapter(
            storyline_id=sid,
            seq=1,
            start_date="2026-01-01",
            end_date="2026-03-01",
            state="closed",
        )
        left, right = storyline_repo.split_chapter(ch.id, "2026-02-01")
        assert left.boundary_locked is True
        assert right.boundary_locked is True

    def test_update_chapter_window_locks_boundary(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        sid = self._storyline(storyline_repo, seed_user, seed_entity)
        ch = storyline_repo.create_chapter(
            storyline_id=sid,
            seq=1,
            start_date="2026-01-01",
            end_date="2026-03-01",
            state="closed",
        )
        changed = storyline_repo.update_chapter_window(
            ch.id, start_date="2026-01-05", end_date="2026-03-01",
        )
        edited = next(c for c in changed if c.id == ch.id)
        assert edited.boundary_locked is True

    def test_set_chapter_word_count_persists(
        self,
        storyline_repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        sid = self._storyline(storyline_repo, seed_user, seed_entity)
        ch = storyline_repo.create_chapter(storyline_id=sid, seq=1)
        assert ch.narrative_word_count == 0
        storyline_repo.set_chapter_word_count(ch.id, 212)
        refreshed = storyline_repo.get_chapter(ch.id)
        assert refreshed is not None
        assert refreshed.narrative_word_count == 212
