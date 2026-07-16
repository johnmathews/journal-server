"""Tests for SQLiteStorylineRepository + the dated-entity-excerpts query.

Covers the storylines redesign (spec: docs/superpowers/specs/2026-07-12-
storylines-redesign-design.md), repository rewrite task:

* CRUD on the storylines table (create / get / list / update / delete).
* Anchors (storyline_entities join table).
* Chapter lifecycle: seeded draft, publish/unpublish transactions.
* Immutability guards (draft-only vs published-only operations).
* Derived chapter fields (entry_count/first/last entry date) + unread counts.
* Pending (matched-but-unassigned) entries.
* Bootstrap replace-all-chapters.
* The `_MentionsMixin.get_dated_entity_excerpts` query that powers the
  generation service's source-corpus fetch.
* The segments helpers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from journal.db.storyline_repository import (
    BootstrapChapterSpec,
    SQLiteStorylineRepository,
)
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
    from journal.models import Storyline


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
def repo(
    factory: ConnectionFactory,
) -> Generator[SQLiteStorylineRepository]:
    yield SQLiteStorylineRepository(factory)


@pytest.fixture
def storyline(
    repo: SQLiteStorylineRepository, seed_user: int, seed_entity: int,
) -> Storyline:
    return repo.create_storyline(seed_user, [seed_entity], "Running")


@pytest.fixture
def entry_ids(factory: ConnectionFactory, seed_user: int) -> list[int]:
    """Seed three entries spanning 2026-02-20 .. 2026-04-25, in date order."""
    conn = factory.get()
    ids: list[int] = []
    rows = [
        ("2026-02-20", "I ran 5km today"),
        ("2026-03-15", "Long Saturday run."),
        ("2026-04-25", "I ran 11km yesterday."),
    ]
    for entry_date, text in rows:
        cursor = conn.execute(
            "INSERT INTO entries"
            " (entry_date, source_type, raw_text, final_text,"
            "  word_count, user_id)"
            " VALUES (?, 'text', ?, ?, ?, ?)",
            (entry_date, text, text, len(text.split()), seed_user),
        )
        assert cursor.lastrowid is not None
        ids.append(cursor.lastrowid)
    conn.commit()
    return ids


# ── Storyline CRUD ───────────────────────────────────────────────


class TestStorylineCRUD:
    def test_create_returns_populated_storyline(
        self,
        repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = repo.create_storyline(
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
        # Single-anchor create populates storyline_entities.
        assert repo.list_anchors(s.id) == [seed_entity]

    def test_create_with_multiple_anchors(
        self,
        repo: SQLiteStorylineRepository,
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
        s = repo.create_storyline(
            user_id=seed_user,
            entity_ids=[ents[2].id, ents[0].id, ents[1].id],  # unsorted input
            name="Trio",
        )
        # Anchors stored, returned sorted ASC.
        assert repo.list_anchors(s.id) == sorted(e.id for e in ents)

    def test_create_dedupes_repeated_entity_ids(
        self,
        repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = repo.create_storyline(
            user_id=seed_user,
            entity_ids=[seed_entity, seed_entity, seed_entity],
            name="Solo",
        )
        assert repo.list_anchors(s.id) == [seed_entity]

    def test_create_rejects_empty_entity_ids(
        self,
        repo: SQLiteStorylineRepository,
        seed_user: int,
    ) -> None:
        with pytest.raises(ValueError):
            repo.create_storyline(
                user_id=seed_user, entity_ids=[], name="Empty",
            )

    def test_get_storyline_with_user_filter(
        self,
        repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = repo.create_storyline(
            user_id=seed_user, entity_ids=[seed_entity], name="A",
        )
        # Found for the right user
        fetched = repo.get_storyline(s.id, user_id=seed_user)
        assert fetched is not None
        assert fetched.id == s.id
        # Not found for a wrong user
        assert repo.get_storyline(s.id, user_id=seed_user + 999) is None
        # Found without user filter
        assert repo.get_storyline(s.id, user_id=None) is not None

    def test_find_by_anchor_set_exact_match_only(
        self,
        repo: SQLiteStorylineRepository,
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
        s_ab = repo.create_storyline(
            user_id=seed_user, entity_ids=[a.id, b.id], name="Pair",
        )
        # Exact match — order-insensitive.
        assert repo.find_by_anchor_set(
            seed_user, [b.id, a.id], "Pair",
        ).id == s_ab.id
        # Subset = no match.
        assert repo.find_by_anchor_set(
            seed_user, [a.id], "Pair",
        ) is None
        # Superset = no match.
        assert repo.find_by_anchor_set(
            seed_user, [a.id, b.id, c.id], "Pair",
        ) is None
        # Different name = no match.
        assert repo.find_by_anchor_set(
            seed_user, [a.id, b.id], "Different",
        ) is None
        # Different user = no match.
        assert repo.find_by_anchor_set(
            seed_user + 999, [a.id, b.id], "Pair",
        ) is None

    def test_list_filters_and_pagination(
        self,
        repo: SQLiteStorylineRepository,
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
            s = repo.create_storyline(
                user_id=seed_user,
                entity_ids=[ent.id],
                name=f"Storyline-{i}",
            )
            ids.append(s.id)

        all_list = repo.list_storylines(seed_user)
        assert len(all_list) == 3
        assert repo.count_storylines(seed_user) == 3

        # Archive one
        repo.update_storyline_status(ids[0], "archived", seed_user)
        active = repo.list_storylines(seed_user, status="active")
        archived = repo.list_storylines(seed_user, status="archived")
        assert len(active) == 2
        assert len(archived) == 1
        assert repo.count_storylines(seed_user, status="active") == 2

        # Pagination
        page = repo.list_storylines(seed_user, limit=2, offset=0)
        assert len(page) == 2
        page2 = repo.list_storylines(seed_user, limit=2, offset=2)
        assert len(page2) == 1

    def test_delete_only_succeeds_for_owner(
        self,
        repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = repo.create_storyline(
            user_id=seed_user, entity_ids=[seed_entity], name="A",
        )
        # Wrong user cannot delete
        assert repo.delete_storyline(s.id, user_id=seed_user + 999) is False
        assert repo.get_storyline(s.id) is not None
        # Owner deletes
        assert repo.delete_storyline(s.id, user_id=seed_user) is True
        assert repo.get_storyline(s.id) is None

    def test_update_storyline_name_renames_and_trims(
        self,
        repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = repo.create_storyline(
            user_id=seed_user, entity_ids=[seed_entity], name="Old name",
        )
        updated = repo.update_storyline_name(
            s.id, "  New name  ", user_id=seed_user,
        )
        assert updated is not None
        assert updated.name == "New name"
        # Persisted, not just echoed.
        refreshed = repo.get_storyline(s.id, user_id=seed_user)
        assert refreshed is not None
        assert refreshed.name == "New name"
        # updated_at is bumped on rename.
        assert refreshed.updated_at != ""

    def test_update_storyline_name_only_for_owner(
        self,
        repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = repo.create_storyline(
            user_id=seed_user, entity_ids=[seed_entity], name="Mine",
        )
        # Wrong user: no row updated, returns None, name unchanged.
        assert (
            repo.update_storyline_name(
                s.id, "Hijacked", user_id=seed_user + 999,
            )
            is None
        )
        refreshed = repo.get_storyline(s.id)
        assert refreshed is not None
        assert refreshed.name == "Mine"

    def test_record_extension_check_updates_timestamp(
        self,
        repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = repo.create_storyline(
            user_id=seed_user, entity_ids=[seed_entity], name="A",
        )
        assert s.last_extension_check_at is None
        repo.record_extension_check(s.id)
        refreshed = repo.get_storyline(s.id)
        assert refreshed is not None
        assert refreshed.last_extension_check_at is not None
        assert refreshed.last_extension_check_at.startswith("20")


# ── Whole-dataset text search (W1) ───────────────────────────────


class TestListSearch:
    """`list_storylines(search=...)` + `count_storylines(search=...)`:
    whole-dataset text search over name + description, evaluated in SQL
    before LIMIT/OFFSET so `total` reflects the filtered count."""

    def _make(
        self,
        repo: SQLiteStorylineRepository,
        factory: ConnectionFactory,
        user_id: int,
        name: str,
        description: str = "",
    ) -> Storyline:
        store = SQLiteEntityStore(factory)
        ent = store.create_entity(
            entity_type="activity",
            canonical_name=f"E-{name}-{description}",
            description="",
            first_seen="2026-01-01",
            user_id=user_id,
        )
        return repo.create_storyline(
            user_id=user_id, entity_ids=[ent.id], name=name,
            description=description,
        )

    def test_search_matches_name(
        self,
        repo: SQLiteStorylineRepository,
        factory: ConnectionFactory,
        seed_user: int,
    ) -> None:
        self._make(repo, factory, seed_user, "Running")
        self._make(repo, factory, seed_user, "Cooking")
        rows = repo.list_storylines(seed_user, search="Run")
        assert [r.name for r in rows] == ["Running"]

    def test_search_matches_description(
        self,
        repo: SQLiteStorylineRepository,
        factory: ConnectionFactory,
        seed_user: int,
    ) -> None:
        self._make(repo, factory, seed_user, "Alpha", "about marathon training")
        self._make(repo, factory, seed_user, "Beta", "about cooking")
        rows = repo.list_storylines(seed_user, search="marathon")
        assert [r.name for r in rows] == ["Alpha"]

    def test_search_is_case_insensitive(
        self,
        repo: SQLiteStorylineRepository,
        factory: ConnectionFactory,
        seed_user: int,
    ) -> None:
        self._make(repo, factory, seed_user, "Running")
        rows = repo.list_storylines(seed_user, search="rUnN")
        assert [r.name for r in rows] == ["Running"]

    def test_search_is_whitespace_trimmed(
        self,
        repo: SQLiteStorylineRepository,
        factory: ConnectionFactory,
        seed_user: int,
    ) -> None:
        self._make(repo, factory, seed_user, "Running")
        self._make(repo, factory, seed_user, "Cooking")
        rows = repo.list_storylines(seed_user, search="  Run  ")
        assert [r.name for r in rows] == ["Running"]

    def test_search_returns_only_matching_rows(
        self,
        repo: SQLiteStorylineRepository,
        factory: ConnectionFactory,
        seed_user: int,
    ) -> None:
        self._make(repo, factory, seed_user, "Running")
        self._make(repo, factory, seed_user, "Cycling")
        self._make(repo, factory, seed_user, "Cooking")
        rows = repo.list_storylines(seed_user, search="c")
        # "Cycling" and "Cooking" match; "Running" does not.
        assert {r.name for r in rows} == {"Cycling", "Cooking"}

    def test_empty_or_whitespace_search_is_ignored(
        self,
        repo: SQLiteStorylineRepository,
        factory: ConnectionFactory,
        seed_user: int,
    ) -> None:
        self._make(repo, factory, seed_user, "Running")
        self._make(repo, factory, seed_user, "Cooking")
        assert len(repo.list_storylines(seed_user, search="")) == 2
        assert len(repo.list_storylines(seed_user, search="   ")) == 2
        assert repo.count_storylines(seed_user, search="   ") == 2

    def test_count_reflects_all_matches_beyond_page(
        self,
        repo: SQLiteStorylineRepository,
        factory: ConnectionFactory,
        seed_user: int,
    ) -> None:
        # Seed 5 matching storylines, plus 2 non-matching.
        for i in range(5):
            self._make(repo, factory, seed_user, f"Marathon-{i}")
        self._make(repo, factory, seed_user, "Cooking")
        self._make(repo, factory, seed_user, "Cycling")
        page = repo.list_storylines(
            seed_user, search="marathon", limit=2, offset=0,
        )
        assert len(page) == 2  # limited page
        # count is the whole filtered total, not the page size.
        assert repo.count_storylines(seed_user, search="marathon") == 5
        assert repo.count_storylines(seed_user, search="marathon") > len(page)

    def test_search_respects_user_isolation(
        self,
        repo: SQLiteStorylineRepository,
        factory: ConnectionFactory,
        seed_user: int,
    ) -> None:
        self._make(repo, factory, seed_user, "Running")
        assert repo.list_storylines(seed_user + 999, search="Run") == []
        assert repo.count_storylines(seed_user + 999, search="Run") == 0

    def test_search_combines_with_status_filter(
        self,
        repo: SQLiteStorylineRepository,
        factory: ConnectionFactory,
        seed_user: int,
    ) -> None:
        active = self._make(repo, factory, seed_user, "Running active")
        archived = self._make(repo, factory, seed_user, "Running archived")
        repo.update_storyline_status(archived.id, "archived", seed_user)
        rows = repo.list_storylines(seed_user, status="active", search="Running")
        assert [r.id for r in rows] == [active.id]
        assert repo.count_storylines(
            seed_user, status="active", search="Running",
        ) == 1


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
        repo: SQLiteStorylineRepository,
        seed_user: int,
        three_entities: list[int],
    ) -> None:
        a, b, c = three_entities
        s = repo.create_storyline(
            user_id=seed_user, entity_ids=[a, b], name="AB",
        )
        result = repo.set_anchors(s.id, [b, c])
        assert result == sorted([b, c])
        assert repo.list_anchors(s.id) == sorted([b, c])
        # No leftover join rows for the dropped anchor.
        assert a not in repo.list_anchors(s.id)

    def test_set_anchors_rejects_empty(
        self,
        repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = repo.create_storyline(
            user_id=seed_user, entity_ids=[seed_entity], name="A",
        )
        with pytest.raises(ValueError):
            repo.set_anchors(s.id, [])

    def test_list_storylines_with_anchor_returns_all_relevant(
        self,
        repo: SQLiteStorylineRepository,
        seed_user: int,
        three_entities: list[int],
    ) -> None:
        a, b, c = three_entities
        s_ab = repo.create_storyline(
            user_id=seed_user, entity_ids=[a, b], name="AB",
        )
        s_bc = repo.create_storyline(
            user_id=seed_user, entity_ids=[b, c], name="BC",
        )
        s_a = repo.create_storyline(
            user_id=seed_user, entity_ids=[a], name="A-only",
        )
        # b is in both s_ab and s_bc, not in s_a.
        rows = repo.list_storylines_with_anchor(seed_user, b)
        assert {r.id for r in rows} == {s_ab.id, s_bc.id}
        # a is in s_ab and s_a, not s_bc.
        rows = repo.list_storylines_with_anchor(seed_user, a)
        assert {r.id for r in rows} == {s_ab.id, s_a.id}

    def test_list_storylines_with_anchor_filters_by_status(
        self,
        repo: SQLiteStorylineRepository,
        seed_user: int,
        three_entities: list[int],
    ) -> None:
        a, b, _ = three_entities
        s_active = repo.create_storyline(
            user_id=seed_user, entity_ids=[a, b], name="active",
        )
        s_arch = repo.create_storyline(
            user_id=seed_user, entity_ids=[a, b], name="archived",
        )
        repo.update_storyline_status(s_arch.id, "archived", seed_user)

        active_only = repo.list_storylines_with_anchor(
            seed_user, a, status="active",
        )
        assert [r.id for r in active_only] == [s_active.id]

    def test_delete_storyline_cascades_to_anchors(
        self,
        repo: SQLiteStorylineRepository,
        seed_user: int,
        three_entities: list[int],
        factory: ConnectionFactory,
    ) -> None:
        a, b, _ = three_entities
        s = repo.create_storyline(
            user_id=seed_user, entity_ids=[a, b], name="AB",
        )
        factory.get().execute("PRAGMA foreign_keys=ON")
        repo.delete_storyline(s.id, user_id=seed_user)
        row = factory.get().execute(
            "SELECT COUNT(*) AS cnt FROM storyline_entities"
            " WHERE storyline_id = ?",
            (s.id,),
        ).fetchone()
        assert int(row["cnt"]) == 0


# ── Chapter lifecycle ────────────────────────────────────────────


class TestChapterLifecycle:
    def test_create_storyline_seeds_one_draft(
        self,
        repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        s = repo.create_storyline(seed_user, [seed_entity], "Running")
        chapters = repo.list_chapters(s.id)
        assert len(chapters) == 1
        assert chapters[0].state == "draft" and chapters[0].seq == 1
        assert repo.get_draft(s.id).id == chapters[0].id

    def test_publish_draft_is_atomic_and_seeds_new_draft(
        self,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
    ) -> None:
        draft = repo.get_draft(storyline.id)
        repo.add_entries_to_draft(draft.id, entry_ids[:2])
        published, new_draft = repo.publish_draft(
            storyline.id, title="The Start",
            segments=[{"kind": "text", "text": "prose"}],
            source_entry_ids=entry_ids[:2], citation_count=2, model_used="m",
            new_draft_entry_ids=[entry_ids[2]],
        )
        assert published.state == "published" and published.title == "The Start"
        assert published.published_at is not None and published.read_at is None
        assert new_draft.state == "draft" and new_draft.seq == published.seq + 1
        assert repo.chapter_entry_ids(new_draft.id) == [entry_ids[2]]

    def test_add_entries_to_draft_rejects_id_already_in_another_chapter(
        self,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
    ) -> None:
        """Membership uniqueness guard (spec §1): an entry belongs to
        exactly one chapter of a storyline at a time. Assign an entry
        to the draft, publish it (so the entry now lives in the
        published chapter), then try to add the SAME entry to the
        fresh draft that publish seeded — must raise, naming the entry
        and its current chapter."""
        draft = repo.get_draft(storyline.id)
        repo.add_entries_to_draft(draft.id, entry_ids[:1])
        published, new_draft = repo.publish_draft(
            storyline.id, title="Chapter One",
            segments=[{"kind": "text", "text": "prose"}],
            source_entry_ids=entry_ids[:1], citation_count=1, model_used="m",
            new_draft_entry_ids=[],
        )

        with pytest.raises(ValueError, match=str(entry_ids[0])):
            repo.add_entries_to_draft(new_draft.id, entry_ids[:1])

        # No partial write: the entry is still only in the published chapter.
        assert repo.chapter_entry_ids(new_draft.id) == []
        assert repo.chapter_entry_ids(published.id) == entry_ids[:1]

    def test_publish_draft_rejects_new_draft_id_already_in_another_chapter(
        self,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
    ) -> None:
        """Same guard, exercised via ``publish_draft``'s
        ``new_draft_entry_ids`` path: an id already sitting in the
        chapter about to be published can't ALSO be routed into the
        fresh draft being seeded by the same call."""
        draft = repo.get_draft(storyline.id)
        repo.add_entries_to_draft(draft.id, entry_ids[:1])

        with pytest.raises(ValueError, match=str(entry_ids[0])):
            repo.publish_draft(
                storyline.id, title="Chapter One",
                segments=[{"kind": "text", "text": "prose"}],
                source_entry_ids=entry_ids[:1], citation_count=1, model_used="m",
                new_draft_entry_ids=entry_ids[:1],
            )

    def test_publish_without_draft_raises(
        self, repo: SQLiteStorylineRepository, seed_user: int,
    ) -> None:
        with pytest.raises(ValueError, match="no draft"):
            repo.publish_draft(9999, title="x", segments=[], source_entry_ids=[],
                               citation_count=0, model_used="m", new_draft_entry_ids=[])

    def test_unpublish_newest_folds_members_into_draft(
        self,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
    ) -> None:
        draft = repo.get_draft(storyline.id)
        repo.add_entries_to_draft(draft.id, entry_ids[:2])
        published, new_draft = repo.publish_draft(
            storyline.id, title="t", segments=[], source_entry_ids=[],
            citation_count=0, model_used="m", new_draft_entry_ids=[entry_ids[2]],
        )
        merged = repo.unpublish_newest(storyline.id)
        assert merged.state == "draft"
        assert set(repo.chapter_entry_ids(merged.id)) == set(entry_ids)
        assert len(repo.list_chapters(storyline.id)) == 1
        # Verify stale narrative fields are cleared
        assert merged.model_used == ""
        assert merged.draft_embedding is None

    def test_unpublish_with_no_published_raises(
        self, repo: SQLiteStorylineRepository, storyline: Storyline,
    ) -> None:
        with pytest.raises(ValueError, match="no published chapter"):
            repo.unpublish_newest(storyline.id)

    def test_rename_chapter_returns_updated(
        self,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
    ) -> None:
        draft = repo.get_draft(storyline.id)
        renamed = repo.rename_chapter(draft.id, "New Title")
        assert renamed is not None
        assert renamed.title == "New Title"
        # Verify persisted
        refreshed = repo.get_chapter(draft.id)
        assert refreshed is not None
        assert refreshed.title == "New Title"

    def test_rename_chapter_unknown_id_returns_none(
        self,
        repo: SQLiteStorylineRepository,
    ) -> None:
        result = repo.rename_chapter(9999, "Title")
        assert result is None

    def test_get_chapter_unknown_id_returns_none(
        self,
        repo: SQLiteStorylineRepository,
    ) -> None:
        result = repo.get_chapter(9999)
        assert result is None

    def test_assigned_entry_ids(
        self,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        seed_user: int,
        seed_entity: int,
        entry_ids: list[int],
    ) -> None:
        # Create a second storyline to verify isolation
        other_storyline = repo.create_storyline(
            seed_user, [seed_entity], "Other Story",
        )
        # Add 2 entries to the first storyline's draft
        draft = repo.get_draft(storyline.id)
        repo.add_entries_to_draft(draft.id, entry_ids[:2])
        # Publish with a third entry as new_draft_entry_ids
        published, new_draft = repo.publish_draft(
            storyline.id, title="Published",
            segments=[{"kind": "text", "text": "content"}],
            source_entry_ids=entry_ids[:2], citation_count=2, model_used="m",
            new_draft_entry_ids=[entry_ids[2]],
        )
        # All three entries should be assigned to the storyline
        assigned = repo.assigned_entry_ids(storyline.id)
        assert assigned == set(entry_ids)
        # Other storyline should have none
        assert repo.assigned_entry_ids(other_storyline.id) == set()


# ── Immutability guards ──────────────────────────────────────────


class TestImmutability:
    def test_set_draft_narrative_refuses_published(
        self,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
    ) -> None:
        draft = repo.get_draft(storyline.id)
        repo.add_entries_to_draft(draft.id, entry_ids[:2])
        published, _ = repo.publish_draft(
            storyline.id, title="t", segments=[], source_entry_ids=[],
            citation_count=0, model_used="m", new_draft_entry_ids=[],
        )
        with pytest.raises(ValueError, match="published"):
            repo.set_draft_narrative(published.id, segments=[], source_entry_ids=[],
                                     citation_count=0, model_used="m", embedding=None)

    def test_add_entries_refuses_published(
        self,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
    ) -> None:
        draft = repo.get_draft(storyline.id)
        repo.add_entries_to_draft(draft.id, entry_ids[:2])
        published, _ = repo.publish_draft(
            storyline.id, title="t", segments=[], source_entry_ids=[],
            citation_count=0, model_used="m", new_draft_entry_ids=[],
        )
        with pytest.raises(ValueError, match="published"):
            repo.add_entries_to_draft(published.id, [entry_ids[2]])

    def test_addendum_refuses_draft(
        self, repo: SQLiteStorylineRepository, storyline: Storyline,
    ) -> None:
        draft = repo.get_draft(storyline.id)
        with pytest.raises(ValueError, match="draft"):
            repo.append_addendum(draft.id, segments=[], entry_ids=[])

    def test_addendum_clears_read_and_marks_late(
        self,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
        factory: ConnectionFactory,
    ) -> None:
        draft = repo.get_draft(storyline.id)
        repo.add_entries_to_draft(draft.id, entry_ids[:2])
        published, _ = repo.publish_draft(
            storyline.id, title="t", segments=[], source_entry_ids=[],
            citation_count=0, model_used="m", new_draft_entry_ids=[],
        )
        repo.set_read(published.id, True)
        assert repo.get_chapter(published.id).read_at is not None

        repo.append_addendum(
            published.id,
            segments=[{"kind": "text", "text": "update"}],
            entry_ids=[entry_ids[2]],
        )
        refreshed = repo.get_chapter(published.id)
        assert refreshed is not None
        assert refreshed.read_at is None
        assert len(refreshed.addenda) == 1
        row = factory.get().execute(
            "SELECT added_late FROM storyline_chapter_entries"
            " WHERE chapter_id = ? AND entry_id = ?",
            (published.id, entry_ids[2]),
        ).fetchone()
        assert row is not None
        assert int(row["added_late"]) == 1

    def test_set_read_refuses_draft(
        self, repo: SQLiteStorylineRepository, storyline: Storyline,
    ) -> None:
        draft = repo.get_draft(storyline.id)
        with pytest.raises(ValueError, match="draft"):
            repo.set_read(draft.id, True)


# ── Derived fields + unread ──────────────────────────────────────


class TestDerivedFieldsAndUnread:
    def test_list_chapters_derives_dates_and_counts(
        self,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
    ) -> None:
        draft = repo.get_draft(storyline.id)
        repo.add_entries_to_draft(draft.id, entry_ids)  # dates 2026-02-20 .. 2026-04-25
        ch = repo.list_chapters(storyline.id)[0]
        assert (ch.entry_count, ch.first_entry_date, ch.last_entry_date) == (
            3, "2026-02-20", "2026-04-25")

    def test_unread_counts(
        self,
        repo: SQLiteStorylineRepository,
        seed_user: int,
        storyline: Storyline,
        entry_ids: list[int],
    ) -> None:
        draft = repo.get_draft(storyline.id)
        repo.add_entries_to_draft(draft.id, entry_ids[:1])
        first_published, _ = repo.publish_draft(
            storyline.id, title="one", segments=[], source_entry_ids=[],
            citation_count=0, model_used="m", new_draft_entry_ids=[entry_ids[1]],
        )
        draft2 = repo.get_draft(storyline.id)
        second_published, _ = repo.publish_draft(
            storyline.id, title="two", segments=[], source_entry_ids=[],
            citation_count=0, model_used="m", new_draft_entry_ids=[entry_ids[2]],
        )
        assert draft2.id == second_published.id
        repo.set_read(first_published.id, True)
        assert repo.unread_counts(seed_user) == {storyline.id: 1}

    def test_chapter_counts(
        self,
        repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
        storyline: Storyline,
        entry_ids: list[int],
    ) -> None:
        # First storyline: publish one, keeping one draft = 2 chapters
        draft1 = repo.get_draft(storyline.id)
        repo.add_entries_to_draft(draft1.id, entry_ids[:1])
        published1, new_draft1 = repo.publish_draft(
            storyline.id, title="one", segments=[], source_entry_ids=[],
            citation_count=0, model_used="m", new_draft_entry_ids=[entry_ids[1]],
        )
        assert published1.seq == 1 and new_draft1.seq == 2
        # Second storyline: one draft only = 1 chapter
        storyline2 = repo.create_storyline(
            user_id=seed_user, entity_ids=[seed_entity], name="Story 2",
        )
        counts = repo.chapter_counts(seed_user)
        assert counts == {storyline.id: 2, storyline2.id: 1}

    def test_chapter_counts_empty_for_other_user(
        self,
        repo: SQLiteStorylineRepository,
        seed_user: int,
        storyline: Storyline,
    ) -> None:
        counts = repo.chapter_counts(seed_user + 999)
        assert counts == {}


# ── Pending entries ──────────────────────────────────────────────


class TestPendingEntries:
    def test_pending_roundtrip(
        self,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
    ) -> None:
        repo.add_pending_entry(storyline.id, entry_ids[0])
        repo.add_pending_entry(storyline.id, entry_ids[0])  # idempotent
        assert repo.list_pending_entries(storyline.id) == [entry_ids[0]]
        repo.clear_pending_entries(storyline.id, [entry_ids[0]])
        assert repo.list_pending_entries(storyline.id) == []


# ── Bootstrap replace-all ─────────────────────────────────────────


class TestBootstrapReplace:
    def test_replace_all_chapters(
        self,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
    ) -> None:
        specs = [
            BootstrapChapterSpec(title="One", state="published", segments=[],
                                 source_entry_ids=[], citation_count=0,
                                 model_used="m", entry_ids=entry_ids[:2],
                                 mark_read=True),
            BootstrapChapterSpec(title="", state="draft", segments=[],
                                 source_entry_ids=[], citation_count=0,
                                 model_used="m", entry_ids=[entry_ids[2]]),
        ]
        chapters = repo.replace_all_chapters(storyline.id, specs)
        assert [c.state for c in chapters] == ["published", "draft"]
        assert chapters[0].read_at is not None
        assert repo.chapter_entry_ids(chapters[1].id) == [entry_ids[2]]

    def test_replace_rejects_non_final_draft(
        self, repo: SQLiteStorylineRepository, storyline: Storyline,
    ) -> None:
        specs = [BootstrapChapterSpec(title="", state="draft", segments=[],
                                      source_entry_ids=[], citation_count=0,
                                      model_used="m", entry_ids=[]),
                 BootstrapChapterSpec(title="x", state="published", segments=[],
                                      source_entry_ids=[], citation_count=0,
                                      model_used="m", entry_ids=[])]
        with pytest.raises(ValueError, match="draft must be the final"):
            repo.replace_all_chapters(storyline.id, specs)


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


class TestFindStorylineIdsForEntry:
    """Reverse entry → storyline lookup (spec 2026-07-13, component 4)."""

    def test_returns_distinct_storylines_ascending(
        self,
        repo: SQLiteStorylineRepository,
        seed_user: int,
        seed_entity: int,
        entry_ids: list[int],
    ) -> None:
        s1 = repo.create_storyline(seed_user, [seed_entity], "Running")
        s2 = repo.create_storyline(seed_user, [seed_entity], "Body")
        d1 = repo.get_draft(s1.id)
        d2 = repo.get_draft(s2.id)
        assert d1 is not None and d2 is not None
        repo.add_entries_to_draft(d1.id, [entry_ids[0], entry_ids[1]])
        repo.add_entries_to_draft(d2.id, [entry_ids[0]])

        assert repo.find_storyline_ids_for_entry(entry_ids[0]) == sorted(
            [s1.id, s2.id]
        )
        assert repo.find_storyline_ids_for_entry(entry_ids[1]) == [s1.id]
        assert repo.find_storyline_ids_for_entry(entry_ids[2]) == []


class TestUnconfirmedEntriesExcludedFromCandidacy:
    """Quarantined entries never reach the storyline corpus
    (spec 2026-07-13, component 3 defense-in-depth)."""

    def _seed_entry(
        self,
        factory: ConnectionFactory,
        user_id: int,
        *,
        entry_date: str,
        text: str,
        date_confirmed: int,
    ) -> int:
        conn = factory.get()
        cur = conn.execute(
            "INSERT INTO entries"
            " (entry_date, source_type, raw_text, final_text,"
            "  word_count, user_id, date_confirmed)"
            " VALUES (?, 'text', ?, ?, ?, ?, ?)",
            (entry_date, text, text, len(text.split()), user_id, date_confirmed),
        )
        conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def test_find_entries_mentioning_excludes_unconfirmed(
        self,
        factory: ConnectionFactory,
        repo: SQLiteStorylineRepository,
        seed_user: int,
    ) -> None:
        confirmed_id = self._seed_entry(
            factory, seed_user,
            entry_date="2026-07-01", text="Atlas played football",
            date_confirmed=1,
        )
        self._seed_entry(
            factory, seed_user,
            entry_date="2019-07-01", text="Atlas at the beach",
            date_confirmed=0,
        )
        hits = repo.find_entries_mentioning(seed_user, "Atlas")
        assert [h.entry_id for h in hits] == [confirmed_id]

    def test_dated_entity_excerpts_exclude_unconfirmed(
        self,
        factory: ConnectionFactory,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        confirmed_id = self._seed_entry(
            factory, seed_user,
            entry_date="2026-07-01", text="I ran 5km today",
            date_confirmed=1,
        )
        held_id = self._seed_entry(
            factory, seed_user,
            entry_date="2019-07-01", text="I ran 8km once",
            date_confirmed=0,
        )
        conn = factory.get()
        for eid, quote in ((confirmed_id, "ran 5km"), (held_id, "ran 8km")):
            conn.execute(
                "INSERT INTO entity_mentions"
                " (entity_id, entry_id, quote, confidence, extraction_run_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (seed_entity, eid, quote, 0.95, "run-1"),
            )
        conn.commit()

        store = SQLiteEntityStore(factory)
        excerpts = store.get_dated_entity_excerpts(
            entity_id=seed_entity, user_id=seed_user,
        )
        assert [e.entry_id for e in excerpts] == [confirmed_id]
