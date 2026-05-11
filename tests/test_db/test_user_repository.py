"""Tests for SQLiteUserRepository."""

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from journal.db.factory import ConnectionFactory
from journal.db.migrations import run_migrations
from journal.db.user_repository import SQLiteUserRepository
from journal.models import ApiKeyInfo, User


@pytest.fixture
def user_repo(db_conn: sqlite3.Connection) -> SQLiteUserRepository:
    return SQLiteUserRepository(db_conn)


@pytest.fixture
def user_factory(tmp_path: Path) -> ConnectionFactory:
    """ConnectionFactory pointing at a migrated temp DB."""
    factory = ConnectionFactory(tmp_path / "users.db")
    run_migrations(factory.get())
    return factory


@pytest.fixture
def user_repo_via_factory(user_factory: ConnectionFactory) -> SQLiteUserRepository:
    return SQLiteUserRepository(user_factory)


# ── Users ───────────────────────────────────────────────────────────


class TestCreateUser:
    def test_create_returns_user(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("alice@example.com", "Alice")
        assert isinstance(user, User)
        assert user.email == "alice@example.com"
        assert user.display_name == "Alice"
        assert user.is_admin is False
        assert user.is_active is True
        assert user.email_verified is False
        assert user.created_at != ""
        assert user.updated_at != ""

    def test_create_with_password_hash(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("bob@example.com", "Bob", password_hash="hashed123")
        assert user.email == "bob@example.com"
        # Password hash is not exposed on the User model
        pw = user_repo.get_password_hash(user.id)
        assert pw == "hashed123"

    def test_create_admin_user(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("admin@example.com", "Admin", is_admin=True)
        assert user.is_admin is True

    def test_create_duplicate_email_raises(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        user_repo.create_user("dup@example.com", "First")
        with pytest.raises(sqlite3.IntegrityError):
            user_repo.create_user("dup@example.com", "Second")

    def test_email_uniqueness_is_case_insensitive(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        user_repo.create_user("case@example.com", "Lower")
        with pytest.raises(sqlite3.IntegrityError):
            user_repo.create_user("CASE@example.com", "Upper")


class TestGetUser:
    def test_get_by_id(self, user_repo: SQLiteUserRepository) -> None:
        created = user_repo.create_user("get@example.com", "Get")
        fetched = user_repo.get_user_by_id(created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.email == "get@example.com"

    def test_get_by_id_missing_returns_none(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        assert user_repo.get_user_by_id(99999) is None

    def test_get_by_email(self, user_repo: SQLiteUserRepository) -> None:
        user_repo.create_user("email@example.com", "Email")
        fetched = user_repo.get_user_by_email("email@example.com")
        assert fetched is not None
        assert fetched.email == "email@example.com"

    def test_get_by_email_missing_returns_none(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        assert user_repo.get_user_by_email("nope@example.com") is None


class TestGetPasswordHash:
    def test_returns_hash(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("pw@example.com", "PW", password_hash="secret-hash")
        assert user_repo.get_password_hash(user.id) == "secret-hash"

    def test_returns_none_when_no_password(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        user = user_repo.create_user("nopw@example.com", "NoPW")
        assert user_repo.get_password_hash(user.id) is None

    def test_returns_none_for_missing_user(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        assert user_repo.get_password_hash(99999) is None


class TestUpdateUser:
    def test_update_display_name(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("upd@example.com", "Old")
        updated = user_repo.update_user(user.id, display_name="New")
        assert updated is not None
        assert updated.display_name == "New"

    def test_update_multiple_fields(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("multi@example.com", "Multi")
        updated = user_repo.update_user(
            user.id, display_name="Updated", is_admin=True, email_verified=True
        )
        assert updated is not None
        assert updated.display_name == "Updated"
        assert updated.is_admin is True
        assert updated.email_verified is True

    def test_update_missing_user_returns_none(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        assert user_repo.update_user(99999, display_name="X") is None

    def test_update_invalid_field_raises(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        user = user_repo.create_user("inv@example.com", "Inv")
        with pytest.raises(ValueError, match="Cannot update fields"):
            user_repo.update_user(user.id, bogus_field="bad")

    def test_update_no_fields_returns_user(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        user = user_repo.create_user("noop@example.com", "Noop")
        result = user_repo.update_user(user.id)
        assert result is not None
        assert result.id == user.id


class TestListUsers:
    def test_list_includes_seeded_admin(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        # Migration 0011 seeds an admin user
        users = user_repo.list_users()
        assert len(users) >= 1
        emails = [u.email for u in users]
        assert "admin@journal.local" in emails

    def test_list_includes_created_users(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        user_repo.create_user("list1@example.com", "One")
        user_repo.create_user("list2@example.com", "Two")
        users = user_repo.list_users()
        emails = [u.email for u in users]
        assert "list1@example.com" in emails
        assert "list2@example.com" in emails


class TestFailedLogins:
    def test_increment_and_reset(
        self, user_repo: SQLiteUserRepository, db_conn: sqlite3.Connection
    ) -> None:
        user = user_repo.create_user("fail@example.com", "Fail")
        user_repo.increment_failed_logins(user.id)
        user_repo.increment_failed_logins(user.id)
        row = db_conn.execute(
            "SELECT failed_login_attempts FROM users WHERE id = ?", (user.id,)
        ).fetchone()
        assert row["failed_login_attempts"] == 2

        user_repo.reset_failed_logins(user.id)
        row = db_conn.execute(
            "SELECT failed_login_attempts FROM users WHERE id = ?", (user.id,)
        ).fetchone()
        assert row["failed_login_attempts"] == 0


class TestLockUser:
    def test_lock_and_check(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("lock@example.com", "Lock")
        assert user_repo.get_lock_status(user.id) is None

        # lock_user is conditional — only applies when failed_login_attempts >= 5
        for _ in range(5):
            user_repo.increment_failed_logins(user.id)

        future = (datetime.now(UTC) + timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        user_repo.lock_user(user.id, future)
        assert user_repo.get_lock_status(user.id) == future

    def test_lock_status_missing_user(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        assert user_repo.get_lock_status(99999) is None


# ── Sessions ────────────────────────────────────────────────────────


class TestSessions:
    def test_create_and_get_session(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        user = user_repo.create_user("sess@example.com", "Sess")
        expires = (datetime.now(UTC) + timedelta(days=7)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        user_repo.create_session("tok-123", user.id, expires, "TestAgent", "127.0.0.1")
        session = user_repo.get_session("tok-123")
        assert session is not None
        assert session["user_id"] == user.id
        assert session["email"] == "sess@example.com"
        assert session["display_name"] == "Sess"
        assert session["user_agent"] == "TestAgent"
        assert session["ip_address"] == "127.0.0.1"

    def test_get_expired_session_returns_none(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        user = user_repo.create_user("exp@example.com", "Exp")
        past = (datetime.now(UTC) - timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        user_repo.create_session("tok-expired", user.id, past)
        assert user_repo.get_session("tok-expired") is None

    def test_get_missing_session_returns_none(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        assert user_repo.get_session("does-not-exist") is None

    def test_update_last_seen(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("seen@example.com", "Seen")
        expires = (datetime.now(UTC) + timedelta(days=7)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        user_repo.create_session("tok-seen", user.id, expires)
        session_before = user_repo.get_session("tok-seen")
        assert session_before is not None
        user_repo.update_session_last_seen("tok-seen")
        session_after = user_repo.get_session("tok-seen")
        assert session_after is not None
        # last_seen_at should be updated (or at least not earlier)
        assert session_after["last_seen_at"] >= session_before["last_seen_at"]

    def test_delete_session(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("del@example.com", "Del")
        expires = (datetime.now(UTC) + timedelta(days=7)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        user_repo.create_session("tok-del", user.id, expires)
        user_repo.delete_session("tok-del")
        assert user_repo.get_session("tok-del") is None

    def test_delete_user_sessions(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        user = user_repo.create_user("delall@example.com", "DelAll")
        expires = (datetime.now(UTC) + timedelta(days=7)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        user_repo.create_session("tok-a", user.id, expires)
        user_repo.create_session("tok-b", user.id, expires)
        count = user_repo.delete_user_sessions(user.id)
        assert count == 2
        assert user_repo.get_session("tok-a") is None
        assert user_repo.get_session("tok-b") is None

    def test_cleanup_expired_sessions(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        user = user_repo.create_user("clean@example.com", "Clean")
        past = (datetime.now(UTC) - timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        future = (datetime.now(UTC) + timedelta(days=7)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        user_repo.create_session("tok-old", user.id, past)
        user_repo.create_session("tok-new", user.id, future)
        count = user_repo.cleanup_expired_sessions()
        assert count >= 1
        # The valid session survives
        assert user_repo.get_session("tok-new") is not None


# ── API Keys ────────────────────────────────────────────────────────


class TestApiKeys:
    def test_create_and_get_by_hash(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        user = user_repo.create_user("key@example.com", "Key")
        key_hash = hashlib.sha256(b"jnl_test_key").hexdigest()
        key_id = user_repo.create_api_key(user.id, "jnl_test_", key_hash, "My Key")
        assert key_id > 0

        result = user_repo.get_api_key_by_hash(key_hash)
        assert result is not None
        assert result["key_id"] == key_id
        assert result["user_id"] == user.id
        assert result["email"] == "key@example.com"
        assert result["name"] == "My Key"
        assert result["key_prefix"] == "jnl_test_"

    def test_get_revoked_key_returns_none(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        user = user_repo.create_user("revkey@example.com", "Rev")
        key_hash = hashlib.sha256(b"jnl_revoked").hexdigest()
        key_id = user_repo.create_api_key(user.id, "jnl_revok", key_hash, "Revoked")
        user_repo.revoke_api_key(key_id, user.id)
        assert user_repo.get_api_key_by_hash(key_hash) is None

    def test_get_expired_key_returns_none(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        user = user_repo.create_user("expkey@example.com", "Exp")
        key_hash = hashlib.sha256(b"jnl_expired").hexdigest()
        past = (datetime.now(UTC) - timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        user_repo.create_api_key(user.id, "jnl_expir", key_hash, "Expired", expires_at=past)
        assert user_repo.get_api_key_by_hash(key_hash) is None

    def test_get_missing_key_returns_none(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        assert user_repo.get_api_key_by_hash("nonexistent") is None

    def test_list_api_keys(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("listk@example.com", "ListK")
        h1 = hashlib.sha256(b"jnl_key_one").hexdigest()
        h2 = hashlib.sha256(b"jnl_key_two").hexdigest()
        user_repo.create_api_key(user.id, "jnl_key_on", h1, "Key One")
        user_repo.create_api_key(user.id, "jnl_key_tw", h2, "Key Two")

        keys = user_repo.list_api_keys(user.id)
        assert len(keys) == 2
        assert all(isinstance(k, ApiKeyInfo) for k in keys)
        names = {k.name for k in keys}
        assert names == {"Key One", "Key Two"}

    def test_revoke_api_key(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("rev2@example.com", "Rev2")
        key_hash = hashlib.sha256(b"jnl_to_revoke").hexdigest()
        key_id = user_repo.create_api_key(user.id, "jnl_to_re", key_hash, "Revocable")
        assert user_repo.revoke_api_key(key_id, user.id) is True
        # Second revoke returns False (already revoked)
        assert user_repo.revoke_api_key(key_id, user.id) is False

    def test_revoke_wrong_user_returns_false(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        user1 = user_repo.create_user("u1@example.com", "U1")
        user2 = user_repo.create_user("u2@example.com", "U2")
        key_hash = hashlib.sha256(b"jnl_u1_key").hexdigest()
        key_id = user_repo.create_api_key(user1.id, "jnl_u1_ke", key_hash, "U1 Key")
        # User2 cannot revoke User1's key
        assert user_repo.revoke_api_key(key_id, user2.id) is False

    def test_update_last_used(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("used@example.com", "Used")
        key_hash = hashlib.sha256(b"jnl_used_key").hexdigest()
        key_id = user_repo.create_api_key(user.id, "jnl_used_", key_hash, "Used Key")
        user_repo.update_api_key_last_used(key_id)
        keys = user_repo.list_api_keys(user.id)
        assert keys[0].last_used_at is not None


# ── Admin queries ───────────────────────────────────────────────────


class TestUserStats:
    def test_get_user_stats(self, user_repo: SQLiteUserRepository) -> None:
        stats = user_repo.get_user_stats()
        # At minimum, the seeded admin user should appear
        assert len(stats) >= 1
        admin_stat = next(s for s in stats if s["email"] == "admin@journal.local")
        assert "entry_count" in admin_stat
        assert "total_words" in admin_stat
        assert "job_count" in admin_stat


class TestFactoryPathSemantics:
    """Production-path coverage for the ``ConnectionFactory`` model.

    The classes above exercise the bare-``Connection`` legacy path
    (kept until W4 of ``docs/sqlite-per-thread-connections-plan.md``
    retires the dual-constructor). These tests cover the factory path
    that production now uses: each thread owns its own connection,
    so the cross-thread implicit-transaction collision documented in
    ``docs/sqlite-threading.md`` becomes structurally impossible.
    """

    def test_lifecycle_round_trip(
        self, user_repo_via_factory: SQLiteUserRepository,
    ) -> None:
        user = user_repo_via_factory.create_user(
            "factory@example.com", "Factory", password_hash="hash",
        )
        fetched = user_repo_via_factory.get_user_by_email("factory@example.com")
        assert fetched is not None
        assert fetched.id == user.id
        assert fetched.display_name == "Factory"

    def test_each_thread_gets_distinct_connection(
        self, user_repo_via_factory: SQLiteUserRepository,
    ) -> None:
        main_conn_id = id(user_repo_via_factory.connection)
        captured: list[int] = []

        def worker() -> None:
            captured.append(id(user_repo_via_factory.connection))

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert len(captured) == 1
        assert captured[0] != main_conn_id

    def test_concurrent_user_writes_under_load(
        self, user_repo_via_factory: SQLiteUserRepository,
    ) -> None:
        """Many threads each creating users + updates. Under the old
        shared-``Connection`` model this would surface
        ``no transaction is active`` from a concurrent commit; under
        the factory model it must complete cleanly.
        """
        thread_count = 6
        users_per_thread = 5
        errors: list[BaseException] = []

        def worker(prefix: str) -> None:
            try:
                for i in range(users_per_thread):
                    user = user_repo_via_factory.create_user(
                        f"{prefix}-{i}@ex.com", f"{prefix}-{i}",
                    )
                    user_repo_via_factory.update_user(
                        user.id, display_name=f"{prefix}-{i}-renamed",
                    )
            except BaseException as exc:  # noqa: BLE001 — test-only
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=thread_count) as ex:
            futures = [
                ex.submit(worker, f"t{i}") for i in range(thread_count)
            ]
            for f in as_completed(futures):
                f.result()

        assert errors == []
        # Don't assume zero seeded users — only assert *our* writes succeeded.
        users = user_repo_via_factory.list_users()
        ours = [u for u in users if "@ex.com" in u.email]
        assert len(ours) == thread_count * users_per_thread
        assert all("-renamed" in u.display_name for u in ours)

    def test_cross_thread_visibility_via_wal(
        self, user_repo_via_factory: SQLiteUserRepository,
    ) -> None:
        from threading import Event

        written = Event()

        def writer() -> None:
            user_repo_via_factory.create_user("a@x.com", "A")
            user_repo_via_factory.create_user("b@x.com", "B")
            written.set()

        t = threading.Thread(target=writer)
        t.start()
        written.wait(timeout=5.0)
        t.join()
        users = user_repo_via_factory.list_users()
        emails = {u.email for u in users}
        assert "a@x.com" in emails
        assert "b@x.com" in emails
