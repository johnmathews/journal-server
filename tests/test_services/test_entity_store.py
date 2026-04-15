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
        "2026-03-22", "photo", "Atlas and I went to Vienna.", 6,
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
        e1 = repo.create_entry("2026-03-22", "photo", "first", 1).id
        e2 = repo.create_entry("2026-03-23", "photo", "second", 1).id
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
        entry = repo.create_entry("2026-03-22", "photo", "text", 1)
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
        entry = repo.create_entry("2026-03-22", "photo", "first", 1)
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


class TestUpdateEntity:
    def test_update_canonical_name(self, store: SQLiteEntityStore) -> None:
        entity = store.create_entity("person", "Lizzie", "", "2026-01-01")
        updated = store.update_entity(entity.id, canonical_name="Lizzie Extance")
        assert updated.canonical_name == "Lizzie Extance"
        assert updated.entity_type == "person"
        fetched = store.get_entity(entity.id)
        assert fetched is not None
        assert fetched.canonical_name == "Lizzie Extance"

    def test_update_entity_type(self, store: SQLiteEntityStore) -> None:
        entity = store.create_entity("other", "Monday", "", "2026-01-01")
        updated = store.update_entity(entity.id, entity_type="activity")
        assert updated.entity_type == "activity"

    def test_update_description(self, store: SQLiteEntityStore) -> None:
        entity = store.create_entity("person", "Atlas", "", "2026-01-01")
        updated = store.update_entity(entity.id, description="a beloved dog")
        assert updated.description == "a beloved dog"

    def test_update_multiple_fields(self, store: SQLiteEntityStore) -> None:
        entity = store.create_entity("other", "Gym", "", "2026-01-01")
        updated = store.update_entity(
            entity.id, canonical_name="Gym session", entity_type="activity"
        )
        assert updated.canonical_name == "Gym session"
        assert updated.entity_type == "activity"

    def test_update_no_fields_returns_unchanged(
        self, store: SQLiteEntityStore
    ) -> None:
        entity = store.create_entity("person", "Atlas", "", "2026-01-01")
        result = store.update_entity(entity.id)
        assert result.canonical_name == "Atlas"

    def test_update_nonexistent_raises(self, store: SQLiteEntityStore) -> None:
        with pytest.raises(ValueError, match="not found"):
            store.update_entity(9999, canonical_name="Ghost")

    def test_update_sets_updated_at(self, store: SQLiteEntityStore) -> None:
        entity = store.create_entity("person", "Atlas", "", "2026-01-01")
        original_updated_at = entity.updated_at
        updated = store.update_entity(entity.id, description="new desc")
        assert updated.updated_at >= original_updated_at


class TestDeleteEntity:
    def test_delete_entity(self, store: SQLiteEntityStore) -> None:
        entity = store.create_entity("person", "Noise", "", "2026-01-01")
        store.delete_entity(entity.id)
        assert store.get_entity(entity.id) is None
        assert store.count_entities() == 0

    def test_delete_cascades_mentions(
        self, store: SQLiteEntityStore, sample_entry_id: int
    ) -> None:
        entity = store.create_entity("person", "Noise", "", "2026-01-01")
        store.create_mention(entity.id, sample_entry_id, "noise", 0.5, "r1")
        store.delete_entity(entity.id)
        assert store.get_mentions_for_entry(sample_entry_id) == []

    def test_delete_cascades_relationships(
        self, store: SQLiteEntityStore, sample_entry_id: int
    ) -> None:
        a = store.create_entity("person", "A", "", "2026-01-01")
        b = store.create_entity("person", "B", "", "2026-01-01")
        store.create_relationship(a.id, "knows", b.id, "q", sample_entry_id, 0.9, "r1")
        store.delete_entity(a.id)
        assert store.get_relationships_for_entry(sample_entry_id) == []

    def test_delete_cascades_aliases(
        self, store: SQLiteEntityStore, db_conn: sqlite3.Connection
    ) -> None:
        entity = store.create_entity("person", "Liz", "", "2026-01-01")
        store.add_alias(entity.id, "lizzie")
        store.delete_entity(entity.id)
        row = db_conn.execute(
            "SELECT COUNT(*) AS cnt FROM entity_aliases WHERE entity_id = ?",
            (entity.id,),
        ).fetchone()
        assert row["cnt"] == 0

    def test_delete_nonexistent_raises(self, store: SQLiteEntityStore) -> None:
        with pytest.raises(ValueError, match="not found"):
            store.delete_entity(9999)


class TestMergeEntities:
    def test_basic_merge(
        self, store: SQLiteEntityStore, sample_entry_id: int
    ) -> None:
        a = store.create_entity("person", "Vienna's aunt", "", "2026-01-01")
        b = store.create_entity("person", "Lizzie Extance", "", "2026-01-01")
        store.create_mention(a.id, sample_entry_id, "Vienna's aunt", 0.9, "r1")
        store.create_mention(b.id, sample_entry_id, "Lizzie", 0.9, "r1")

        result = store.merge_entities(b.id, [a.id])
        assert result.survivor_id == b.id
        assert result.absorbed_ids == [a.id]
        assert result.mentions_reassigned == 1

        # Absorbed entity is gone
        assert store.get_entity(a.id) is None
        # Survivor has both mentions
        mentions = store.get_mentions_for_entity(b.id)
        assert len(mentions) == 2
        # Absorbed name became an alias
        survivor = store.get_entity(b.id)
        assert survivor is not None
        assert "vienna's aunt" in survivor.aliases

    def test_merge_reassigns_relationships(
        self, store: SQLiteEntityStore, sample_entry_id: int
    ) -> None:
        a = store.create_entity("person", "A", "", "2026-01-01")
        b = store.create_entity("person", "B", "", "2026-01-01")
        c = store.create_entity("place", "Park", "", "2026-01-01")
        # A -> Park, Park -> A
        store.create_relationship(a.id, "visited", c.id, "q", sample_entry_id, 0.9, "r1")
        store.create_relationship(c.id, "near", a.id, "q", sample_entry_id, 0.8, "r1")

        result = store.merge_entities(b.id, [a.id])
        assert result.relationships_reassigned == 2

        out, inc = store.get_relationships_for_entity(b.id)
        assert len(out) == 1
        assert out[0].predicate == "visited"
        assert len(inc) == 1
        assert inc[0].predicate == "near"

    def test_merge_multiple_absorbed(
        self, store: SQLiteEntityStore, sample_entry_id: int
    ) -> None:
        survivor = store.create_entity("person", "Lizzie Extance", "", "2026-01-01")
        a = store.create_entity("person", "Vienna's aunt", "", "2026-01-01")
        b = store.create_entity("person", "My sister", "", "2026-01-01")
        store.create_mention(a.id, sample_entry_id, "aunt", 0.9, "r1")
        store.create_mention(b.id, sample_entry_id, "sister", 0.9, "r1")

        result = store.merge_entities(survivor.id, [a.id, b.id])
        assert result.absorbed_ids == [a.id, b.id]
        assert result.mentions_reassigned == 2
        assert store.get_entity(a.id) is None
        assert store.get_entity(b.id) is None
        assert len(store.get_mentions_for_entity(survivor.id)) == 2

    def test_merge_preserves_merge_history(
        self, store: SQLiteEntityStore, sample_entry_id: int
    ) -> None:
        a = store.create_entity("person", "Old Name", "old desc", "2026-01-01")
        store.add_alias(a.id, "alias1")
        b = store.create_entity("person", "New Name", "", "2026-01-01")
        store.merge_entities(b.id, [a.id])

        history = store.get_merge_history(b.id)
        assert len(history) == 1
        assert history[0]["absorbed_id"] == a.id
        assert history[0]["absorbed_name"] == "Old Name"
        assert history[0]["absorbed_type"] == "person"
        assert history[0]["absorbed_desc"] == "old desc"
        assert "alias1" in history[0]["absorbed_aliases"]

    def test_merge_into_self_raises(self, store: SQLiteEntityStore) -> None:
        a = store.create_entity("person", "A", "", "2026-01-01")
        with pytest.raises(ValueError, match="Cannot merge entity into itself"):
            store.merge_entities(a.id, [a.id])

    def test_merge_nonexistent_survivor_raises(
        self, store: SQLiteEntityStore
    ) -> None:
        with pytest.raises(ValueError, match="Survivor entity"):
            store.merge_entities(9999, [1])

    def test_merge_nonexistent_absorbed_raises(
        self, store: SQLiteEntityStore
    ) -> None:
        a = store.create_entity("person", "A", "", "2026-01-01")
        with pytest.raises(ValueError, match="Absorbed entity"):
            store.merge_entities(a.id, [9999])


class TestMergeCandidates:
    def test_create_and_list_candidates(
        self, store: SQLiteEntityStore
    ) -> None:
        a = store.create_entity("person", "A", "", "2026-01-01")
        b = store.create_entity("person", "B", "", "2026-01-01")
        store.create_merge_candidate(a.id, b.id, 0.82, "run-1")

        candidates = store.list_merge_candidates(status="pending")
        assert len(candidates) == 1
        assert candidates[0].entity_a.id == a.id
        assert candidates[0].entity_b.id == b.id
        assert candidates[0].similarity == pytest.approx(0.82)

    def test_resolve_candidate_dismissed(
        self, store: SQLiteEntityStore
    ) -> None:
        a = store.create_entity("person", "A", "", "2026-01-01")
        b = store.create_entity("person", "B", "", "2026-01-01")
        store.create_merge_candidate(a.id, b.id, 0.82, "run-1")

        candidates = store.list_merge_candidates()
        store.resolve_merge_candidate(candidates[0].id, "dismissed")

        assert store.list_merge_candidates(status="pending") == []
        dismissed = store.list_merge_candidates(status="dismissed")
        assert len(dismissed) == 1

    def test_resolve_candidate_accepted(
        self, store: SQLiteEntityStore
    ) -> None:
        a = store.create_entity("person", "A", "", "2026-01-01")
        b = store.create_entity("person", "B", "", "2026-01-01")
        store.create_merge_candidate(a.id, b.id, 0.82, "run-1")

        candidates = store.list_merge_candidates()
        store.resolve_merge_candidate(candidates[0].id, "accepted")

        assert store.list_merge_candidates(status="pending") == []

    def test_resolve_invalid_status_raises(
        self, store: SQLiteEntityStore
    ) -> None:
        with pytest.raises(ValueError, match="Invalid status"):
            store.resolve_merge_candidate(1, "invalid")

    def test_normalised_order(self, store: SQLiteEntityStore) -> None:
        """(a,b) and (b,a) should be the same candidate."""
        a = store.create_entity("person", "A", "", "2026-01-01")
        b = store.create_entity("person", "B", "", "2026-01-01")
        store.create_merge_candidate(b.id, a.id, 0.82, "run-1")

        candidates = store.list_merge_candidates()
        assert len(candidates) == 1
        assert candidates[0].entity_a.id == min(a.id, b.id)

    def test_merge_auto_resolves_candidates(
        self, store: SQLiteEntityStore, sample_entry_id: int
    ) -> None:
        a = store.create_entity("person", "A", "", "2026-01-01")
        b = store.create_entity("person", "B", "", "2026-01-01")
        store.create_merge_candidate(a.id, b.id, 0.85, "run-1")
        store.create_mention(a.id, sample_entry_id, "A", 0.9, "r1")

        store.merge_entities(b.id, [a.id])

        # Candidate should be auto-resolved
        assert store.list_merge_candidates(status="pending") == []
