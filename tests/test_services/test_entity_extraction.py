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
    llm_candidate_top_k: int = 30,
    llm_candidate_threshold: float = 0.4,
    llm_match_min_cosine: float = 0.3,
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
        llm_candidate_top_k=llm_candidate_top_k,
        llm_candidate_threshold=llm_candidate_threshold,
        llm_match_min_cosine=llm_match_min_cosine,
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

        def side_effect(entry_text, entry_date, author_name, **kwargs):  # type: ignore[no-untyped-def]
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

        def side_effect(entry_text, entry_date, author_name, **kwargs):  # type: ignore[no-untyped-def]
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

    def test_is_short_difference_true_for_typo_leftover(self) -> None:
        # Single-character drift — the canonical typo case Case 2 was
        # designed to catch.
        assert _is_short_difference("Andrews", "Andrew")

    def test_is_short_difference_false_for_word_leftover(self) -> None:
        # WU3: a real-word leftover ("Weg") indicates a more specific
        # entity (the canal road), not a typo of the shorter name.
        assert not _is_short_difference("Zij Kanaal C Weg", "Zij Kanaal C")

    def test_is_short_difference_false_for_possessive_leftover(self) -> None:
        # Relational suffix — different person.
        assert not _is_short_difference(
            "JohnMathews'mother", "JohnMathews"
        )

    def test_is_short_difference_false_when_not_substring(self) -> None:
        assert not _is_short_difference("Amsterdam", "Rotterdam")

    def test_is_short_difference_false_for_long_leftover(self) -> None:
        # Contains "Apple" but the leftover " has many trailing words" is
        # longer than 6 chars and word-shaped — should not flag.
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

    # WU3 regression cases — every pair below produced a 0.95 signature
    # match in the user's prod database that was then dismissed as
    # "not a duplicate". Each represents a distinct false-positive
    # pattern the heuristic used to mistake for a typo.

    def test_relational_possessive_suffix_not_matched(self) -> None:
        assert not _is_signature_match(
            "John Mathews", "John Mathews' mother"
        )
        assert not _is_signature_match(
            "John Mathews", "John Mathews’ mother"
        )  # curly apostrophe

    def test_numeric_specifier_not_matched(self) -> None:
        assert not _is_signature_match("Psalms", "Psalms 63")
        assert not _is_signature_match("Highway", "Highway 5")

    def test_word_specifier_suffix_not_matched(self) -> None:
        assert not _is_signature_match("Bible", "Bible study")
        assert not _is_signature_match("Interview", "Interview practice")
        assert not _is_signature_match("Haarlem", "Haarlem Centraal")
        assert not _is_signature_match("Spaarne", "Spaarnebuiten")

    def test_word_qualifier_prefix_not_matched(self) -> None:
        # Prefix-side qualifier (acronym or noun) on the longer name —
        # almost always a different concept.
        assert not _is_signature_match("pipelines", "RAG pipelines")
        assert not _is_signature_match(
            "Engineering", "Chaos Engineering"
        )

    def test_common_suffix_with_wordy_prefix_tails_not_matched(self) -> None:
        # Common suffix "engineering" but distinct front-words — the
        # Case-3 SUFFIX branch should reject these.
        assert not _is_signature_match(
            "Chaos Engineering", "Data Engineering"
        )

    def test_typo_recall_preserved(self) -> None:
        # Single-character drift at the end (OCR letter dropped/added)
        # should still match.
        assert _is_signature_match("Andrew", "Andrews")
        # Empty-leftover (whitespace-only difference) still matches.
        assert _is_signature_match("Zij Kanaal", "ZijKanaal")

    def test_short_place_qualifiers_still_matched(self) -> None:
        # Case 3 prefix branch with short place qualifiers — preserved
        # by the more lenient ``allow_short_words`` threshold so that
        # near-duplicate Dutch place names still get flagged.
        assert _is_signature_match(
            "Zij Kanaal C Weg", "Zij Kanaal C Zuid"
        )


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


class TestNoEmbeddingNearMissCandidates:
    """WU4: the embedding "near-miss" candidate creation path was
    removed. Below-threshold embedding similarity now produces no
    candidate at all — only the signature heuristic creates them."""

    def test_below_threshold_above_old_near_miss_creates_no_candidate(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        # Embedding similarity ~0.80: previously surfaced as a "near
        # miss" candidate (threshold 0.9 minus 0.15 floor → 0.75). With
        # WU4 it is silently dropped.
        existing = entity_store.create_entity(
            "person", "Sarah", "", "2026-03-01",
        )
        # Two near-parallel unit vectors give cosine ≈ 0.80.
        entity_store.set_entity_embedding(existing.id, [1.0, 0.0, 0.0])

        e = repo.create_entry("2026-03-22", "photo", "one", 1).id
        extractor = MagicMock()
        # Different name so signature heuristic doesn't fire.
        extractor.extract_entities.return_value = _raw(
            entities=[_entity("Bob", "person")],
        )
        embeddings = MagicMock()
        embeddings.embed_query = MagicMock(
            return_value=[0.8, 0.6, 0.0],  # cos = 0.8 vs [1, 0, 0]
        )
        service = _make_service(
            repo, entity_store, extractor,
            embeddings=embeddings, threshold=0.9,
        )
        service.extract_from_entry(e)

        assert entity_store.list_merge_candidates(status="pending") == []


class TestSignatureCandidateSkipsWhenRejected:
    """WU1: when the user has previously dismissed a candidate, the
    signature heuristic should not re-suggest the pair on subsequent
    extractions."""

    def test_signature_match_suppressed_for_rejected_pair(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        existing = entity_store.create_entity(
            "place", "Zij Kanaal C Weg", "", "2026-03-01"
        )
        entity_store.set_entity_embedding(existing.id, [1.0, 0.0, 0.0])
        # Stand-in for the (yet-to-exist) sibling entity. Pre-create it
        # so we can record a rejection between the two ids before the
        # extraction runs.
        sibling = entity_store.create_entity(
            "place", "Zij Kanaal C Zuid", "", "2026-03-01"
        )
        entity_store.set_entity_embedding(sibling.id, [0.0, 1.0, 0.0])
        # Reject the pair as the user would have via the dismiss flow.
        entity_store.record_pair_rejection(1, existing.id, sibling.id)

        # Now run extraction that would normally trigger the signature
        # heuristic for this pair (existing canonical re-extracted).
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


class TestReembedDescription:
    """WU2: re-embed an entity from its current name + description.

    Used by the ``entity_reembed`` background job that fires when the
    user edits a description in the webapp. Without this the stored
    embedding would stay frozen at creation-time text.
    """

    def test_reembed_updates_stored_embedding(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        entity = entity_store.create_entity(
            "person", "Sarah", "my mother", "2026-01-01",
        )
        extractor = MagicMock()
        embeddings = MagicMock()
        embeddings.embed_query = MagicMock(return_value=[0.42, -0.3, 0.7])
        service = _make_service(repo, entity_store, extractor, embeddings=embeddings)

        result = service.reembed_entity_for_description(
            entity.id, user_id=1,
        )

        assert result["embedded"] is True
        assert result["entity_id"] == entity.id
        assert result["dimensions"] == 3
        embeddings.embed_query.assert_called_once_with("Sarah my mother")
        stored = entity_store.get_entity_embedding(entity.id)
        assert stored == [0.42, -0.3, 0.7]

    def test_reembed_uses_current_description_after_update(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        entity = entity_store.create_entity(
            "person", "Sarah", "old description", "2026-01-01",
        )
        # Simulate the user editing the description.
        entity_store.update_entity(entity.id, description="my mother, lives in Edinburgh")

        extractor = MagicMock()
        embeddings = MagicMock()
        embeddings.embed_query = MagicMock(return_value=[1.0])
        service = _make_service(repo, entity_store, extractor, embeddings=embeddings)

        service.reembed_entity_for_description(entity.id, user_id=1)
        embeddings.embed_query.assert_called_once_with(
            "Sarah my mother, lives in Edinburgh"
        )

    def test_reembed_skips_when_description_empty(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        entity = entity_store.create_entity(
            "person", "Sarah", "", "2026-01-01",
        )
        extractor = MagicMock()
        embeddings = MagicMock()
        service = _make_service(repo, entity_store, extractor, embeddings=embeddings)

        result = service.reembed_entity_for_description(entity.id, user_id=1)

        assert result["embedded"] is False
        assert result["reason"] == "empty description"
        embeddings.embed_query.assert_not_called()
        # Embedding column stays None.
        assert entity_store.get_entity_embedding(entity.id) is None

    def test_reembed_skips_when_description_whitespace_only(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        entity = entity_store.create_entity(
            "person", "Sarah", "   \n  ", "2026-01-01",
        )
        extractor = MagicMock()
        embeddings = MagicMock()
        service = _make_service(repo, entity_store, extractor, embeddings=embeddings)

        result = service.reembed_entity_for_description(entity.id, user_id=1)
        assert result["embedded"] is False
        embeddings.embed_query.assert_not_called()

    def test_reembed_missing_entity_raises(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        extractor = MagicMock()
        embeddings = MagicMock()
        service = _make_service(repo, entity_store, extractor, embeddings=embeddings)
        with pytest.raises(ValueError, match="not found"):
            service.reembed_entity_for_description(9999, user_id=1)

    def test_reembed_user_scoped(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
        db_conn,
    ) -> None:
        # An entity owned by user 1 cannot be reembedded under user 2.
        db_conn.execute(
            "INSERT INTO users (email, display_name, is_admin, email_verified) "
            "VALUES ('u2@test.com', 'User Two', 0, 1)"
        )
        entity = entity_store.create_entity(
            "person", "Sarah", "my mother", "2026-01-01", user_id=1,
        )
        extractor = MagicMock()
        embeddings = MagicMock()
        embeddings.embed_query = MagicMock(return_value=[0.5, 0.5])
        service = _make_service(repo, entity_store, extractor, embeddings=embeddings)
        with pytest.raises(ValueError, match="not found"):
            service.reembed_entity_for_description(entity.id, user_id=2)
        # Correct user works.
        result = service.reembed_entity_for_description(entity.id, user_id=1)
        assert result["embedded"] is True


class TestBuildKnownEntityCandidates:
    """WU4-C: vector pre-filter for the known-entities prompt block."""

    def test_returns_top_k_above_threshold_sorted_by_score(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        # Three entities with embeddings; one is far from the entry,
        # the other two are close. Threshold 0.4 should drop the far
        # one; results should be sorted by descending similarity.
        sarah = entity_store.create_entity(
            "person", "Sarah", "my mother", "2026-01-01",
        )
        entity_store.set_entity_embedding(sarah.id, [1.0, 0.0, 0.0])
        atlas = entity_store.create_entity(
            "person", "Atlas", "the dog", "2026-01-01",
        )
        entity_store.set_entity_embedding(atlas.id, [0.7, 0.7, 0.0])
        far = entity_store.create_entity(
            "place", "Antarctica", "cold", "2026-01-01",
        )
        entity_store.set_entity_embedding(far.id, [0.0, 0.0, 1.0])

        embeddings = MagicMock()
        embeddings.embed_query = MagicMock(return_value=[1.0, 0.0, 0.0])
        service = _make_service(
            repo, entity_store, MagicMock(),
            embeddings=embeddings, llm_candidate_threshold=0.4,
        )

        candidates, by_id = service.build_known_entity_candidates(
            "I called Mum", user_id=1,
        )
        ids_in_order = [c["id"] for c in candidates]
        assert ids_in_order == [sarah.id, atlas.id]
        assert far.id not in by_id

    def test_top_k_caps_results(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        # Five entities, top_k=2 → only 2 returned.
        for i in range(5):
            e = entity_store.create_entity(
                "person", f"Person {i}", f"desc {i}", "2026-01-01",
            )
            entity_store.set_entity_embedding(e.id, [1.0, 0.0])

        embeddings = MagicMock()
        embeddings.embed_query = MagicMock(return_value=[1.0, 0.0])
        service = _make_service(
            repo, entity_store, MagicMock(),
            embeddings=embeddings,
            llm_candidate_top_k=2, llm_candidate_threshold=0.0,
        )
        candidates, _ = service.build_known_entity_candidates(
            "any text", user_id=1,
        )
        assert len(candidates) == 2

    def test_returns_empty_when_user_has_no_embedded_entities(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        # Entity exists but no embedding set yet.
        entity_store.create_entity("person", "Sarah", "", "2026-01-01")
        embeddings = MagicMock()
        embeddings.embed_query = MagicMock(return_value=[1.0, 0.0])
        service = _make_service(
            repo, entity_store, MagicMock(), embeddings=embeddings,
        )
        candidates, by_id = service.build_known_entity_candidates(
            "any text", user_id=1,
        )
        assert candidates == []
        assert by_id == {}
        # The entry text should not be embedded if there are no
        # candidates to compare against — saves an OpenAI call.
        embeddings.embed_query.assert_not_called()

    def test_returns_empty_when_user_id_none(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        embeddings = MagicMock()
        service = _make_service(
            repo, entity_store, MagicMock(), embeddings=embeddings,
        )
        candidates, by_id = service.build_known_entity_candidates(
            "text", user_id=None,
        )
        assert candidates == []
        assert by_id == {}
        embeddings.embed_query.assert_not_called()

    def test_candidate_dict_shape(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        sarah = entity_store.create_entity(
            "person", "Sarah", "my mother", "2026-01-01",
        )
        entity_store.add_alias(sarah.id, "Mum")
        entity_store.set_entity_embedding(sarah.id, [1.0, 0.0])
        embeddings = MagicMock()
        embeddings.embed_query = MagicMock(return_value=[1.0, 0.0])
        service = _make_service(
            repo, entity_store, MagicMock(), embeddings=embeddings,
        )
        candidates, _ = service.build_known_entity_candidates(
            "I called Mum", user_id=1,
        )
        assert len(candidates) == 1
        c = candidates[0]
        assert c["id"] == sarah.id
        assert c["canonical_name"] == "Sarah"
        assert c["entity_type"] == "person"
        assert "mum" in c["aliases"]
        assert c["description"] == "my mother"


class TestExtractFromEntryPassesKnownEntities:
    """End-to-end check: extract_from_entry calls the candidate
    builder and forwards the result to the extraction provider.
    """

    def test_known_entities_forwarded_to_extractor(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        sarah = entity_store.create_entity(
            "person", "Sarah", "my mother", "2026-01-01", user_id=1,
        )
        entity_store.set_entity_embedding(sarah.id, [1.0, 0.0])
        entry = repo.create_entry(
            "2026-01-02", "photo", "I called Mum today.", 5, user_id=1,
        )

        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw()
        embeddings = MagicMock()
        embeddings.embed_query = MagicMock(return_value=[1.0, 0.0])

        service = _make_service(
            repo, entity_store, extractor, embeddings=embeddings,
        )
        service.extract_from_entry(entry.id)

        kwargs = extractor.extract_entities.call_args.kwargs
        known = kwargs["known_entities"]
        assert known is not None and len(known) == 1
        assert known[0]["id"] == sarah.id

    def test_no_known_entities_passed_when_user_has_none(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        entry = repo.create_entry(
            "2026-01-02", "photo", "fresh entry", 2, user_id=1,
        )
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw()
        service = _make_service(repo, entity_store, extractor)

        service.extract_from_entry(entry.id)
        kwargs = extractor.extract_entities.call_args.kwargs
        assert kwargs["known_entities"] is None


class TestLLMAssertedMatch:
    """WU4-D: stage-0 LLM-asserted match with the four-guard hybrid
    sanity check.
    """

    def _setup(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
        *,
        candidate_embedding: list[float] | None = None,
        new_embedding: list[float] | None = None,
        threshold: float = 0.3,
    ) -> tuple[object, object, EntityExtractionService]:
        """Common setup: create Sarah (known) with an embedding, an
        entry mentioning "Mum", and a service primed with one
        candidate.
        """
        sarah = entity_store.create_entity(
            "person", "Sarah", "my mother", "2026-01-01", user_id=1,
        )
        if candidate_embedding is not None:
            entity_store.set_entity_embedding(sarah.id, candidate_embedding)
        entry = repo.create_entry(
            "2026-01-02", "photo", "I called Mum.", 3, user_id=1,
        )
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw(
            entities=[
                _entity(
                    "Mum", "person",
                    description="my mother",
                    quote="I called Mum.",
                ),
            ],
        )
        # Inject matches_known_id post-hoc since the helper doesn't
        # accept it.
        extractor.extract_entities.return_value.entities[0]["matches_known_id"] = sarah.id
        extractor.extract_entities.return_value.entities[0]["match_justification"] = (
            "description says my mother"
        )
        embeddings = MagicMock()
        embeddings.embed_query = MagicMock(
            return_value=new_embedding or [1.0, 0.0],
        )
        service = _make_service(
            repo, entity_store, extractor,
            embeddings=embeddings,
            llm_match_min_cosine=threshold,
        )
        return sarah, entry, service

    def test_match_accepted_when_all_four_guards_pass(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        sarah, entry, service = self._setup(
            repo, entity_store,
            candidate_embedding=[1.0, 0.0],
            new_embedding=[1.0, 0.0],
        )
        service.extract_from_entry(entry.id)

        # The new "Mum" extraction should resolve to Sarah, not create
        # a duplicate. And the mention should be tagged llm_asserted.
        mentions = entity_store.get_mentions_for_entry(entry.id)
        assert len(mentions) == 1
        assert mentions[0].entity_id == sarah.id
        assert mentions[0].match_source == "llm_asserted"
        # No duplicate "Mum" entity should exist as a person. (Sarah
        # may be soft-quarantined by the post-extraction sanity sweep
        # because the literal string "Sarah" doesn't appear in the
        # entry quote — that's expected and orthogonal to this test.)
        all_persons = entity_store.list_entities(
            entity_type="person", user_id=1, include_quarantined=True,
        )
        assert len(all_persons) == 1
        assert all_persons[0].id == sarah.id

    def test_guard_a_rejects_unknown_id(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        # LLM hallucinates an id that doesn't exist. Guard A should
        # reject and fall through to a/b/c (which will also miss),
        # creating a brand-new entity.
        entry = repo.create_entry(
            "2026-01-02", "photo", "I called Mum.", 3, user_id=1,
        )
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw(
            entities=[_entity("Mum", "person", quote="I called Mum.")],
        )
        extractor.extract_entities.return_value.entities[0]["matches_known_id"] = 9999
        service = _make_service(repo, entity_store, extractor)
        service.extract_from_entry(entry.id)

        mentions = entity_store.get_mentions_for_entry(entry.id)
        assert len(mentions) == 1
        # Created a new entity (no llm_asserted, no stage_a/b/c match).
        assert mentions[0].match_source is None

    def test_guard_b_rejects_id_outside_candidate_set(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        # Sarah exists but has NO embedding, so she won't be in the
        # candidate set passed to the LLM. If the LLM still asserts
        # her id, guard B rejects.
        sarah = entity_store.create_entity(
            "person", "Sarah", "my mother", "2026-01-01", user_id=1,
        )
        # No set_entity_embedding — Sarah is not a candidate.
        entry = repo.create_entry(
            "2026-01-02", "photo", "I called Mum.", 3, user_id=1,
        )
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw(
            entities=[_entity("Mum", "person", quote="I called Mum.")],
        )
        extractor.extract_entities.return_value.entities[0]["matches_known_id"] = sarah.id
        service = _make_service(repo, entity_store, extractor)
        service.extract_from_entry(entry.id)

        mentions = entity_store.get_mentions_for_entry(entry.id)
        # Stage-a triggered? No, "Mum" != "Sarah". Created new entity.
        assert mentions[0].match_source is None
        assert mentions[0].entity_id != sarah.id

    def test_guard_c_rejects_type_mismatch(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        # A topic entity is asserted as a person mention. Guard C
        # rejects.
        topic = entity_store.create_entity(
            "topic", "Sarah Project", "old", "2026-01-01", user_id=1,
        )
        entity_store.set_entity_embedding(topic.id, [1.0, 0.0])
        entry = repo.create_entry(
            "2026-01-02", "photo", "I called Mum.", 3, user_id=1,
        )
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw(
            entities=[_entity("Mum", "person", quote="I called Mum.")],
        )
        extractor.extract_entities.return_value.entities[0]["matches_known_id"] = topic.id
        embeddings = MagicMock()
        embeddings.embed_query = MagicMock(return_value=[1.0, 0.0])
        service = _make_service(repo, entity_store, extractor, embeddings=embeddings)
        service.extract_from_entry(entry.id)

        mentions = entity_store.get_mentions_for_entry(entry.id)
        assert mentions[0].entity_id != topic.id
        assert mentions[0].match_source is None

    def test_guard_d_rejects_low_cosine(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        # Cosine of orthogonal vectors is 0.0 → below default 0.3.
        # Guard D rejects, so the LLM-asserted match is discarded.
        sarah, entry, service = self._setup(
            repo, entity_store,
            candidate_embedding=[1.0, 0.0],
            new_embedding=[0.0, 1.0],
            threshold=0.3,
        )
        service.extract_from_entry(entry.id)

        mentions = entity_store.get_mentions_for_entry(entry.id)
        # Sarah was a candidate by virtue of embedding presence, but
        # the cosine guard rejected the match. Stage a/b/c didn't fire
        # either ("Mum" doesn't match "Sarah" and stage-c uses a
        # different threshold). New entity created.
        assert mentions[0].match_source is None
        assert mentions[0].entity_id != sarah.id

    def test_no_matches_known_id_runs_normal_resolution(
        self,
        repo: SQLiteEntryRepository,
        entity_store: SQLiteEntityStore,
    ) -> None:
        # When the LLM doesn't set matches_known_id, the extracted
        # entity flows through stage-a/b/c as before. Here stage-a
        # fires because the canonical exactly matches.
        sarah = entity_store.create_entity(
            "person", "Sarah", "", "2026-01-01", user_id=1,
        )
        entry = repo.create_entry(
            "2026-01-02", "photo", "I saw Sarah.", 3, user_id=1,
        )
        extractor = MagicMock()
        extractor.extract_entities.return_value = _raw(
            entities=[_entity("Sarah", "person", quote="I saw Sarah.")],
        )
        # No matches_known_id field on the dict.
        service = _make_service(repo, entity_store, extractor)
        service.extract_from_entry(entry.id)

        mentions = entity_store.get_mentions_for_entry(entry.id)
        assert mentions[0].entity_id == sarah.id
        assert mentions[0].match_source == "stage_a"
