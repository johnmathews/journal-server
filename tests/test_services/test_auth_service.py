"""Tests for AuthService."""

import sqlite3
import time

import pytest

from journal.db.factory import ConnectionFactory
from journal.db.user_repository import SQLiteUserRepository
from journal.models import ApiKeyInfo, User
from journal.services.auth import AuthService


@pytest.fixture
def user_repo(factory: ConnectionFactory) -> SQLiteUserRepository:
    return SQLiteUserRepository(factory)


@pytest.fixture
def auth(user_repo: SQLiteUserRepository) -> AuthService:
    return AuthService(user_repo, secret_key="test-secret-key-1234", session_expiry_days=7)


# ── Password hashing ───────────────────────────────────────────────


class TestPasswordHashing:
    def test_hash_and_verify(self, auth: AuthService) -> None:
        hashed = auth.hash_password("my-password")
        assert hashed != "my-password"
        assert auth.verify_password(hashed, "my-password") is True

    def test_wrong_password(self, auth: AuthService) -> None:
        hashed = auth.hash_password("correct")
        assert auth.verify_password(hashed, "wrong") is False

    def test_hash_is_different_each_time(self, auth: AuthService) -> None:
        h1 = auth.hash_password("same")
        h2 = auth.hash_password("same")
        assert h1 != h2  # Argon2 uses random salts


# ── Registration ────────────────────────────────────────────────────


class TestRegistration:
    def test_register_creates_user(self, auth: AuthService) -> None:
        user = auth.register_user("reg@example.com", "password123", "Reg User")
        assert isinstance(user, User)
        assert user.email == "reg@example.com"
        assert user.display_name == "Reg User"
        assert user.is_active is True

    def test_register_duplicate_raises(self, auth: AuthService) -> None:
        auth.register_user("dup@example.com", "password123", "First")
        with pytest.raises(ValueError, match="Email already registered"):
            auth.register_user("dup@example.com", "password456", "Second")

    def test_register_stores_hashed_password(
        self, auth: AuthService, user_repo: SQLiteUserRepository
    ) -> None:
        user = auth.register_user("hash@example.com", "plaintext", "Hash")
        stored_hash = user_repo.get_password_hash(user.id)
        assert stored_hash is not None
        assert stored_hash != "plaintext"
        assert auth.verify_password(stored_hash, "plaintext") is True


# ── Authentication ──────────────────────────────────────────────────


class TestAuthentication:
    def test_authenticate_success(self, auth: AuthService) -> None:
        auth.register_user("auth@example.com", "correct-pw", "Auth")
        user = auth.authenticate("auth@example.com", "correct-pw")
        assert user.email == "auth@example.com"

    def test_authenticate_wrong_password(self, auth: AuthService) -> None:
        auth.register_user("wrong@example.com", "correct-pw", "Wrong")
        with pytest.raises(ValueError, match="Invalid email or password"):
            auth.authenticate("wrong@example.com", "bad-pw")

    def test_authenticate_unknown_email(self, auth: AuthService) -> None:
        with pytest.raises(ValueError, match="Invalid email or password"):
            auth.authenticate("nobody@example.com", "anything")

    def test_authenticate_disabled_account(
        self, auth: AuthService, user_repo: SQLiteUserRepository
    ) -> None:
        user = auth.register_user("disabled@example.com", "pw", "Disabled")
        user_repo.update_user(user.id, is_active=False)
        with pytest.raises(ValueError, match="Account is disabled"):
            auth.authenticate("disabled@example.com", "pw")

    def test_authenticate_no_password_set(
        self, auth: AuthService, user_repo: SQLiteUserRepository
    ) -> None:
        # User created without a password (e.g. social login placeholder)
        user_repo.create_user("nopw@example.com", "NoPW")
        with pytest.raises(ValueError, match="Invalid email or password"):
            auth.authenticate("nopw@example.com", "anything")

    def test_lockout_after_repeated_failures(
        self, auth: AuthService, db_conn: sqlite3.Connection
    ) -> None:
        auth.register_user("lock@example.com", "correct", "Lock")
        for _ in range(5):
            with pytest.raises(ValueError):
                auth.authenticate("lock@example.com", "wrong")
        # After 5 failures, account should be locked — even the correct password fails
        with pytest.raises(ValueError, match="Account temporarily locked"):
            auth.authenticate("lock@example.com", "correct")

    def test_successful_login_resets_failures(
        self, auth: AuthService, db_conn: sqlite3.Connection
    ) -> None:
        auth.register_user("reset@example.com", "correct", "Reset")
        # Fail a few times
        for _ in range(3):
            with pytest.raises(ValueError):
                auth.authenticate("reset@example.com", "wrong")
        # Succeed
        user = auth.authenticate("reset@example.com", "correct")
        assert user.email == "reset@example.com"
        # Check counter was reset
        row = db_conn.execute(
            "SELECT failed_login_attempts FROM users WHERE id = ?", (user.id,)
        ).fetchone()
        assert row["failed_login_attempts"] == 0


# ── Sessions ────────────────────────────────────────────────────────


class TestSessions:
    def test_create_and_validate_session(self, auth: AuthService) -> None:
        user = auth.register_user("sess@example.com", "pw", "Sess")
        token = auth.create_session(user.id, user_agent="pytest", ip_address="127.0.0.1")
        assert isinstance(token, str)
        assert len(token) > 20

        validated = auth.validate_session(token)
        assert validated is not None
        assert validated.id == user.id
        assert validated.email == "sess@example.com"

    def test_validate_invalid_session(self, auth: AuthService) -> None:
        assert auth.validate_session("bogus-token") is None

    def test_logout_invalidates_session(self, auth: AuthService) -> None:
        user = auth.register_user("logout@example.com", "pw", "Logout")
        token = auth.create_session(user.id)
        auth.logout(token)
        assert auth.validate_session(token) is None

    def test_logout_all(self, auth: AuthService) -> None:
        user = auth.register_user("logoutall@example.com", "pw", "LogoutAll")
        t1 = auth.create_session(user.id)
        t2 = auth.create_session(user.id)
        count = auth.logout_all(user.id)
        assert count == 2
        assert auth.validate_session(t1) is None
        assert auth.validate_session(t2) is None


# ── API Keys ────────────────────────────────────────────────────────


class TestApiKeys:
    def test_create_and_validate_key(self, auth: AuthService) -> None:
        user = auth.register_user("apikey@example.com", "pw", "API")
        full_key, info = auth.create_api_key(user.id, "Test Key")
        assert full_key.startswith("jnl_")
        assert isinstance(info, ApiKeyInfo)
        assert info.name == "Test Key"
        assert info.user_id == user.id
        assert info.key_prefix == full_key[:12]

        validated = auth.validate_api_key(full_key)
        assert validated is not None
        assert validated.id == user.id

    def test_validate_wrong_key(self, auth: AuthService) -> None:
        assert auth.validate_api_key("jnl_bogus_key_value") is None

    def test_create_key_with_expiry(self, auth: AuthService) -> None:
        user = auth.register_user("expkey@example.com", "pw", "ExpKey")
        _, info = auth.create_api_key(user.id, "Expiring", expires_days=30)
        assert info.expires_at is not None

    def test_revoke_key(self, auth: AuthService) -> None:
        user = auth.register_user("revoke@example.com", "pw", "Revoke")
        full_key, info = auth.create_api_key(user.id, "Revocable")
        assert auth.revoke_api_key(info.id, user.id) is True
        assert auth.validate_api_key(full_key) is None

    def test_list_keys(self, auth: AuthService) -> None:
        user = auth.register_user("list@example.com", "pw", "List")
        auth.create_api_key(user.id, "Key 1")
        auth.create_api_key(user.id, "Key 2")
        keys = auth.list_api_keys(user.id)
        assert len(keys) == 2
        names = {k.name for k in keys}
        assert names == {"Key 1", "Key 2"}


# ── Password reset tokens ──────────────────────────────────────────


class TestPasswordResetTokens:
    def test_generate_and_validate_reset_token(self, auth: AuthService) -> None:
        auth.register_user("reset@example.com", "password123", "Reset")
        token = auth.generate_reset_token("reset@example.com")
        email = auth.validate_reset_token(token)
        assert email == "reset@example.com"

    def test_invalid_reset_token_raises(self, auth: AuthService) -> None:
        with pytest.raises(ValueError, match="Invalid or expired reset token"):
            auth.validate_reset_token("garbage-token")

    def test_expired_reset_token_raises(self, auth: AuthService) -> None:
        token = auth.generate_reset_token("old@example.com")
        time.sleep(1.1)
        with pytest.raises(ValueError, match="Invalid or expired reset token"):
            auth.validate_reset_token(token, max_age=0)

    def test_reset_password_flow(self, auth: AuthService) -> None:
        auth.register_user("pwreset@example.com", "old-password", "PW Reset")
        token = auth.generate_reset_token("pwreset@example.com")
        user = auth.reset_password(token, "new-password")
        assert user.email == "pwreset@example.com"
        # Old password no longer works
        with pytest.raises(ValueError, match="Invalid email or password"):
            auth.authenticate("pwreset@example.com", "old-password")
        # New password works
        authed = auth.authenticate("pwreset@example.com", "new-password")
        assert authed.email == "pwreset@example.com"

    def test_reset_password_unknown_email(self, auth: AuthService) -> None:
        # A token generated for an unregistered email must never
        # validate — and must not reveal whether the account exists.
        token = auth.generate_reset_token("ghost@example.com")
        with pytest.raises(ValueError, match="Invalid or expired reset token"):
            auth.reset_password(token, "irrelevant-pw")

    def test_reset_token_is_single_use(self, auth: AuthService) -> None:
        """A reset token is bound to the password hash it was issued
        against: once the password changes, replaying the same token
        must fail like any invalid/expired token."""
        auth.register_user("single@example.com", "first-password", "Single")
        token = auth.generate_reset_token("single@example.com")
        auth.reset_password(token, "second-password")
        with pytest.raises(ValueError, match="Invalid or expired reset token"):
            auth.reset_password(token, "third-password")
        # The first reset still holds.
        authed = auth.authenticate("single@example.com", "second-password")
        assert authed.email == "single@example.com"

    def test_outstanding_reset_tokens_invalidated_by_any_reset(
        self, auth: AuthService
    ) -> None:
        """Two outstanding tokens: using either one invalidates the other."""
        auth.register_user("pair@example.com", "first-password", "Pair")
        token_a = auth.generate_reset_token("pair@example.com")
        token_b = auth.generate_reset_token("pair@example.com")
        auth.reset_password(token_a, "second-password")
        with pytest.raises(ValueError, match="Invalid or expired reset token"):
            auth.validate_reset_token(token_b)


# ── Email verification tokens ──────────────────────────────────────


class TestVerificationTokens:
    def test_generate_and_validate_verification_token(
        self, auth: AuthService
    ) -> None:
        token = auth.generate_verification_token("verify@example.com")
        email = auth.validate_verification_token(token)
        assert email == "verify@example.com"

    def test_invalid_verification_token_raises(self, auth: AuthService) -> None:
        with pytest.raises(
            ValueError, match="Invalid or expired verification token"
        ):
            auth.validate_verification_token("garbage")

    def test_expired_verification_token_raises(self, auth: AuthService) -> None:
        token = auth.generate_verification_token("old@example.com")
        time.sleep(1.1)
        with pytest.raises(
            ValueError, match="Invalid or expired verification token"
        ):
            auth.validate_verification_token(token, max_age=0)

    def test_verify_email_flow(self, auth: AuthService) -> None:
        auth.register_user("vemail@example.com", "pw", "VEmail")
        token = auth.generate_verification_token("vemail@example.com")
        user = auth.verify_email(token)
        assert user.email_verified is True

    def test_verify_email_unknown_user(self, auth: AuthService) -> None:
        token = auth.generate_verification_token("ghost@example.com")
        with pytest.raises(ValueError, match="User not found"):
            auth.verify_email(token)
