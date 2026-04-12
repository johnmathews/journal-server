"""Tests for SQLiteEntityStore."""

import sqlite3

import pytest

from journal.db.repository import SQLiteEntryRepository
from journal.entitystore.store import SQLiteEntityStore


@pytest.fixture
def store(db_conn: sqlite3.Connection) -> SQLiteEntityStore:
    return SQLiteEntityStore(db_conn)


@pytest.fixture
def repo(db_conn: sqlite3.Connection) -> SQLiteEntryRepository:
    return SQLiteEntryRepository(db_conn)


@pytest.fixture
def sample_entry_id(repo: SQLiteEntryRepository) -> int:
    entry = repo.create_entry(
        "2026-03-22", "ocr", "Atlas and I went to Vienna.", 6,
    )
    return entry.id


class TestEntityCreation:
    def test_create_entity_round_trip(
        self, store: SQLiteEntityStore
    ) -> None:
        entity = store.create_entity(
            "person", "Atlas", "a dog", "2026-03-22"
        )
        assert entity.id > 0
        assert entity.canonical_name == "Atlas"
        assert entity.entity_type == "person"
        assert entity.description == "a dog"
        assert entity.first_seen == "2026-03-22"

        fetched = store.get_entity_by_name("Atlas", "person")
        assert fetched is not None
        assert fetched.id == entity.id

    def test_get_entity_by_name_case_insensitive(
        self, store: SQLiteEntityStore
    ) -> None:
        entity = store.create_entity("person", "Atlas", "", "2026-01-01")
        assert store.get_entity_by_name("atlas", "person") is not None
        assert store.get_entity_by_name("ATLAS", "person") is not None
        assert store.get_entity_by_name("Atlas", "person") is not None
        assert (
            store.get_entity_by_name("atlas", "person").id == entity.id  # type: ignore[union-attr]
        )

    def test_unique_by_type_and_name(
        self, store: SQLiteEntityStore
    ) -> None:
        store.create_entity("person", "Atlas", "", "2026-01-01")
        with pytest.raises(sqlite3.IntegrityError):
            store.create_entity("person", "Atlas", "", "2026-01-02")

    def test_same_name_different_types_allowed(
        self, store: SQLiteEntityStore
    ) -> None:
        p = store.create_entity("person", "Atlas", "", "2026-01-01")
        t = store.create_entity("topic", "Atlas", "", "2026-01-01")
        assert p.id != t.id


class TestAliases:
    def test_add_alias_and_find(
        self, store: SQLiteEntityStore
    ) -> None:
        entity = store.create_entity(
            "person", "Atlas Wong", "", "2026-01-01"
        )
        store.add_alias(entity.id, "Atlas")
        store.add_alias(entity.id, "A. Wong")

        found = store.find_by_alias("atlas", "person")
        assert found is not None
        assert found.id == entity.id
        assert "atlas" in found.aliases

    def test_add_alias_is_idempotent(
        self, store: SQLiteEntityStore
    ) -> None:
        entity = store.create_entity("person", "Atlas", "", "2026-01-01")
        store.add_alias(entity.id, "atlas")
        store.add_alias(entity.id, "Atlas")  # normalises to same
        refetched = store.get_entity(entity.id)
        assert refetched is not None
        assert refetched.aliases.count("atlas") == 1

    def test_find_by_alias_respects_type(
        self, store: SQLiteEntityStore
    ) -> None:
        person = store.create_entity("person", "Atlas Wong", "", "2026-01-01")
        topic = store.create_entity("topic", "Atlas", "", "2026-01-01")
        store.add_alias(person.id, "Atlas")

        assert (
            store.find_by_alias("Atlas", "person").id == person.id  # type: ignore[union-attr]
        )
        # Topic lookup should NOT return the person even though the
        # alias text matches.
        topic_hit = store.find_by_alias("Atlas", "topic")
        assert topic_hit is None or topic_hit.id == topic.id


class TestEmbeddings:
    def test_embedding_initially_none(
        self, store: SQLiteEntityStore
    ) -> None:
        entity = store.create_entity("person", "Atlas", "", "2026-01-01")
        assert store.get_entity_embedding(entity.id) is None

    def test_set_get_embedding_round_trip(
        self, store: SQLiteEntityStore
    ) -> None:
        entity = store.create_entity("person", "Atlas", "", "2026-01-01")
        vec = [0.1, 0.2, -0.3, 0.4]
        store.set_entity_embedding(entity.id, vec)
        fetched = store.get_entity_embedding(entity.id)
        assert fetched is not None
        assert len(fetched) == 4
        for x, y in zip(fetched, vec, strict=True):
            assert abs(x - y) < 1e-9

    def test_list_entities_of_type_with_embeddings(
        self, store: SQLiteEntityStore
    ) -> None:
        atlas = store.create_entity("person", "Atlas", "", "2026-01-01")
        store.create_entity("person", "Without embedding", "", "2026-01-01")
        vienna = store.create_entity("place", "Vienna", "", "2026-01-01")
        store.set_entity_embedding(atlas.id, [0.1, 0.2, 0.3])
        store.set_entity_embedding(vienna.id, [0.9, 0.1, 0.0])

        persons = store.list_entities_of_type_with_embeddings("person")
        assert len(persons) == 1
        assert persons[0][0].id == atlas.id
        assert persons[0][1] == [0.1, 0.2, 0.3]


class TestListing:
    def test_list_entities_all_types(
        self, store: SQLiteEntityStore
    ) -> None:
        store.create_entity("person", "Atlas", "", "2026-01-01")
        store.create_entity("place", "Vienna", "", "2026-01-01")
        store.create_entity("topic", "Work", "", "2026-01-01")

        all_entities = store.list_entities(limit=50)
        assert len(all_entities) == 3
        # Ordered by entity_type ASC then canonical_name ASC
        assert all_entities[0].entity_type == "person"
        assert all_entities[1].entity_type == "place"
        assert all_entities[2].entity_type == "topic"

    def test_list_entities_filter_by_type(
        self, store: SQLiteEntityStore
    ) -> None:
        store.create_entity("person", "Atlas", "", "2026-01-01")
        store.create_entity("place", "Vienna", "", "2026-01-01")
        persons = store.list_entities(entity_type="person")
        assert len(persons) == 1
        assert persons[0].canonical_name == "Atlas"

    def test_list_entities_pagination(
        self, store: SQLiteEntityStore
    ) -> None:
        for i in range(5):
            store.create_entity("person", f"Person{i}", "", "2026-01-01")
        first = store.list_entities(limit=2, offset=0)
        second = store.list_entities(limit=2, offset=2)
        assert len(first) == 2
        assert len(second) == 2
        assert first[0].id != second[0].id

    def test_list_entities_with_mention_counts(
        self,
        store: SQLiteEntityStore,
        sample_entry_id: int,
    ) -> None:
        atlas = store.create_entity("person", "Atlas", "", "2026-03-22")
        vienna = store.create_entity("place", "Vienna", "", "2026-03-22")
        store.create_mention(atlas.id, sample_entry_id, "Atlas", 0.9, "run1")
        store.create_mention(atlas.id, sample_entry_id, "Atlas", 0.8, "run1")
        store.create_mention(vienna.id, sample_entry_id, "Vienna", 0.9, "run1")

        rows = store.list_entities_with_mention_counts()
        counts = {e.canonical_name: c for e, c, _ls in rows}
        assert counts["Atlas"] == 2
        assert counts["Vienna"] == 1

    def test_count_entities(
        self, store: SQLiteEntityStore
    ) -> None:
        assert store.count_entities() == 0
        store.create_entity("person", "Atlas", "", "2026-01-01")
        store.create_entity("place", "Vienna", "", "2026-01-01")
        assert store.count_entities() == 2
        assert store.count_entities("person") == 1


class TestMentions:
    def test_create_and_fetch_mentions(
        self,
        store: SQLiteEntityStore,
        sample_entry_id: int,
    ) -> None:
        atlas = store.create_entity("person", "Atlas", "", "2026-03-22")
        m = store.create_mention(
            atlas.id, sample_entry_id, "Atlas and I", 0.95, "run-1"
        )
        assert m.entity_id == atlas.id
        assert m.entry_id == sample_entry_id

        by_entity = store.get_mentions_for_entity(atlas.id)
        assert len(by_entity) == 1
        by_entry = store.get_mentions_for_entry(sample_entry_id)
        assert len(by_entry) == 1

    def test_delete_mentions_for_entry_only_removes_that_entry(
        self,
        store: SQLiteEntityStore,
        repo: SQLiteEntryRepository,
    ) -> None:
        e1 = repo.create_entry("2026-03-22", "ocr", "first", 1).id
        e2 = repo.create_entry("2026-03-23", "ocr", "second", 1).id
        atlas = store.create_entity("person", "Atlas", "", "2026-03-22")
        store.create_mention(atlas.id, e1, "Atlas", 0.9, "run1")
        store.create_mention(atlas.id, e2, "Atlas", 0.9, "run1")

        deleted = store.delete_mentions_for_entry(e1)
        assert deleted == 1
        remaining = store.get_mentions_for_entity(atlas.id)
        assert len(remaining) == 1
        assert remaining[0].entry_id == e2


class TestRelationships:
    def test_create_relationship_and_fetch_outgoing_incoming(
        self,
        store: SQLiteEntityStore,
        sample_entry_id: int,
    ) -> None:
        atlas = store.create_entity("person", "Atlas", "", "2026-03-22")
        vienna = store.create_entity("place", "Vienna", "", "2026-03-22")
        store.create_relationship(
            subject_id=atlas.id,
            predicate="visited",
            object_id=vienna.id,
            quote="Atlas visited Vienna",
            entry_id=sample_entry_id,
            confidence=0.9,
            extraction_run_id="run-1",
        )

        atlas_out, atlas_in = store.get_relationships_for_entity(atlas.id)
        vienna_out, vienna_in = store.get_relationships_for_entity(vienna.id)
        assert len(atlas_out) == 1
        assert atlas_out[0].predicate == "visited"
        assert atlas_in == []
        assert vienna_out == []
        assert len(vienna_in) == 1

    def test_get_relationships_for_entry(
        self,
        store: SQLiteEntityStore,
        sample_entry_id: int,
    ) -> None:
        a = store.create_entity("person", "Atlas", "", "2026-03-22")
        v = store.create_entity("place", "Vienna", "", "2026-03-22")
        store.create_relationship(
            subject_id=a.id,
            predicate="visited",
            object_id=v.id,
            quote="",
            entry_id=sample_entry_id,
            confidence=0.9,
            extraction_run_id="r1",
        )
        rels = store.get_relationships_for_entry(sample_entry_id)
        assert len(rels) == 1


class TestStaleFlag:
    def test_mark_entry_extracted_clears_flag(
        self,
        store: SQLiteEntityStore,
        repo: SQLiteEntryRepository,
        db_conn: sqlite3.Connection,
    ) -> None:
        entry = repo.create_entry("2026-03-22", "ocr", "text", 1)
        row = db_conn.execute(
            "SELECT entity_extraction_stale FROM entries WHERE id = ?",
            (entry.id,),
        ).fetchone()
        assert row["entity_extraction_stale"] == 1

        store.mark_entry_extracted(entry.id)
        row = db_conn.execute(
            "SELECT entity_extraction_stale FROM entries WHERE id = ?",
            (entry.id,),
        ).fetchone()
        assert row["entity_extraction_stale"] == 0

    def test_trigger_reflags_entry_on_final_text_update(
        self,
        store: SQLiteEntityStore,
        repo: SQLiteEntryRepository,
        db_conn: sqlite3.Connection,
    ) -> None:
        entry = repo.create_entry("2026-03-22", "ocr", "first", 1)
        store.mark_entry_extracted(entry.id)

        repo.update_final_text(entry.id, "corrected text", 2, 0)
        row = db_conn.execute(
            "SELECT entity_extraction_stale FROM entries WHERE id = ?",
            (entry.id,),
        ).fetchone()
        assert row["entity_extraction_stale"] == 1


class TestGetEntitiesForEntry:
    def test_returns_distinct_entities_for_entry(
        self,
        store: SQLiteEntityStore,
        sample_entry_id: int,
    ) -> None:
        a = store.create_entity("person", "Atlas", "", "2026-03-22")
        v = store.create_entity("place", "Vienna", "", "2026-03-22")
        store.create_mention(a.id, sample_entry_id, "Atlas", 0.9, "r1")
        store.create_mention(a.id, sample_entry_id, "Atlas again", 0.8, "r1")
        store.create_mention(v.id, sample_entry_id, "Vienna", 0.9, "r1")
        entities = store.get_entities_for_entry(sample_entry_id)
        ids = {e.id for e in entities}
        assert ids == {a.id, v.id}
