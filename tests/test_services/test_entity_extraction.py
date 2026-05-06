"""Tests for EntityExtractionService."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from journal.db.repository import SQLiteEntryRepository
from journal.db.user_repository import SQLiteUserRepository
from journal.entitystore.store import SQLiteEntityStore
from journal.providers.extraction import RawExtractionResult
from journal.services.entity_extraction import (
    EntityExtractionService,
    _is_short_difference,
    _is_signature_match,
    _normalized_signature,
)

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
        # Entry text contains "Atlas" so the post-extraction sanity
        # sweep won't quarantine the dedup'd entity.
        e1 = repo.create_entry("2026-03-22", "photo", "Atlas one", 2).id
        e2 = repo.create_entry("2026-03-23", "photo", "Atlas two", 2).id

        extractor = MagicMock()
        extractor.extract_entities.side_effect = [
            _raw(entities=[_entity("Atlas", "person", quote="Atlas one")]),
            _raw(entities=[_entity("Atlas", "person", quote="Atlas two")]),
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


class TestDeletedEntryRace:
    """Defence-in-depth: FK errors are converted to ValueError when
    an entry is deleted while extraction is in progress."""

    def test_mention_fk_error_raises_value_error(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        import sqlite3 as _sqlite3

        entry = repo.create_entry("2026-03-22", "photo", "text", 1)
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw(
            entities=[_entity("Alice", "person", quote="Alice")],
        )

        # Simulate the FK error that occurs when the entry is deleted
        # between get_entry() and create_mention().
        original_create = entity_store.create_mention

        def fk_bomb(**kwargs):
            raise _sqlite3.IntegrityError("FOREIGN KEY constraint failed")

        entity_store.create_mention = fk_bomb
        service = _make_service(repo, entity_store, extractor)

        with pytest.raises(ValueError, match="deleted during extraction"):
            service.extract_from_entry(entry.id)

        entity_store.create_mention = original_create

    def test_relationship_fk_error_raises_value_error(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        import sqlite3 as _sqlite3

        entry = repo.create_entry("2026-03-22", "photo", "text", 1)
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw(
            entities=[
                _entity("Alice", "person", quote="Alice"),
                _entity("Bob", "person", quote="Bob"),
            ],
            relationships=[
                _rel("Alice", "knows", "Bob", quote="met"),
            ],
        )

        original_create_rel = entity_store.create_relationship

        def fk_bomb(**kwargs):
            raise _sqlite3.IntegrityError("FOREIGN KEY constraint failed")

        entity_store.create_relationship = fk_bomb
        service = _make_service(repo, entity_store, extractor)

        with pytest.raises(ValueError, match="deleted during extraction"):
            service.extract_from_entry(entry.id)

        entity_store.create_relationship = original_create_rel


class TestNormalizedSignatureHelpers:
    """Pure-function tests for the relaxed merge-candidate heuristic."""

    def test_normalized_signature_collapses_whitespace_and_case(self) -> None:
        assert _normalized_signature("Zij Kanaal C Weg") == "zijkanaalcweg"
        assert _normalized_signature("Zijkanaal C Weg") == "zijkanaalcweg"
        # Identical text differing only in whitespace should collapse to
        # the same signature.
        assert (
            _normalized_signature("Zij Kanaal")
            == _normalized_signature("ZijKanaal")
        )

    def test_normalized_signature_drops_trivial_punctuation(self) -> None:
        assert _normalized_signature("St. Mary-le-Bow") == "stmarylebow"
        assert _normalized_signature("St Mary, le Bow") == "stmarylebow"

    def test_is_short_difference_true_for_short_suffix(self) -> None:
        # "Zij Kanaal C" is contained in "Zij Kanaal C Weg"; leftover
        # "Weg" is a single token.
        assert _is_short_difference("Zij Kanaal C Weg", "Zij Kanaal C")

    def test_is_short_difference_false_when_not_substring(self) -> None:
        assert not _is_short_difference("Amsterdam", "Rotterdam")

    def test_is_short_difference_false_for_long_leftover(self) -> None:
        # Contains "Apple" but the leftover " has many trailing words" is
        # both longer than 6 chars AND multi-word — should not flag.
        assert not _is_short_difference(
            "Apple has many trailing words", "Apple"
        )

    def test_is_signature_match_equal_signatures(self) -> None:
        assert _is_signature_match("Zij Kanaal C Weg", "Zijkanaal C Weg")

    def test_is_signature_match_short_suffix_difference(self) -> None:
        # Differs in the trailing word — the user's prod case.
        assert _is_signature_match("Zij Kanaal C Weg", "Zij Kanaal C Zuid")

    def test_is_signature_match_whitespace_only_difference(self) -> None:
        assert _is_signature_match("Zij Kanaal", "ZijKanaal")

    def test_is_signature_match_unrelated_names(self) -> None:
        assert not _is_signature_match("Amsterdam", "Rotterdam")

    def test_is_signature_match_skips_identical_strings(self) -> None:
        # Pure equality is technically a signature match, but the caller
        # uses _is_signature_match to find *near* duplicates within the
        # same entity_type — identical strings can't pair with themselves
        # so this returns True and the caller filters self-pairs by id.
        assert _is_signature_match("Vienna", "Vienna")

    def test_is_signature_match_skips_empty_or_singlechar_names(self) -> None:
        # Avoid false positives on degenerate inputs.
        assert not _is_signature_match("", "Anything")
        assert not _is_signature_match("a", "Apple")


class TestSignatureMergeCandidatesDuringExtraction:
    """Integration: relaxed heuristic emits merge candidates during
    extraction even when embedding distance does not."""

    def test_two_places_with_whitespace_only_difference_flagged(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        # Pre-seed the existing place with an embedding that is clearly
        # NOT close to whatever the new extraction produces, so that the
        # embedding-distance path will not flag the pair.
        existing = entity_store.create_entity(
            "place", "Zijkanaal C Weg", "", "2026-03-01"
        )
        entity_store.set_entity_embedding(existing.id, [1.0, 0.0, 0.0])

        e = repo.create_entry("2026-03-22", "photo", "one", 1).id
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw(
            entities=[_entity("Zij Kanaal C Weg", "place")],
        )
        embeddings = MagicMock()
        # Orthogonal — embedding distance gives 0 similarity, well below
        # any threshold. Without the new heuristic, no candidate is created.
        embeddings.embed_query = MagicMock(return_value=[0.0, 1.0, 0.0])
        service = _make_service(
            repo, entity_store, extractor,
            embeddings=embeddings, threshold=0.9,
        )
        service.extract_from_entry(e)

        candidates = entity_store.list_merge_candidates(status="pending")
        names = {
            tuple(sorted([c.entity_a.canonical_name, c.entity_b.canonical_name]))
            for c in candidates
        }
        assert ("Zij Kanaal C Weg", "Zijkanaal C Weg") in names

    def test_two_places_differing_in_trailing_word_flagged(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        existing = entity_store.create_entity(
            "place", "Zij Kanaal C Weg", "", "2026-03-01"
        )
        entity_store.set_entity_embedding(existing.id, [1.0, 0.0, 0.0])

        e = repo.create_entry("2026-03-22", "photo", "one", 1).id
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw(
            entities=[_entity("Zij Kanaal C Zuid", "place")],
        )
        embeddings = MagicMock()
        embeddings.embed_query = MagicMock(return_value=[0.0, 1.0, 0.0])
        service = _make_service(
            repo, entity_store, extractor,
            embeddings=embeddings, threshold=0.9,
        )
        service.extract_from_entry(e)

        candidates = entity_store.list_merge_candidates(status="pending")
        names = {
            tuple(sorted([c.entity_a.canonical_name, c.entity_b.canonical_name]))
            for c in candidates
        }
        assert ("Zij Kanaal C Weg", "Zij Kanaal C Zuid") in names

    def test_unrelated_place_names_not_flagged_by_heuristic(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        # Both names have similar length but no signature overlap, and
        # we keep embeddings orthogonal so the embedding-distance path
        # cannot accidentally flag them either.
        existing = entity_store.create_entity(
            "place", "Amsterdam", "", "2026-03-01"
        )
        entity_store.set_entity_embedding(existing.id, [1.0, 0.0, 0.0])

        e = repo.create_entry("2026-03-22", "photo", "one", 1).id
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw(
            entities=[_entity("Rotterdam", "place")],
        )
        embeddings = MagicMock()
        embeddings.embed_query = MagicMock(return_value=[0.0, 1.0, 0.0])
        service = _make_service(
            repo, entity_store, extractor,
            embeddings=embeddings, threshold=0.9,
        )
        service.extract_from_entry(e)

        assert entity_store.list_merge_candidates(status="pending") == []

    def test_signature_match_skipped_across_entity_types(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        # A "Football" activity and a "Football" organization should not
        # be flagged — they're different types, so they can't be merged.
        existing = entity_store.create_entity(
            "activity", "Football", "", "2026-03-01"
        )
        entity_store.set_entity_embedding(existing.id, [1.0, 0.0, 0.0])

        e = repo.create_entry("2026-03-22", "photo", "one", 1).id
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw(
            entities=[_entity("Football", "organization")],
        )
        embeddings = MagicMock()
        embeddings.embed_query = MagicMock(return_value=[0.0, 1.0, 0.0])
        service = _make_service(
            repo, entity_store, extractor,
            embeddings=embeddings, threshold=0.9,
        )
        service.extract_from_entry(e)

        assert entity_store.list_merge_candidates(status="pending") == []


class TestPostExtractionSanitySweep:
    """WU4: after extraction, every entity touched in this run is
    re-checked. If its canonical name doesn't appear in any of its
    mention quotes or any mentioned entry's final_text, it is
    soft-quarantined. Catches LLM hallucinations that survive the
    provider-level repair stage and zombie-rebound entities (a
    hallucinated name re-bound to a corrected quote via embedding
    similarity).
    """

    def test_entity_with_canonical_name_in_quote_not_quarantined(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        entry = repo.create_entry(
            "2026-03-22", "photo", "I cycled along Vienna today.", 5,
        )
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw(
            entities=[_entity(
                "Vienna", "place", quote="cycled along Vienna today",
            )],
        )
        service = _make_service(repo, entity_store, extractor)
        service.extract_from_entry(entry.id)
        e = entity_store.get_entity_by_name("Vienna", "place")
        assert e is not None
        assert e.is_quarantined is False

    def test_entity_with_canonical_name_in_entry_text_but_not_quote_not_quarantined(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        """Quotes can be paraphrases or shortened — the entry text is
        the broader source. If the canonical appears in the entry text
        but not in the (clipped) quote, the entity is still legitimate.
        """
        entry = repo.create_entry(
            "2026-03-22",
            "photo",
            "I went cycling along Zij Kanaal today after lunch.",
            10,
        )
        extractor = MagicMock()
        # Quote is clipped — doesn't contain the full canonical, but
        # the entry text does.
        extractor.extract_entities.return_value = _raw(
            entities=[_entity(
                "Zij Kanaal", "place", quote="cycling along",
            )],
        )
        service = _make_service(repo, entity_store, extractor)
        service.extract_from_entry(entry.id)
        e = entity_store.get_entity_by_name("Zij Kanaal", "place")
        assert e is not None
        assert e.is_quarantined is False

    def test_entity_with_canonical_name_in_neither_quote_nor_entry_quarantined(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        """A canonical that doesn't appear anywhere — soft-quarantine.
        Pre-seed the entity and the quote so the provider-level repair
        wouldn't have caught it (we want to test the post-extraction
        sweep specifically)."""
        entry = repo.create_entry(
            "2026-03-22", "photo", "Just a quiet day at home.", 6,
        )
        extractor = MagicMock()
        # Quote contains 'a quiet day' (so the provider repair would
        # rename the canonical away from 'Atlantis'). To exercise the
        # SWEEP path specifically, pre-seed the entity directly.
        existing = entity_store.create_entity(
            "place", "Atlantis", "", "2026-03-01"
        )
        # And give it a mention with a quote that *also* doesn't contain
        # the canonical name.
        entity_store.create_mention(
            entity_id=existing.id,
            entry_id=entry.id,
            quote="quiet day",
            confidence=0.4,
            extraction_run_id="seed",
        )
        # Now re-extract — the LLM produces the same entity (matched on
        # canonical-by-name), with a quote that ALSO lacks 'Atlantis'.
        # The provider repair won't fire (we bypass it via _resolve), so
        # the entity is `touched` in this run; the sweep must quarantine.
        extractor.extract_entities.return_value = _raw(
            entities=[_entity("Atlantis", "place", quote="quiet day")],
        )
        service = _make_service(repo, entity_store, extractor)
        service.extract_from_entry(entry.id)

        refreshed = entity_store.get_entity(
            existing.id, user_id=None,
        )
        # If the post-extraction sweep is wired in, this is True.
        assert refreshed is not None
        assert refreshed.is_quarantined is True
        assert refreshed.quarantine_reason
        assert "Atlantis" in refreshed.quarantine_reason

    def test_reextraction_after_entry_edit_quarantines_orphaned_canonical(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        """Headline regression test for the prod ``Zij Kanaal C Zuid``
        incident.

        Reproduction:
          1. Original entry text mentions ``Zij Kanaal C Zuid``; an
             entity is created with that canonical_name and a mention
             whose quote contains the full string.
          2. The user edits the entry (final_text correction) and
             removes the stray "Zuid", leaving only ``Zij Kanaal C``.
          3. Re-extraction is triggered. The new mention's quote
             contains ``Zij Kanaal C`` only. Because of the existing
             embedding-similarity dedup logic (and the old canonical
             name still being a substring on the alias ladder), the
             new mention re-binds to the SAME entity that still carries
             the hallucinated canonical ``Zij Kanaal C Zuid``.
          4. After the post-extraction sanity sweep runs, the entity
             must be quarantined — its canonical can't be found in any
             quote or entry text any more.
        """
        # 1. seed: original entry + entity with the full hallucinated name.
        entry = repo.create_entry(
            "2026-03-22",
            "photo",
            "I cycled past Zij Kanaal C Zuid this afternoon.",
            10,
        )
        original_entity = entity_store.create_entity(
            "place", "Zij Kanaal C Zuid", "", "2026-03-01"
        )
        entity_store.create_mention(
            entity_id=original_entity.id,
            entry_id=entry.id,
            quote="cycled past Zij Kanaal C Zuid this afternoon",
            confidence=0.9,
            extraction_run_id="initial",
        )

        # 2. user edits entry text: drops the stray "Zuid".
        repo.update_final_text(
            entry.id,
            "I cycled past Zij Kanaal C this afternoon.",
            8,
            1,
        )

        # 3. re-extract. The LLM emits 'Zij Kanaal C Zuid' (the same
        #    canonical it returned originally), with a quote that
        #    contains only 'Zij Kanaal C'. The provider's longest-
        #    substring repair will rename it to 'Zij Kanaal C', but
        #    because of stage-a/b matching this re-binds to the SAME
        #    original entity (745 in prod). Wait — that won't actually
        #    happen because the old entity has a different canonical
        #    after rename. To reproduce the prod path more faithfully,
        #    we feed the LLM raw extraction with a quote that DOES
        #    contain the full hallucinated string (so the provider
        #    repair leaves the canonical alone), and let the sweep
        #    catch the still-orphan canonical via the entry-text
        #    check.
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw(
            entities=[
                _entity(
                    "Zij Kanaal C Zuid",
                    "place",
                    quote="cycled past Zij Kanaal C Zuid this afternoon",
                ),
            ],
        )
        service = _make_service(repo, entity_store, extractor)
        service.extract_from_entry(entry.id)

        # Sanity: the entity was matched (no new entity created).
        assert entity_store.count_entities(include_quarantined=True) == 1

        # 4. post-sanity sweep: 'Zij Kanaal C Zuid' is in the new quote
        #    but NOT in the entry's final_text. The current sweep checks
        #    quote-or-entry-text — quote alone is enough to clear it. To
        #    exercise the regression in the user's prod case (where the
        #    entry text is the source of truth), we now strip "Zuid"
        #    from the quote too via a second re-extraction round whose
        #    quote no longer contains the hallucinated tail.
        extractor.extract_entities.return_value = _raw(
            entities=[
                _entity(
                    "Zij Kanaal C Zuid",
                    "place",
                    # Quote is now the corrected snippet — does NOT
                    # contain the stray "Zuid". The provider repair
                    # WILL rename this; but for the sweep to fire we
                    # bypass that by pre-seeding via the store API.
                    quote="cycled past Zij Kanaal C this afternoon",
                ),
            ],
        )
        service.extract_from_entry(entry.id)

        # The entity matched against the seeded canonical (stage a),
        # so its name is still 'Zij Kanaal C Zuid'. The sweep checks
        # quotes (now: 'Zij Kanaal C') and entry text (now: 'Zij Kanaal
        # C') — neither contains 'Zij Kanaal C Zuid'. Quarantine.
        refreshed = entity_store.get_entity(original_entity.id)
        assert refreshed is not None
        assert refreshed.is_quarantined is True
        assert "Zij Kanaal C Zuid" in refreshed.quarantine_reason

    def test_quarantined_entity_can_be_released_via_store(
        self,
        entity_store: SQLiteEntityStore,
    ) -> None:
        """Orthogonal sanity check — soft-quarantine is reversible."""
        e = entity_store.create_entity(
            "place", "Atlantis", "", "2026-03-01"
        )
        entity_store.quarantine_entity(e.id, "test reason")
        refreshed = entity_store.get_entity(e.id)
        assert refreshed is not None and refreshed.is_quarantined is True
        entity_store.release_quarantine(e.id)
        refreshed = entity_store.get_entity(e.id)
        assert refreshed is not None and refreshed.is_quarantined is False
