"""Tests for EntityExtractionService."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from journal.db.repository import SQLiteEntryRepository
from journal.db.user_repository import SQLiteUserRepository
from journal.entitystore.store import SQLiteEntityStore
from journal.providers.extraction import RawExtractionResult
from journal.services.entity_extraction import EntityExtractionService

if TYPE_CHECKING:
    import sqlite3


@pytest.fixture
def repo(db_conn: sqlite3.Connection) -> SQLiteEntryRepository:
    return SQLiteEntryRepository(db_conn)


@pytest.fixture
def entity_store(db_conn: sqlite3.Connection) -> SQLiteEntityStore:
    return SQLiteEntityStore(db_conn)


def _raw(
    entities: list[dict] | None = None,
    relationships: list[dict] | None = None,
) -> RawExtractionResult:
    return RawExtractionResult(
        entities=entities or [],
        relationships=relationships or [],
    )


def _entity(
    canonical_name: str,
    entity_type: str = "person",
    description: str = "",
    aliases: list[str] | None = None,
    quote: str = "",
    confidence: float = 0.9,
) -> dict:
    return {
        "entity_type": entity_type,
        "canonical_name": canonical_name,
        "description": description,
        "aliases": aliases or [],
        "quote": quote,
        "confidence": confidence,
    }


def _rel(
    subject: str,
    predicate: str,
    obj: str,
    quote: str = "",
    confidence: float = 0.9,
) -> dict:
    return {
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "quote": quote,
        "confidence": confidence,
    }


@pytest.fixture
def sample_entry(repo: SQLiteEntryRepository) -> int:
    entry = repo.create_entry(
        "2026-03-22",
        "photo",
        "I went to Vienna with Atlas today.",
        8,
    )
    return entry.id


def _make_service(
    repo: SQLiteEntryRepository,
    store: SQLiteEntityStore,
    extractor: MagicMock,
    *,
    author_name: str = "John",
    threshold: float = 0.88,
    embeddings: MagicMock | None = None,
    user_repo: SQLiteUserRepository | None = None,
) -> EntityExtractionService:
    if embeddings is None:
        embeddings = MagicMock()
        embeddings.embed_query = MagicMock(return_value=[0.0] * 8)
    return EntityExtractionService(
        repository=repo,
        entity_store=store,
        extraction_provider=extractor,
        embeddings_provider=embeddings,
        author_name=author_name,
        dedup_similarity_threshold=threshold,
        user_repo=user_repo,
    )


class TestHappyPath:
    def test_basic_entity_and_relationship(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
        sample_entry: int,
    ) -> None:
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw(
            entities=[
                _entity("John", "person", quote="I went"),
                _entity("Atlas", "person", quote="with Atlas"),
                _entity("Vienna", "place", quote="to Vienna"),
            ],
            relationships=[
                _rel("John", "visited", "Vienna", "I went to Vienna"),
                _rel("John", "knows", "Atlas", "with Atlas"),
            ],
        )
        service = _make_service(repo, entity_store, extractor)

        result = service.extract_from_entry(sample_entry)
        assert result.entities_created == 3
        assert result.entities_matched == 0
        assert result.mentions_created == 3
        assert result.relationships_created == 2
        assert result.warnings == []

        assert entity_store.count_entities() == 3
        mentions = entity_store.get_mentions_for_entry(sample_entry)
        assert len(mentions) == 3
        rels = entity_store.get_relationships_for_entry(sample_entry)
        assert len(rels) == 2

    def test_entry_marked_extracted_after_success(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
        sample_entry: int,
        db_conn: sqlite3.Connection,
    ) -> None:
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw()
        service = _make_service(repo, entity_store, extractor)
        service.extract_from_entry(sample_entry)
        row = db_conn.execute(
            "SELECT entity_extraction_stale FROM entries WHERE id = ?",
            (sample_entry,),
        ).fetchone()
        assert row["entity_extraction_stale"] == 0


class TestDedupExactName:
    def test_second_extraction_reuses_existing(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        e1 = repo.create_entry("2026-03-22", "photo", "one", 1).id
        e2 = repo.create_entry("2026-03-23", "photo", "two", 1).id

        extractor = MagicMock()
        extractor.extract_entities.side_effect = [
            _raw(entities=[_entity("Atlas", "person")]),
            _raw(entities=[_entity("Atlas", "person")]),
        ]
        service = _make_service(repo, entity_store, extractor)

        r1 = service.extract_from_entry(e1)
        r2 = service.extract_from_entry(e2)
        assert r1.entities_created == 1
        assert r2.entities_created == 0
        assert r2.entities_matched == 1
        assert entity_store.count_entities() == 1


class TestDedupAliasMatch:
    def test_alias_matches_existing_entity(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        # Pre-seed an entity with an alias.
        existing = entity_store.create_entity(
            "person", "Atlas Wong", "", "2026-03-01"
        )
        entity_store.add_alias(existing.id, "Atty")

        e1 = repo.create_entry("2026-03-22", "photo", "one", 1).id
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw(
            entities=[_entity("Atty", "person")],
        )
        service = _make_service(repo, entity_store, extractor)
        r = service.extract_from_entry(e1)
        assert r.entities_created == 0
        assert r.entities_matched == 1


class TestDedupEmbeddingSimilarity:
    def test_embedding_fallback_produces_warning(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        # Pre-seed an entity WITH an embedding so the fallback has
        # something to compare against.
        existing = entity_store.create_entity(
            "person", "Dr Atlas Wong", "", "2026-03-01"
        )
        entity_store.set_entity_embedding(existing.id, [1.0, 0.0, 0.0])

        e1 = repo.create_entry("2026-03-22", "photo", "one", 1).id
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw(
            entities=[_entity("Atlas W.", "person")],
        )
        # Return an embedding close to the existing one.
        embeddings = MagicMock()
        embeddings.embed_query = MagicMock(return_value=[0.99, 0.01, 0.0])

        service = _make_service(
            repo, entity_store, extractor, embeddings=embeddings, threshold=0.9
        )
        r = service.extract_from_entry(e1)
        assert r.entities_created == 0
        assert r.entities_matched == 1
        assert any("potential merge" in w for w in r.warnings)

    def test_below_threshold_creates_new_entity(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        existing = entity_store.create_entity(
            "person", "Somebody Else", "", "2026-03-01"
        )
        entity_store.set_entity_embedding(existing.id, [1.0, 0.0, 0.0])

        e1 = repo.create_entry("2026-03-22", "photo", "one", 1).id
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw(
            entities=[_entity("Atlas", "person")],
        )
        embeddings = MagicMock()
        embeddings.embed_query = MagicMock(return_value=[0.0, 1.0, 0.0])
        service = _make_service(
            repo, entity_store, extractor, embeddings=embeddings, threshold=0.9
        )
        r = service.extract_from_entry(e1)
        assert r.entities_created == 1
        assert r.warnings == []


class TestIdempotency:
    def test_rerun_replaces_mentions(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
        sample_entry: int,
    ) -> None:
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw(
            entities=[_entity("Atlas", "person", quote="q1")],
            relationships=[],
        )
        service = _make_service(repo, entity_store, extractor)
        service.extract_from_entry(sample_entry)
        service.extract_from_entry(sample_entry)

        mentions = entity_store.get_mentions_for_entry(sample_entry)
        assert len(mentions) == 1

    def test_rerun_replaces_relationships(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
        sample_entry: int,
    ) -> None:
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw(
            entities=[
                _entity("John", "person"),
                _entity("Vienna", "place"),
            ],
            relationships=[_rel("John", "visited", "Vienna")],
        )
        service = _make_service(repo, entity_store, extractor)
        service.extract_from_entry(sample_entry)
        service.extract_from_entry(sample_entry)
        rels = entity_store.get_relationships_for_entry(sample_entry)
        assert len(rels) == 1

    def test_rerun_deletes_orphaned_entities(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
        sample_entry: int,
    ) -> None:
        """When re-extraction no longer finds an entity that was only
        mentioned in this entry, the entity should be deleted."""
        extractor = MagicMock()
        extractor.extract_entities.side_effect = [
            _raw(entities=[
                _entity("Atlas", "person", quote="Atlas"),
                _entity("Vienna", "place", quote="Vienna"),
            ]),
            # Second run: Atlas is no longer mentioned.
            _raw(entities=[
                _entity("Vienna", "place", quote="Vienna"),
            ]),
        ]
        service = _make_service(repo, entity_store, extractor)
        service.extract_from_entry(sample_entry)
        assert entity_store.count_entities() == 2

        service.extract_from_entry(sample_entry)
        assert entity_store.count_entities() == 1
        assert entity_store.get_entity_by_name("Atlas", "person") is None
        assert entity_store.get_entity_by_name("Vienna", "place") is not None

    def test_rerun_keeps_entity_mentioned_in_other_entries(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
        sample_entry: int,
    ) -> None:
        """An entity that disappears from one entry but is still
        mentioned in another should NOT be deleted."""
        e2 = repo.create_entry("2026-03-23", "photo", "Other entry", 5).id
        extractor = MagicMock()
        extractor.extract_entities.side_effect = [
            # Entry 1: Atlas + Vienna
            _raw(entities=[
                _entity("Atlas", "person", quote="Atlas"),
                _entity("Vienna", "place", quote="Vienna"),
            ]),
            # Entry 2: Atlas only
            _raw(entities=[
                _entity("Atlas", "person", quote="Atlas"),
            ]),
            # Re-run entry 1: Vienna only (Atlas gone from this entry)
            _raw(entities=[
                _entity("Vienna", "place", quote="Vienna"),
            ]),
        ]
        service = _make_service(repo, entity_store, extractor)
        service.extract_from_entry(sample_entry)
        service.extract_from_entry(e2)
        assert entity_store.count_entities() == 2

        service.extract_from_entry(sample_entry)
        # Atlas should survive — still mentioned in e2.
        assert entity_store.count_entities() == 2
        assert entity_store.get_entity_by_name("Atlas", "person") is not None


class TestAuthorPronoun:
    def test_author_created_lazily_via_relationship(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
        sample_entry: int,
    ) -> None:
        # LLM does NOT include John in its entity list — only Vienna.
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw(
            entities=[_entity("Vienna", "place", quote="to Vienna")],
            relationships=[_rel("John", "visited", "Vienna")],
        )
        service = _make_service(repo, entity_store, extractor)
        result = service.extract_from_entry(sample_entry)

        # John should have been auto-created as a person.
        john = entity_store.get_entity_by_name("John", "person")
        assert john is not None
        assert result.relationships_created == 1


class TestBatchExtraction:
    def test_batch_collects_per_entry_failures(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        e1 = repo.create_entry("2026-03-22", "photo", "first", 1).id
        e2 = repo.create_entry("2026-03-23", "photo", "second", 1).id

        extractor = MagicMock()

        def side_effect(entry_text, entry_date, author_name):  # type: ignore[no-untyped-def]
            if "first" in entry_text:
                return _raw(entities=[_entity("Atlas", "person")])
            raise RuntimeError("boom")

        extractor.extract_entities.side_effect = side_effect
        service = _make_service(repo, entity_store, extractor)
        results = service.extract_batch(entry_ids=[e1, e2])
        assert len(results) == 2
        assert results[0].entities_created == 1
        assert any("extraction failed" in w for w in results[1].warnings)

    def test_stale_only_filter(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        e1 = repo.create_entry("2026-03-22", "photo", "first", 1).id
        e2 = repo.create_entry("2026-03-23", "photo", "second", 1).id
        # Mark e1 as already extracted so stale_only should skip it.
        entity_store.mark_entry_extracted(e1)

        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw()
        service = _make_service(repo, entity_store, extractor)

        results = service.extract_batch(stale_only=True)
        ids = {r.entry_id for r in results}
        assert e1 not in ids
        assert e2 in ids


class TestErrors:
    def test_missing_entry_raises(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        extractor = MagicMock()
        service = _make_service(repo, entity_store, extractor)
        with pytest.raises(ValueError, match="not found"):
            service.extract_from_entry(999)


class TestBatchProgressCallback:
    def test_extract_batch_calls_progress_callback(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        e1 = repo.create_entry("2026-03-22", "photo", "first", 1).id
        e2 = repo.create_entry("2026-03-23", "photo", "second", 1).id
        e3 = repo.create_entry("2026-03-24", "photo", "third", 1).id

        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw()
        service = _make_service(repo, entity_store, extractor)

        calls: list[tuple[int, int]] = []

        def spy(current: int, total: int) -> None:
            calls.append((current, total))

        service.extract_batch(entry_ids=[e1, e2, e3], on_progress=spy)

        assert calls == [(0, 3), (1, 3), (2, 3), (3, 3)]

    def test_extract_batch_progress_reports_on_failure(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        """Progress must advance even when an entry fails."""
        e1 = repo.create_entry("2026-03-22", "photo", "first", 1).id
        e2 = repo.create_entry("2026-03-23", "photo", "second", 1).id

        extractor = MagicMock()

        def side_effect(entry_text, entry_date, author_name):  # type: ignore[no-untyped-def]
            if "first" in entry_text:
                return _raw(entities=[_entity("Atlas", "person")])
            raise RuntimeError("boom")

        extractor.extract_entities.side_effect = side_effect
        service = _make_service(repo, entity_store, extractor)

        calls: list[tuple[int, int]] = []
        service.extract_batch(
            entry_ids=[e1, e2],
            on_progress=lambda c, t: calls.append((c, t)),
        )
        assert calls == [(0, 2), (1, 2), (2, 2)]

    def test_extract_batch_raising_callback_does_not_break_batch(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        e1 = repo.create_entry("2026-03-22", "photo", "first", 1).id
        e2 = repo.create_entry("2026-03-23", "photo", "second", 1).id

        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw()
        service = _make_service(repo, entity_store, extractor)

        def boom(current: int, total: int) -> None:
            raise RuntimeError("callback kaboom")

        with caplog.at_level("WARNING", logger="journal.services.entity_extraction"):
            results = service.extract_batch(
                entry_ids=[e1, e2], on_progress=boom
            )

        # Batch still completed for every entry.
        assert len(results) == 2
        # And the failure was logged rather than propagated.
        assert any(
            "Progress callback failed" in rec.message for rec in caplog.records
        )


class TestMultiUserAuthorName:
    """Entity extraction should use the entry owner's display name, not
    the global default, when resolving first-person pronouns."""

    @pytest.fixture
    def user_repo(self, db_conn: sqlite3.Connection) -> SQLiteUserRepository:
        return SQLiteUserRepository(db_conn)

    def test_extraction_uses_owner_display_name(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
        user_repo: SQLiteUserRepository,
    ) -> None:
        """When user 'Demo User' writes 'I visited Paris', the LLM
        should be told the author is 'Demo User', not 'John'."""
        demo = user_repo.create_user("demo@example.com", "Demo User")
        entry = repo.create_entry(
            "2026-04-15", "photo", "I visited Paris today.", 5,
            user_id=demo.id,
        )

        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw(
            entities=[
                _entity("Demo User", "person", quote="I visited"),
                _entity("Paris", "place", quote="Paris"),
            ],
            relationships=[
                _rel("Demo User", "visited", "Paris", "I visited Paris"),
            ],
        )
        service = _make_service(
            repo, entity_store, extractor,
            author_name="John",
            user_repo=user_repo,
        )
        result = service.extract_from_entry(entry.id)

        # Verify the LLM was called with the correct author name.
        call_kwargs = extractor.extract_entities.call_args
        assert call_kwargs.kwargs["author_name"] == "Demo User"

        # Verify relationships were created correctly.
        assert result.relationships_created == 1
        assert result.warnings == []

    def test_extraction_falls_back_to_default_without_user_repo(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        """Without a user_repo, falls back to the global author_name."""
        entry = repo.create_entry(
            "2026-04-15", "photo", "I visited Paris.", 3,
        )
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw()
        service = _make_service(
            repo, entity_store, extractor,
            author_name="John",
            user_repo=None,
        )
        service.extract_from_entry(entry.id)

        call_kwargs = extractor.extract_entities.call_args
        assert call_kwargs.kwargs["author_name"] == "John"

    def test_author_entity_created_with_correct_name_for_non_default_user(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
        user_repo: SQLiteUserRepository,
    ) -> None:
        """When the LLM emits a relationship with the author as subject,
        the auto-created author entity should use the user's display
        name, not the global default."""
        harry = user_repo.create_user("harry@example.com", "Harry")
        entry = repo.create_entry(
            "2026-04-15", "photo", "I played squash today.", 4,
            user_id=harry.id,
        )

        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw(
            entities=[_entity("squash", "activity", quote="squash")],
            relationships=[
                _rel("Harry", "plays", "squash", "I played squash"),
            ],
        )
        service = _make_service(
            repo, entity_store, extractor,
            author_name="John",
            user_repo=user_repo,
        )
        result = service.extract_from_entry(entry.id)

        # Harry should have been auto-created, not John.
        harry_entity = entity_store.get_entity_by_name("Harry", "person")
        john_entity = entity_store.get_entity_by_name("John", "person")
        assert harry_entity is not None
        assert john_entity is None
        assert result.relationships_created == 1

    def test_two_users_get_separate_author_entities(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
        user_repo: SQLiteUserRepository,
    ) -> None:
        """Entries from different users should create distinct author
        entities with each user's display name."""
        john = user_repo.create_user("john@example.com", "John")
        demo = user_repo.create_user("demo@example.com", "Demo")

        entry_john = repo.create_entry(
            "2026-04-15", "photo", "I went to the gym.", 6,
            user_id=john.id,
        )
        entry_demo = repo.create_entry(
            "2026-04-15", "photo", "I went to the park.", 6,
            user_id=demo.id,
        )

        extractor = MagicMock()
        extractor.extract_entities.side_effect = [
            _raw(
                entities=[
                    _entity("John", "person", quote="I went"),
                    _entity("gym", "place", quote="the gym"),
                ],
                relationships=[_rel("John", "visited", "gym")],
            ),
            _raw(
                entities=[
                    _entity("Demo", "person", quote="I went"),
                    _entity("park", "place", quote="the park"),
                ],
                relationships=[_rel("Demo", "visited", "park")],
            ),
        ]
        service = _make_service(
            repo, entity_store, extractor,
            author_name="John",
            user_repo=user_repo,
        )

        service.extract_from_entry(entry_john.id)
        service.extract_from_entry(entry_demo.id)

        # Verify the LLM was called with the correct author for each.
        calls = extractor.extract_entities.call_args_list
        assert calls[0].kwargs["author_name"] == "John"
        assert calls[1].kwargs["author_name"] == "Demo"
