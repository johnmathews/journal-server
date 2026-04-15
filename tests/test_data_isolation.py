"""Integration tests for per-user data isolation.

Verifies that user 1 cannot see, modify, or delete user 2's data
across all repository layers: entries, entities, and jobs.
"""

import sqlite3

import pytest

from journal.db.connection import get_connection
from journal.db.jobs_repository import SQLiteJobRepository
from journal.db.migrations import run_migrations
from journal.db.repository import SQLiteEntryRepository
from journal.entitystore.store import SQLiteEntityStore

# ---- Fixtures ---------------------------------------------------------------

USER_1 = 1  # admin, created by migration
USER_2 = 2  # second user, inserted in fixture


@pytest.fixture
def conn(tmp_path: pytest.TempPathFactory) -> sqlite3.Connection:
    """Migrated DB with two users."""
    db_path = tmp_path / "isolation.db"  # type: ignore[operator]
    connection = get_connection(db_path)
    run_migrations(connection)
    # Migration seeds user 1 (admin). Insert a second user.
    connection.execute(
        "INSERT INTO users (email, display_name, is_admin, email_verified) "
        "VALUES ('user2@test.com', 'User Two', 0, 1)"
    )
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def repo(conn: sqlite3.Connection) -> SQLiteEntryRepository:
    return SQLiteEntryRepository(conn)


@pytest.fixture
def entity_store(conn: sqlite3.Connection) -> SQLiteEntityStore:
    return SQLiteEntityStore(conn)


@pytest.fixture
def jobs_repo(conn: sqlite3.Connection) -> SQLiteJobRepository:
    return SQLiteJobRepository(conn)


# ---- Entry Repository Isolation ---------------------------------------------


class TestEntryGetIsolation:
    """get_entry must be scoped by user_id."""

    def test_owner_can_fetch_own_entry(self, repo: SQLiteEntryRepository) -> None:
        entry = repo.create_entry("2026-04-01", "ocr", "User 1 text", 3, user_id=USER_1)
        fetched = repo.get_entry(entry.id, user_id=USER_1)
        assert fetched is not None
        assert fetched.id == entry.id

    def test_other_user_cannot_fetch_entry(self, repo: SQLiteEntryRepository) -> None:
        entry = repo.create_entry("2026-04-01", "ocr", "User 1 text", 3, user_id=USER_1)
        fetched = repo.get_entry(entry.id, user_id=USER_2)
        assert fetched is None

    def test_no_user_id_bypasses_filter(self, repo: SQLiteEntryRepository) -> None:
        entry = repo.create_entry("2026-04-01", "ocr", "User 1 text", 3, user_id=USER_1)
        fetched = repo.get_entry(entry.id, user_id=None)
        assert fetched is not None


class TestEntryListIsolation:
    """list_entries must be scoped by user_id."""

    def test_list_entries_only_shows_own(self, repo: SQLiteEntryRepository) -> None:
        repo.create_entry("2026-04-01", "ocr", "User 1 first", 3, user_id=USER_1)
        repo.create_entry("2026-04-02", "ocr", "User 1 second", 3, user_id=USER_1)
        repo.create_entry("2026-04-01", "ocr", "User 2 first", 3, user_id=USER_2)

        u1_entries = repo.list_entries(user_id=USER_1)
        u2_entries = repo.list_entries(user_id=USER_2)

        assert len(u1_entries) == 2
        assert all(e.user_id == USER_1 for e in u1_entries)
        assert len(u2_entries) == 1
        assert u2_entries[0].user_id == USER_2

    def test_list_entries_no_user_id_returns_all(self, repo: SQLiteEntryRepository) -> None:
        repo.create_entry("2026-04-01", "ocr", "User 1", 2, user_id=USER_1)
        repo.create_entry("2026-04-01", "ocr", "User 2", 2, user_id=USER_2)

        all_entries = repo.list_entries(user_id=None)
        assert len(all_entries) == 2


class TestEntryDateIsolation:
    """get_entries_by_date must be scoped by user_id."""

    def test_entries_by_date_scoped(self, repo: SQLiteEntryRepository) -> None:
        date = "2026-04-10"
        repo.create_entry(date, "ocr", "User 1 diary", 3, user_id=USER_1)
        repo.create_entry(date, "voice", "User 2 diary", 3, user_id=USER_2)

        u1 = repo.get_entries_by_date(date, user_id=USER_1)
        u2 = repo.get_entries_by_date(date, user_id=USER_2)

        assert len(u1) == 1
        assert u1[0].raw_text == "User 1 diary"
        assert len(u2) == 1
        assert u2[0].raw_text == "User 2 diary"


class TestEntrySearchIsolation:
    """FTS5 search must be scoped by user_id."""

    def test_search_text_scoped(self, repo: SQLiteEntryRepository) -> None:
        repo.create_entry("2026-04-01", "ocr", "Vienna trip was great", 5, user_id=USER_1)
        repo.create_entry("2026-04-01", "ocr", "Vienna is beautiful", 3, user_id=USER_2)

        u1_results = repo.search_text("Vienna", user_id=USER_1)
        u2_results = repo.search_text("Vienna", user_id=USER_2)

        assert len(u1_results) == 1
        assert u1_results[0].user_id == USER_1
        assert len(u2_results) == 1
        assert u2_results[0].user_id == USER_2

    def test_search_text_with_snippets_scoped(self, repo: SQLiteEntryRepository) -> None:
        repo.create_entry("2026-04-01", "ocr", "Coffee in Prague morning", 4, user_id=USER_1)
        repo.create_entry("2026-04-01", "ocr", "Coffee in London afternoon", 4, user_id=USER_2)

        u1_results = repo.search_text_with_snippets("Coffee", user_id=USER_1)
        u2_results = repo.search_text_with_snippets("Coffee", user_id=USER_2)

        assert len(u1_results) == 1
        assert u1_results[0][0].user_id == USER_1
        assert len(u2_results) == 1
        assert u2_results[0][0].user_id == USER_2

    def test_count_text_matches_scoped(self, repo: SQLiteEntryRepository) -> None:
        repo.create_entry("2026-04-01", "ocr", "Running in the park", 5, user_id=USER_1)
        repo.create_entry("2026-04-02", "ocr", "Running on the beach", 5, user_id=USER_1)
        repo.create_entry("2026-04-01", "ocr", "Running errands today", 3, user_id=USER_2)

        assert repo.count_text_matches("Running", user_id=USER_1) == 2
        assert repo.count_text_matches("Running", user_id=USER_2) == 1


class TestEntryStatisticsIsolation:
    """get_statistics and count_entries must be scoped by user_id."""

    def test_statistics_scoped(self, repo: SQLiteEntryRepository) -> None:
        repo.create_entry("2026-04-01", "ocr", "Short entry", 2, user_id=USER_1)
        repo.create_entry("2026-04-02", "ocr", "Another entry here", 3, user_id=USER_1)
        repo.create_entry("2026-04-01", "voice", "Voice note from user two", 5, user_id=USER_2)

        stats_1 = repo.get_statistics(user_id=USER_1)
        stats_2 = repo.get_statistics(user_id=USER_2)

        assert stats_1.total_entries == 2
        assert stats_1.total_words == 5  # 2 + 3
        assert stats_2.total_entries == 1
        assert stats_2.total_words == 5

    def test_count_entries_scoped(self, repo: SQLiteEntryRepository) -> None:
        repo.create_entry("2026-04-01", "ocr", "One", 1, user_id=USER_1)
        repo.create_entry("2026-04-02", "ocr", "Two", 1, user_id=USER_1)
        repo.create_entry("2026-04-03", "ocr", "Three", 1, user_id=USER_1)
        repo.create_entry("2026-04-01", "ocr", "Uno", 1, user_id=USER_2)

        assert repo.count_entries(user_id=USER_1) == 3
        assert repo.count_entries(user_id=USER_2) == 1


class TestEntryMutationIsolation:
    """delete_entry, update_final_text, verify_doubts must not cross user boundaries."""

    def test_delete_entry_blocked_for_other_user(self, repo: SQLiteEntryRepository) -> None:
        entry = repo.create_entry("2026-04-01", "ocr", "Private data", 2, user_id=USER_1)
        deleted = repo.delete_entry(entry.id, user_id=USER_2)
        assert deleted is False
        # Entry must still exist for user 1
        assert repo.get_entry(entry.id, user_id=USER_1) is not None

    def test_delete_entry_succeeds_for_owner(self, repo: SQLiteEntryRepository) -> None:
        entry = repo.create_entry("2026-04-01", "ocr", "Delete me", 2, user_id=USER_1)
        deleted = repo.delete_entry(entry.id, user_id=USER_1)
        assert deleted is True
        assert repo.get_entry(entry.id) is None

    def test_update_final_text_blocked_for_other_user(
        self, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-04-01", "ocr", "Original text", 2, user_id=USER_1)
        result = repo.update_final_text(entry.id, "Hacked text", 2, 0, user_id=USER_2)
        assert result is None
        # Original text must remain intact
        original = repo.get_entry(entry.id, user_id=USER_1)
        assert original is not None
        assert original.final_text == "Original text"

    def test_update_final_text_succeeds_for_owner(
        self, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-04-01", "ocr", "Original text", 2, user_id=USER_1)
        result = repo.update_final_text(entry.id, "Corrected text", 2, 1, user_id=USER_1)
        assert result is not None
        assert result.final_text == "Corrected text"

    def test_verify_doubts_blocked_for_other_user(
        self, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-04-01", "ocr", "Doubtful text", 2, user_id=USER_1)
        result = repo.verify_doubts(entry.id, user_id=USER_2)
        assert result is False
        # doubts_verified must remain False for the entry
        fetched = repo.get_entry(entry.id, user_id=USER_1)
        assert fetched is not None
        assert fetched.doubts_verified is False

    def test_verify_doubts_succeeds_for_owner(
        self, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-04-01", "ocr", "Doubtful text", 2, user_id=USER_1)
        result = repo.verify_doubts(entry.id, user_id=USER_1)
        assert result is True
        fetched = repo.get_entry(entry.id, user_id=USER_1)
        assert fetched is not None
        assert fetched.doubts_verified is True


# ---- Entity Store Isolation -------------------------------------------------


class TestEntityGetIsolation:
    """get_entity and get_entity_by_name must be scoped by user_id."""

    def test_owner_can_fetch_own_entity(self, entity_store: SQLiteEntityStore) -> None:
        entity = entity_store.create_entity(
            "person", "Atlas", "a dog", "2026-04-01", user_id=USER_1
        )
        fetched = entity_store.get_entity(entity.id, user_id=USER_1)
        assert fetched is not None
        assert fetched.id == entity.id

    def test_other_user_cannot_fetch_entity(self, entity_store: SQLiteEntityStore) -> None:
        entity = entity_store.create_entity(
            "person", "Atlas", "a dog", "2026-04-01", user_id=USER_1
        )
        fetched = entity_store.get_entity(entity.id, user_id=USER_2)
        assert fetched is None

    def test_get_entity_by_name_scoped(self, entity_store: SQLiteEntityStore) -> None:
        entity_store.create_entity("person", "Alice", "user 1 friend", "2026-04-01", user_id=USER_1)
        entity_store.create_entity("person", "Alice", "user 2 friend", "2026-04-01", user_id=USER_2)

        u1_alice = entity_store.get_entity_by_name("Alice", "person", user_id=USER_1)
        u2_alice = entity_store.get_entity_by_name("Alice", "person", user_id=USER_2)

        assert u1_alice is not None
        assert u2_alice is not None
        assert u1_alice.id != u2_alice.id
        assert u1_alice.description == "user 1 friend"
        assert u2_alice.description == "user 2 friend"

    def test_get_entity_by_name_invisible_to_other_user(
        self, entity_store: SQLiteEntityStore
    ) -> None:
        entity_store.create_entity("place", "Vienna", "city", "2026-04-01", user_id=USER_1)
        fetched = entity_store.get_entity_by_name("Vienna", "place", user_id=USER_2)
        assert fetched is None


class TestEntityListIsolation:
    """list_entities and count_entities must be scoped by user_id."""

    def test_list_entities_scoped(self, entity_store: SQLiteEntityStore) -> None:
        entity_store.create_entity("person", "Atlas", "dog", "2026-04-01", user_id=USER_1)
        entity_store.create_entity("person", "Bruno", "cat", "2026-04-01", user_id=USER_1)
        entity_store.create_entity("person", "Charlie", "bird", "2026-04-01", user_id=USER_2)

        u1_entities = entity_store.list_entities(user_id=USER_1)
        u2_entities = entity_store.list_entities(user_id=USER_2)

        assert len(u1_entities) == 2
        assert all(e.user_id == USER_1 for e in u1_entities)
        assert len(u2_entities) == 1
        assert u2_entities[0].canonical_name == "Charlie"

    def test_count_entities_scoped(self, entity_store: SQLiteEntityStore) -> None:
        entity_store.create_entity("person", "Atlas", "", "2026-04-01", user_id=USER_1)
        entity_store.create_entity("place", "Vienna", "", "2026-04-01", user_id=USER_1)
        entity_store.create_entity("person", "Bob", "", "2026-04-01", user_id=USER_2)

        assert entity_store.count_entities(user_id=USER_1) == 2
        assert entity_store.count_entities(user_id=USER_2) == 1
        assert entity_store.count_entities(entity_type="person", user_id=USER_1) == 1

    def test_list_entities_no_user_id_returns_all(
        self, entity_store: SQLiteEntityStore
    ) -> None:
        entity_store.create_entity("person", "Atlas", "", "2026-04-01", user_id=USER_1)
        entity_store.create_entity("person", "Bob", "", "2026-04-01", user_id=USER_2)

        all_entities = entity_store.list_entities(user_id=None)
        assert len(all_entities) == 2


class TestEntityMutationIsolation:
    """delete_entity and update_entity must not cross user boundaries."""

    def test_delete_entity_blocked_for_other_user(
        self, entity_store: SQLiteEntityStore
    ) -> None:
        entity = entity_store.create_entity(
            "person", "Atlas", "a dog", "2026-04-01", user_id=USER_1
        )
        with pytest.raises(ValueError, match="not found"):
            entity_store.delete_entity(entity.id, user_id=USER_2)
        # Entity must still exist for user 1
        assert entity_store.get_entity(entity.id, user_id=USER_1) is not None

    def test_delete_entity_succeeds_for_owner(
        self, entity_store: SQLiteEntityStore
    ) -> None:
        entity = entity_store.create_entity(
            "person", "Atlas", "a dog", "2026-04-01", user_id=USER_1
        )
        entity_store.delete_entity(entity.id, user_id=USER_1)
        assert entity_store.get_entity(entity.id) is None

    def test_update_entity_blocked_for_other_user(
        self, entity_store: SQLiteEntityStore
    ) -> None:
        entity = entity_store.create_entity(
            "person", "Atlas", "a dog", "2026-04-01", user_id=USER_1
        )
        with pytest.raises(ValueError, match="not found"):
            entity_store.update_entity(
                entity.id, description="hacked description", user_id=USER_2
            )
        # Description must remain unchanged
        original = entity_store.get_entity(entity.id, user_id=USER_1)
        assert original is not None
        assert original.description == "a dog"

    def test_update_entity_succeeds_for_owner(
        self, entity_store: SQLiteEntityStore
    ) -> None:
        entity = entity_store.create_entity(
            "person", "Atlas", "a dog", "2026-04-01", user_id=USER_1
        )
        updated = entity_store.update_entity(
            entity.id, description="a very good dog", user_id=USER_1
        )
        assert updated.description == "a very good dog"


# ---- Job Repository Isolation -----------------------------------------------


class TestJobListIsolation:
    """list_jobs must be scoped by user_id."""

    def test_list_jobs_only_shows_own(self, jobs_repo: SQLiteJobRepository) -> None:
        jobs_repo.create("entity_extraction", {"entry_ids": [1]}, user_id=USER_1)
        jobs_repo.create("mood_backfill", {"entry_ids": [2]}, user_id=USER_1)
        jobs_repo.create("entity_extraction", {"entry_ids": [3]}, user_id=USER_2)

        u1_jobs, u1_total = jobs_repo.list_jobs(user_id=USER_1)
        u2_jobs, u2_total = jobs_repo.list_jobs(user_id=USER_2)

        assert u1_total == 2
        assert len(u1_jobs) == 2
        assert all(j.user_id == USER_1 for j in u1_jobs)

        assert u2_total == 1
        assert len(u2_jobs) == 1
        assert u2_jobs[0].user_id == USER_2

    def test_list_jobs_empty_for_user_with_no_jobs(
        self, jobs_repo: SQLiteJobRepository
    ) -> None:
        jobs_repo.create("entity_extraction", {"entry_ids": [1]}, user_id=USER_1)

        u2_jobs, u2_total = jobs_repo.list_jobs(user_id=USER_2)
        assert u2_total == 0
        assert len(u2_jobs) == 0

    def test_list_jobs_no_user_id_returns_all(
        self, jobs_repo: SQLiteJobRepository
    ) -> None:
        jobs_repo.create("entity_extraction", {}, user_id=USER_1)
        jobs_repo.create("mood_backfill", {}, user_id=USER_2)

        all_jobs, total = jobs_repo.list_jobs(user_id=None)
        assert total == 2
        assert len(all_jobs) == 2


class TestJobGetIsolation:
    """get(job_id, user_id) must be scoped by user_id."""

    def test_owner_can_fetch_own_job(self, jobs_repo: SQLiteJobRepository) -> None:
        job = jobs_repo.create("entity_extraction", {}, user_id=USER_1)
        fetched = jobs_repo.get(job.id, user_id=USER_1)
        assert fetched is not None
        assert fetched.id == job.id

    def test_other_user_cannot_fetch_job(self, jobs_repo: SQLiteJobRepository) -> None:
        job = jobs_repo.create("entity_extraction", {}, user_id=USER_1)
        fetched = jobs_repo.get(job.id, user_id=USER_2)
        assert fetched is None

    def test_get_no_user_id_bypasses_filter(
        self, jobs_repo: SQLiteJobRepository
    ) -> None:
        job = jobs_repo.create("entity_extraction", {}, user_id=USER_1)
        fetched = jobs_repo.get(job.id, user_id=None)
        assert fetched is not None


class TestJobFilterIsolation:
    """list_jobs with status/type filters must still respect user_id."""

    def test_list_jobs_with_status_filter_scoped(
        self, jobs_repo: SQLiteJobRepository
    ) -> None:
        job1 = jobs_repo.create("entity_extraction", {}, user_id=USER_1)
        jobs_repo.create("entity_extraction", {}, user_id=USER_2)

        # Move user 1's job to running
        jobs_repo.mark_running(job1.id)

        running_u1, total = jobs_repo.list_jobs(status="running", user_id=USER_1)
        assert total == 1
        assert running_u1[0].id == job1.id

        running_u2, total = jobs_repo.list_jobs(status="running", user_id=USER_2)
        assert total == 0

    def test_list_jobs_with_type_filter_scoped(
        self, jobs_repo: SQLiteJobRepository
    ) -> None:
        jobs_repo.create("entity_extraction", {}, user_id=USER_1)
        jobs_repo.create("mood_backfill", {}, user_id=USER_1)
        jobs_repo.create("entity_extraction", {}, user_id=USER_2)

        ee_u1, total = jobs_repo.list_jobs(job_type="entity_extraction", user_id=USER_1)
        assert total == 1
        assert len(ee_u1) == 1

        ee_u2, total = jobs_repo.list_jobs(job_type="entity_extraction", user_id=USER_2)
        assert total == 1
        assert len(ee_u2) == 1


# ---- Cross-cutting: ensure user_id is stored correctly ----------------------


class TestUserIdPersistence:
    """Verify that user_id is round-tripped correctly through creation and read."""

    def test_entry_user_id_persisted(self, repo: SQLiteEntryRepository) -> None:
        e1 = repo.create_entry("2026-04-01", "ocr", "Text", 1, user_id=USER_1)
        e2 = repo.create_entry("2026-04-01", "ocr", "Text", 1, user_id=USER_2)
        assert e1.user_id == USER_1
        assert e2.user_id == USER_2

    def test_entity_user_id_persisted(self, entity_store: SQLiteEntityStore) -> None:
        ent1 = entity_store.create_entity("person", "A", "", "2026-04-01", user_id=USER_1)
        ent2 = entity_store.create_entity("person", "B", "", "2026-04-01", user_id=USER_2)
        assert ent1.user_id == USER_1
        assert ent2.user_id == USER_2

    def test_job_user_id_persisted(self, jobs_repo: SQLiteJobRepository) -> None:
        j1 = jobs_repo.create("test_type", {}, user_id=USER_1)
        j2 = jobs_repo.create("test_type", {}, user_id=USER_2)
        assert j1.user_id == USER_1
        assert j2.user_id == USER_2
