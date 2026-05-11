"""Tests for session token SHA-256 hashing before storage.

Session tokens follow the same pattern as API keys: the raw token is
returned to the caller (set as a cookie), but only the SHA-256 hash is
persisted in the ``user_sessions`` table. These tests verify that
contract end-to-end through AuthService.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

import pytest

from journal.db.user_repository import SQLiteUserRepository
from journal.services.auth import AuthService

if TYPE_CHECKING:
    import sqlite3

    from journal.db.factory import ConnectionFactory

# The seeded admin user created by migration 0011.
_ADMIN_USER_ID = 1
_ADMIN_EMAIL = "admin@journal.local"


@pytest.fixture
def user_repo(factory: ConnectionFactory) -> SQLiteUserRepository:
    return SQLiteUserRepository(factory)


@pytest.fixture
def auth_service(user_repo: SQLiteUserRepository) -> AuthService:
    return AuthService(user_repo, secret_key="test-secret")


# ── 1. Token is hashed before storage ─────────────────────────────────


class TestTokenHashedBeforeStorage:
    def test_raw_token_not_stored_in_sessions_table(
        self,
        auth_service: AuthService,
        db_conn: sqlite3.Connection,
    ) -> None:
        """After create_session, the raw token must NOT appear in user_sessions.id."""
        raw_token = auth_service.create_session(_ADMIN_USER_ID)

        rows = db_conn.execute("SELECT id FROM user_sessions").fetchall()
        stored_ids = [row["id"] for row in rows]

        assert raw_token not in stored_ids, (
            "The raw token was stored directly -- it should be hashed first"
        )

    def test_stored_id_is_sha256_of_raw_token(
        self,
        auth_service: AuthService,
        db_conn: sqlite3.Connection,
    ) -> None:
        """The id column should contain SHA-256(raw_token)."""
        raw_token = auth_service.create_session(_ADMIN_USER_ID)
        expected_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        row = db_conn.execute(
            "SELECT id FROM user_sessions WHERE id = ?", (expected_hash,)
        ).fetchone()

        assert row is not None, (
            "No session row found with the expected SHA-256 hash as its id"
        )
        assert row["id"] == expected_hash


# ── 2. Validation works with raw token ────────────────────────────────


class TestValidateWithRawToken:
    def test_validate_session_returns_user_for_raw_token(
        self,
        auth_service: AuthService,
    ) -> None:
        """validate_session should accept the raw token and return the user."""
        raw_token = auth_service.create_session(_ADMIN_USER_ID)
        user = auth_service.validate_session(raw_token)

        assert user is not None
        assert user.id == _ADMIN_USER_ID
        assert user.email == _ADMIN_EMAIL


# ── 3. Validation rejects the hash directly ───────────────────────────


class TestValidateRejectsHashDirectly:
    def test_passing_hash_to_validate_returns_none(
        self,
        auth_service: AuthService,
    ) -> None:
        """Passing the stored hash (instead of the raw token) should fail.

        validate_session hashes its input, so passing an already-hashed
        value produces a double-hash that won't match any stored row.
        """
        raw_token = auth_service.create_session(_ADMIN_USER_ID)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        result = auth_service.validate_session(token_hash)
        assert result is None, (
            "validate_session should return None when given the hash directly"
        )


# ── 4. Logout works with raw token ───────────────────────────────────


class TestLogoutWithRawToken:
    def test_logout_deletes_session(
        self,
        auth_service: AuthService,
        db_conn: sqlite3.Connection,
    ) -> None:
        """logout(raw_token) should delete the session row."""
        raw_token = auth_service.create_session(_ADMIN_USER_ID)

        # Confirm the session exists first
        assert auth_service.validate_session(raw_token) is not None

        auth_service.logout(raw_token)

        # validate_session should now return None
        assert auth_service.validate_session(raw_token) is None

    def test_logout_removes_row_from_db(
        self,
        auth_service: AuthService,
        db_conn: sqlite3.Connection,
    ) -> None:
        """After logout, the hashed session id should be gone from the table."""
        raw_token = auth_service.create_session(_ADMIN_USER_ID)
        expected_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        auth_service.logout(raw_token)

        row = db_conn.execute(
            "SELECT id FROM user_sessions WHERE id = ?", (expected_hash,)
        ).fetchone()
        assert row is None, "Session row should be deleted after logout"


# ── 5. Hash format matches SHA-256 ───────────────────────────────────


class TestHashFormatIsSHA256:
    _SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

    def test_stored_session_id_is_64_hex_chars(
        self,
        auth_service: AuthService,
        db_conn: sqlite3.Connection,
    ) -> None:
        """The stored session id must be exactly 64 lowercase hex characters."""
        auth_service.create_session(_ADMIN_USER_ID)

        rows = db_conn.execute("SELECT id FROM user_sessions").fetchall()
        assert len(rows) >= 1

        for row in rows:
            stored_id = row["id"]
            assert len(stored_id) == 64, (
                f"Expected 64 hex chars, got {len(stored_id)}"
            )
            assert self._SHA256_HEX_RE.match(stored_id), (
                f"Stored id is not a valid SHA-256 hex digest: {stored_id!r}"
            )
