"""Tests for auth and admin REST API endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from journal.auth import build_auth_middleware_stack
from journal.config import Config
from journal.db.connection import get_connection
from journal.db.migrations import run_migrations
from journal.db.user_repository import SQLiteUserRepository
from journal.services.auth import AuthService

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Generator
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_db_conn(tmp_path: Path) -> Generator[sqlite3.Connection]:
    """Migrated SQLite connection for auth API tests.

    Uses ``check_same_thread=False`` because the Starlette TestClient
    runs the ASGI app in a separate thread.
    """
    db_path = tmp_path / "test_auth_api.db"
    conn = get_connection(db_path, check_same_thread=False)
    run_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def user_repo(auth_db_conn: sqlite3.Connection) -> SQLiteUserRepository:
    return SQLiteUserRepository(auth_db_conn)


@pytest.fixture
def auth_config(tmp_path: Path) -> Config:
    return Config(
        db_path=tmp_path / "test_auth_api.db",
        secret_key="test-secret-key-for-tokens",
        registration_enabled=True,
        session_expiry_days=7,
        app_base_url="http://localhost:5173",
    )


@pytest.fixture
def auth_service(user_repo: SQLiteUserRepository, auth_config: Config) -> AuthService:
    return AuthService(
        user_repo=user_repo,
        secret_key=auth_config.secret_key,
        session_expiry_days=auth_config.session_expiry_days,
    )


@pytest.fixture
def mock_email_service() -> MagicMock:
    svc = MagicMock()
    svc.send_verification_email = AsyncMock()
    svc.send_password_reset_email = AsyncMock()
    return svc


@pytest.fixture
def services(
    auth_service: AuthService,
    user_repo: SQLiteUserRepository,
    auth_config: Config,
    mock_email_service: MagicMock,
) -> dict[str, Any]:
    return {
        "auth_service": auth_service,
        "email_service": mock_email_service,
        "user_repo": user_repo,
        "config": auth_config,
    }


@pytest.fixture
def client(services: dict, auth_service: AuthService) -> Generator[TestClient]:
    """Create a TestClient with auth routes + auth middleware."""
    from mcp.server.fastmcp import FastMCP

    from journal.auth_api import register_admin_routes, register_auth_routes

    test_mcp = FastMCP("test-auth-api")
    register_auth_routes(test_mcp, lambda: services)
    register_admin_routes(test_mcp, lambda: services)

    inner_app = test_mcp.streamable_http_app()
    # Wrap with auth middleware so get_authenticated_user() works
    app = build_auth_middleware_stack(inner_app, auth_service)

    with TestClient(app, raise_server_exceptions=False) as tc:
        yield tc


def _register_user(
    auth_service: AuthService,
    email: str = "alice@example.com",
    password: str = "securepassword",
    display_name: str = "Alice",
) -> tuple[Any, str]:
    """Register a user and create a session, returning (user, session_id)."""
    user = auth_service.register_user(email, password, display_name)
    session_id = auth_service.create_session(user.id)
    return user, session_id


def _register_admin(
    auth_service: AuthService,
    user_repo: SQLiteUserRepository,
    email: str = "admin@example.com",
    password: str = "adminpassword",
    display_name: str = "Admin",
) -> tuple[Any, str]:
    """Register an admin user and create a session."""
    user = auth_service.register_user(email, password, display_name)
    user_repo.update_user(user.id, is_admin=True, email_verified=True)
    session_id = auth_service.create_session(user.id)
    return user, session_id


# ---------------------------------------------------------------------------
# Login tests
# ---------------------------------------------------------------------------


class TestLogin:
    def test_login_success(
        self,
        client: TestClient,
        auth_service: AuthService,
    ) -> None:
        auth_service.register_user("alice@example.com", "securepassword", "Alice")
        resp = client.post(
            "/api/auth/login",
            json={"email": "alice@example.com", "password": "securepassword"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user"]["email"] == "alice@example.com"
        assert data["user"]["display_name"] == "Alice"
        assert "session_id" in resp.cookies

    def test_login_bad_password(
        self,
        client: TestClient,
        auth_service: AuthService,
    ) -> None:
        auth_service.register_user("alice@example.com", "securepassword", "Alice")
        resp = client.post(
            "/api/auth/login",
            json={"email": "alice@example.com", "password": "wrong"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"] == "invalid_credentials"

    def test_login_nonexistent_user(self, client: TestClient) -> None:
        resp = client.post(
            "/api/auth/login",
            json={"email": "nobody@example.com", "password": "anything"},
        )
        assert resp.status_code == 401

    def test_login_missing_fields(self, client: TestClient) -> None:
        resp = client.post("/api/auth/login", json={"email": ""})
        assert resp.status_code == 400
        assert resp.json()["error"] == "missing_fields"

    def test_login_invalid_json(self, client: TestClient) -> None:
        resp = client.post(
            "/api/auth/login",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Logout tests
# ---------------------------------------------------------------------------


class TestLogout:
    def test_logout_clears_session(
        self,
        client: TestClient,
        auth_service: AuthService,
    ) -> None:
        _user, session_id = _register_user(auth_service)
        resp = client.post(
            "/api/auth/logout",
            cookies={"session_id": session_id},
        )
        assert resp.status_code == 200
        # Session should be deleted
        assert auth_service.validate_session(session_id) is None

    def test_logout_without_session_still_succeeds(
        self,
        client: TestClient,
        auth_service: AuthService,
    ) -> None:
        # Need to be authenticated to reach the endpoint (middleware).
        # Use a valid session but without a cookie named session_id.
        _user, session_id = _register_user(auth_service)
        resp = client.post(
            "/api/auth/logout",
            cookies={"session_id": session_id},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Me tests
# ---------------------------------------------------------------------------


class TestMe:
    def test_me_returns_current_user(
        self,
        client: TestClient,
        auth_service: AuthService,
    ) -> None:
        _user, session_id = _register_user(auth_service)
        resp = client.get(
            "/api/auth/me",
            cookies={"session_id": session_id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user"]["email"] == "alice@example.com"

    def test_me_unauthenticated(self, client: TestClient) -> None:
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Update me tests
# ---------------------------------------------------------------------------


class TestUpdateMe:
    def test_update_display_name(
        self,
        client: TestClient,
        auth_service: AuthService,
    ) -> None:
        _user, session_id = _register_user(auth_service)
        resp = client.patch(
            "/api/auth/me",
            json={"display_name": "Alice Wonderland"},
            cookies={"session_id": session_id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user"]["display_name"] == "Alice Wonderland"

    def test_update_display_name_persists(
        self,
        client: TestClient,
        auth_service: AuthService,
    ) -> None:
        _user, session_id = _register_user(auth_service)
        client.patch(
            "/api/auth/me",
            json={"display_name": "New Name"},
            cookies={"session_id": session_id},
        )
        resp = client.get(
            "/api/auth/me",
            cookies={"session_id": session_id},
        )
        assert resp.json()["user"]["display_name"] == "New Name"

    def test_update_display_name_empty_rejected(
        self,
        client: TestClient,
        auth_service: AuthService,
    ) -> None:
        _user, session_id = _register_user(auth_service)
        resp = client.patch(
            "/api/auth/me",
            json={"display_name": "  "},
            cookies={"session_id": session_id},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "missing_fields"

    def test_update_display_name_missing_field(
        self,
        client: TestClient,
        auth_service: AuthService,
    ) -> None:
        _user, session_id = _register_user(auth_service)
        resp = client.patch(
            "/api/auth/me",
            json={},
            cookies={"session_id": session_id},
        )
        assert resp.status_code == 400

    def test_update_display_name_invalid_json(
        self,
        client: TestClient,
        auth_service: AuthService,
    ) -> None:
        _user, session_id = _register_user(auth_service)
        resp = client.patch(
            "/api/auth/me",
            content=b"not json",
            headers={"content-type": "application/json"},
            cookies={"session_id": session_id},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_body"

    def test_update_display_name_unauthenticated(self, client: TestClient) -> None:
        resp = client.patch(
            "/api/auth/me",
            json={"display_name": "Hacker"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_success(
        self,
        client: TestClient,
        mock_email_service: MagicMock,
    ) -> None:
        resp = client.post(
            "/api/auth/register",
            json={
                "email": "bob@example.com",
                "password": "securepassword",
                "display_name": "Bob",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["user"]["email"] == "bob@example.com"
        assert "session_id" in resp.cookies
        mock_email_service.send_verification_email.assert_awaited_once()

    def test_register_duplicate_email(
        self,
        client: TestClient,
        auth_service: AuthService,
    ) -> None:
        auth_service.register_user("dup@example.com", "securepassword", "Dup")
        resp = client.post(
            "/api/auth/register",
            json={
                "email": "dup@example.com",
                "password": "securepassword",
                "display_name": "Dup2",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "duplicate_email"

    def test_register_weak_password(self, client: TestClient) -> None:
        resp = client.post(
            "/api/auth/register",
            json={
                "email": "weak@example.com",
                "password": "short",
                "display_name": "Weak",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "weak_password"

    def test_register_missing_fields(self, client: TestClient) -> None:
        resp = client.post(
            "/api/auth/register",
            json={"email": "missing@example.com"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "missing_fields"

    def test_register_disabled(
        self,
        client: TestClient,
        services: dict,
    ) -> None:
        # Replace config with registration disabled
        original_config = services["config"]
        services["config"] = Config(
            db_path=original_config.db_path,
            secret_key=original_config.secret_key,
            registration_enabled=False,
        )
        resp = client.post(
            "/api/auth/register",
            json={
                "email": "disabled@example.com",
                "password": "securepassword",
                "display_name": "Disabled",
            },
        )
        assert resp.status_code == 403
        assert resp.json()["error"] == "registration_disabled"
        # Restore
        services["config"] = original_config

    def test_register_without_email_service(
        self,
        client: TestClient,
        services: dict,
    ) -> None:
        """Registration succeeds even if email service is None."""
        services["email_service"] = None
        resp = client.post(
            "/api/auth/register",
            json={
                "email": "noemail@example.com",
                "password": "securepassword",
                "display_name": "No Email",
            },
        )
        assert resp.status_code == 201
        # Restore
        services["email_service"] = MagicMock()


# ---------------------------------------------------------------------------
# Auth config tests
# ---------------------------------------------------------------------------


class TestAuthConfig:
    def test_auth_config_returns_registration_status(
        self,
        client: TestClient,
    ) -> None:
        resp = client.get("/api/auth/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "registration_enabled" in data
        assert data["registration_enabled"] is True


# ---------------------------------------------------------------------------
# Forgot password tests
# ---------------------------------------------------------------------------


class TestForgotPassword:
    def test_forgot_password_existing_user(
        self,
        client: TestClient,
        auth_service: AuthService,
        mock_email_service: MagicMock,
    ) -> None:
        auth_service.register_user("forgot@example.com", "securepassword", "Forgot")
        resp = client.post(
            "/api/auth/forgot-password",
            json={"email": "forgot@example.com"},
        )
        assert resp.status_code == 200
        mock_email_service.send_password_reset_email.assert_awaited_once()

    def test_forgot_password_nonexistent_user_still_200(
        self,
        client: TestClient,
        mock_email_service: MagicMock,
    ) -> None:
        """No email enumeration — always returns 200."""
        resp = client.post(
            "/api/auth/forgot-password",
            json={"email": "nobody@example.com"},
        )
        assert resp.status_code == 200
        mock_email_service.send_password_reset_email.assert_not_awaited()

    def test_forgot_password_empty_email(self, client: TestClient) -> None:
        resp = client.post(
            "/api/auth/forgot-password",
            json={"email": ""},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Reset token verification tests
# ---------------------------------------------------------------------------


class TestVerifyResetToken:
    def test_verify_valid_reset_token(
        self,
        client: TestClient,
        auth_service: AuthService,
    ) -> None:
        auth_service.register_user("token@example.com", "securepassword", "Token")
        token = auth_service.generate_reset_token("token@example.com")
        resp = client.get(f"/api/auth/verify-reset-token?token={token}")
        assert resp.status_code == 200
        assert resp.json()["valid"] is True

    def test_verify_invalid_reset_token(self, client: TestClient) -> None:
        resp = client.get("/api/auth/verify-reset-token?token=bogus")
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_token"

    def test_verify_missing_token(self, client: TestClient) -> None:
        resp = client.get("/api/auth/verify-reset-token")
        assert resp.status_code == 400
        assert resp.json()["error"] == "missing_token"


# ---------------------------------------------------------------------------
# Reset password tests
# ---------------------------------------------------------------------------


class TestResetPassword:
    def test_reset_password_success(
        self,
        client: TestClient,
        auth_service: AuthService,
    ) -> None:
        auth_service.register_user("reset@example.com", "oldpassword1", "Reset")
        token = auth_service.generate_reset_token("reset@example.com")
        resp = client.post(
            "/api/auth/reset-password",
            json={"token": token, "password": "newpassword1"},
        )
        assert resp.status_code == 200

        # Verify new password works
        user = auth_service.authenticate("reset@example.com", "newpassword1")
        assert user.email == "reset@example.com"

    def test_reset_password_invalid_token(self, client: TestClient) -> None:
        resp = client.post(
            "/api/auth/reset-password",
            json={"token": "bogus", "password": "newpassword1"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_token"

    def test_reset_password_weak_password(
        self,
        client: TestClient,
        auth_service: AuthService,
    ) -> None:
        auth_service.register_user("weakreset@example.com", "oldpassword1", "Weak")
        token = auth_service.generate_reset_token("weakreset@example.com")
        resp = client.post(
            "/api/auth/reset-password",
            json={"token": token, "password": "short"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "weak_password"

    def test_reset_password_missing_fields(self, client: TestClient) -> None:
        resp = client.post(
            "/api/auth/reset-password",
            json={"token": ""},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "missing_fields"


# ---------------------------------------------------------------------------
# Email verification tests
# ---------------------------------------------------------------------------


class TestVerifyEmail:
    def test_verify_email_success(
        self,
        client: TestClient,
        auth_service: AuthService,
        user_repo: SQLiteUserRepository,
    ) -> None:
        user = auth_service.register_user(
            "verify@example.com", "securepassword", "Verify",
        )
        assert not user.email_verified
        token = auth_service.generate_verification_token("verify@example.com")
        resp = client.get(f"/api/auth/verify-email?token={token}")
        assert resp.status_code == 200

        updated = user_repo.get_user_by_id(user.id)
        assert updated is not None
        assert updated.email_verified is True

    def test_verify_email_invalid_token(self, client: TestClient) -> None:
        resp = client.get("/api/auth/verify-email?token=bogus")
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_token"

    def test_verify_email_missing_token(self, client: TestClient) -> None:
        resp = client.get("/api/auth/verify-email")
        assert resp.status_code == 400
        assert resp.json()["error"] == "missing_token"

    def test_verify_email_post_not_allowed(self, client: TestClient) -> None:
        """Regression: webapp previously sent POST — server only accepts GET."""
        resp = client.post(
            "/api/auth/verify-email",
            json={"token": "some-token"},
        )
        assert resp.status_code == 405

    def test_full_register_verify_access_flow(
        self,
        client: TestClient,
        auth_service: AuthService,
        user_repo: SQLiteUserRepository,
    ) -> None:
        """Regression: full register → verify → access protected endpoint flow.

        Previously, email verification never completed because:
        1. The webapp router redirected authenticated users away from /verify-email
        2. The webapp sent POST instead of GET
        3. The auth store wasn't refreshed after verification

        This test ensures the server-side flow works end-to-end: register a user,
        confirm they get 403 on protected endpoints, verify their email, then
        confirm they get 200.
        """
        # Register and get session
        resp = client.post(
            "/api/auth/register",
            json={
                "email": "newuser@example.com",
                "password": "securepassword",
                "display_name": "New User",
            },
        )
        assert resp.status_code == 201
        session_id = resp.cookies.get("session_id")
        assert session_id

        # Make user admin so we can test a real protected endpoint
        user = user_repo.get_user_by_email("newuser@example.com")
        assert user is not None
        user_repo.update_user(user.id, is_admin=True)

        # Unverified user gets 403 on protected endpoints
        client.cookies.set("session_id", session_id)
        resp = client.get("/api/admin/users")
        assert resp.status_code == 403
        assert resp.json()["message"] == "Please verify your email"

        # Generate verification token and verify via GET
        token = auth_service.generate_verification_token("newuser@example.com")
        resp = client.get(f"/api/auth/verify-email?token={token}")
        assert resp.status_code == 200

        # Now the same session should have access
        resp = client.get("/api/admin/users")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Resend verification tests
# ---------------------------------------------------------------------------


class TestResendVerification:
    def test_resend_verification(
        self,
        client: TestClient,
        auth_service: AuthService,
        mock_email_service: MagicMock,
    ) -> None:
        _user, session_id = _register_user(auth_service)
        resp = client.post(
            "/api/auth/resend-verification",
            cookies={"session_id": session_id},
        )
        assert resp.status_code == 200
        mock_email_service.send_verification_email.assert_awaited_once()

    def test_resend_verification_already_verified(
        self,
        client: TestClient,
        auth_service: AuthService,
        user_repo: SQLiteUserRepository,
    ) -> None:
        user, session_id = _register_user(auth_service)
        user_repo.update_user(user.id, email_verified=True)
        # Need a new session since the old one cached the unverified state
        session_id = auth_service.create_session(user.id)
        resp = client.post(
            "/api/auth/resend-verification",
            cookies={"session_id": session_id},
        )
        assert resp.status_code == 200
        assert "already verified" in resp.json()["message"].lower()

    def test_resend_verification_no_email_service(
        self,
        client: TestClient,
        auth_service: AuthService,
        services: dict,
    ) -> None:
        _user, session_id = _register_user(auth_service)
        services["email_service"] = None
        resp = client.post(
            "/api/auth/resend-verification",
            cookies={"session_id": session_id},
        )
        assert resp.status_code == 500
        assert resp.json()["error"] == "email_not_configured"

    def test_resend_verification_unauthenticated(self, client: TestClient) -> None:
        resp = client.post("/api/auth/resend-verification")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# API keys tests
# ---------------------------------------------------------------------------


class TestApiKeys:
    def test_create_api_key(
        self,
        client: TestClient,
        auth_service: AuthService,
        user_repo: SQLiteUserRepository,
    ) -> None:
        user, session_id = _register_user(auth_service)
        # Verify email so middleware passes
        user_repo.update_user(user.id, email_verified=True)
        session_id = auth_service.create_session(user.id)
        resp = client.post(
            "/api/auth/api-keys",
            json={"name": "Test Key"},
            cookies={"session_id": session_id},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["key"].startswith("jnl_")
        assert data["name"] == "Test Key"

    def test_create_api_key_with_expiry(
        self,
        client: TestClient,
        auth_service: AuthService,
        user_repo: SQLiteUserRepository,
    ) -> None:
        user, session_id = _register_user(auth_service)
        user_repo.update_user(user.id, email_verified=True)
        session_id = auth_service.create_session(user.id)
        resp = client.post(
            "/api/auth/api-keys",
            json={"name": "Expiring Key", "expires_days": 30},
            cookies={"session_id": session_id},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["expires_at"] is not None

    def test_create_api_key_missing_name(
        self,
        client: TestClient,
        auth_service: AuthService,
        user_repo: SQLiteUserRepository,
    ) -> None:
        user, session_id = _register_user(auth_service)
        user_repo.update_user(user.id, email_verified=True)
        session_id = auth_service.create_session(user.id)
        resp = client.post(
            "/api/auth/api-keys",
            json={},
            cookies={"session_id": session_id},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "missing_fields"

    def test_list_api_keys(
        self,
        client: TestClient,
        auth_service: AuthService,
        user_repo: SQLiteUserRepository,
    ) -> None:
        user, session_id = _register_user(auth_service)
        user_repo.update_user(user.id, email_verified=True)
        session_id = auth_service.create_session(user.id)
        # Create a key first
        auth_service.create_api_key(user.id, "My Key")
        resp = client.get(
            "/api/auth/api-keys",
            cookies={"session_id": session_id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == "My Key"
        # Listed keys should NOT contain the full key
        assert "key" not in data["items"][0]

    def test_revoke_api_key(
        self,
        client: TestClient,
        auth_service: AuthService,
        user_repo: SQLiteUserRepository,
    ) -> None:
        user, session_id = _register_user(auth_service)
        user_repo.update_user(user.id, email_verified=True)
        session_id = auth_service.create_session(user.id)
        _full_key, key_info = auth_service.create_api_key(user.id, "Revokable")
        resp = client.delete(
            f"/api/auth/api-keys/{key_info.id}",
            cookies={"session_id": session_id},
        )
        assert resp.status_code == 200

    def test_revoke_nonexistent_api_key(
        self,
        client: TestClient,
        auth_service: AuthService,
        user_repo: SQLiteUserRepository,
    ) -> None:
        user, session_id = _register_user(auth_service)
        user_repo.update_user(user.id, email_verified=True)
        session_id = auth_service.create_session(user.id)
        resp = client.delete(
            "/api/auth/api-keys/99999",
            cookies={"session_id": session_id},
        )
        assert resp.status_code == 404

    def test_api_keys_unauthenticated(self, client: TestClient) -> None:
        resp = client.get("/api/auth/api-keys")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Admin: list users tests
# ---------------------------------------------------------------------------


class TestAdminListUsers:
    def test_admin_list_users(
        self,
        client: TestClient,
        auth_service: AuthService,
        user_repo: SQLiteUserRepository,
    ) -> None:
        _admin, session_id = _register_admin(auth_service, user_repo)
        # Also register a regular user
        auth_service.register_user("regular@example.com", "securepassword", "Regular")

        resp = client.get(
            "/api/admin/users",
            cookies={"session_id": session_id},
        )
        assert resp.status_code == 200
        data = resp.json()
        # Migrations may seed an initial admin user, so check we have at
        # least the two we created. Use >= to be resilient to seed data.
        assert len(data["items"]) >= 2
        emails = {u["email"] for u in data["items"]}
        assert "admin@example.com" in emails
        assert "regular@example.com" in emails

    def test_non_admin_forbidden(
        self,
        client: TestClient,
        auth_service: AuthService,
        user_repo: SQLiteUserRepository,
    ) -> None:
        user, session_id = _register_user(auth_service)
        user_repo.update_user(user.id, email_verified=True)
        session_id = auth_service.create_session(user.id)
        resp = client.get(
            "/api/admin/users",
            cookies={"session_id": session_id},
        )
        assert resp.status_code == 403
        assert resp.json()["error"] == "forbidden"

    def test_admin_list_users_unauthenticated(self, client: TestClient) -> None:
        resp = client.get("/api/admin/users")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Admin: update user tests
# ---------------------------------------------------------------------------


class TestAdminUpdateUser:
    def test_admin_update_user_active_flag(
        self,
        client: TestClient,
        auth_service: AuthService,
        user_repo: SQLiteUserRepository,
    ) -> None:
        _admin, admin_session = _register_admin(auth_service, user_repo)
        target_user = auth_service.register_user(
            "target@example.com", "securepassword", "Target",
        )

        resp = client.patch(
            f"/api/admin/users/{target_user.id}",
            json={"is_active": False},
            cookies={"session_id": admin_session},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user"]["is_active"] is False

    def test_admin_update_user_admin_flag(
        self,
        client: TestClient,
        auth_service: AuthService,
        user_repo: SQLiteUserRepository,
    ) -> None:
        _admin, admin_session = _register_admin(auth_service, user_repo)
        target_user = auth_service.register_user(
            "promote@example.com", "securepassword", "Promote",
        )

        resp = client.patch(
            f"/api/admin/users/{target_user.id}",
            json={"is_admin": True},
            cookies={"session_id": admin_session},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user"]["is_admin"] is True

    def test_admin_update_nonexistent_user(
        self,
        client: TestClient,
        auth_service: AuthService,
        user_repo: SQLiteUserRepository,
    ) -> None:
        _admin, admin_session = _register_admin(auth_service, user_repo)
        resp = client.patch(
            "/api/admin/users/99999",
            json={"is_active": False},
            cookies={"session_id": admin_session},
        )
        assert resp.status_code == 404

    def test_admin_update_no_fields(
        self,
        client: TestClient,
        auth_service: AuthService,
        user_repo: SQLiteUserRepository,
    ) -> None:
        _admin, admin_session = _register_admin(auth_service, user_repo)
        target_user = auth_service.register_user(
            "nofields@example.com", "securepassword", "NoFields",
        )
        resp = client.patch(
            f"/api/admin/users/{target_user.id}",
            json={},
            cookies={"session_id": admin_session},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "missing_fields"

    def test_admin_update_invalid_field_type(
        self,
        client: TestClient,
        auth_service: AuthService,
        user_repo: SQLiteUserRepository,
    ) -> None:
        _admin, admin_session = _register_admin(auth_service, user_repo)
        target_user = auth_service.register_user(
            "badtype@example.com", "securepassword", "BadType",
        )
        resp = client.patch(
            f"/api/admin/users/{target_user.id}",
            json={"is_active": "yes"},
            cookies={"session_id": admin_session},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_field"

    def test_non_admin_update_forbidden(
        self,
        client: TestClient,
        auth_service: AuthService,
        user_repo: SQLiteUserRepository,
    ) -> None:
        user, session_id = _register_user(auth_service)
        user_repo.update_user(user.id, email_verified=True)
        session_id = auth_service.create_session(user.id)
        resp = client.patch(
            f"/api/admin/users/{user.id}",
            json={"is_admin": True},
            cookies={"session_id": session_id},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Helper serialization tests
# ---------------------------------------------------------------------------


class TestUserSerialization:
    def test_user_to_dict_with_user_model(self) -> None:
        from journal.auth_api import _user_to_dict
        from journal.models import User

        user = User(
            id=1,
            email="test@example.com",
            display_name="Test",
            is_admin=True,
            is_active=True,
            email_verified=True,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
        )
        d = _user_to_dict(user)
        assert d["id"] == 1
        assert d["email"] == "test@example.com"
        assert d["is_admin"] is True
        assert d["created_at"] == "2026-01-01T00:00:00Z"

    def test_user_to_dict_with_authenticated_user(self) -> None:
        from journal.auth import AuthenticatedUser
        from journal.auth_api import _user_to_dict

        user = AuthenticatedUser(
            user_id=2,
            email="auth@example.com",
            display_name="Auth",
            is_admin=False,
            is_active=True,
            email_verified=False,
        )
        d = _user_to_dict(user)
        assert d["id"] == 2
        assert d["email"] == "auth@example.com"
        assert d["is_admin"] is False

    def test_api_key_info_to_dict(self) -> None:
        from journal.auth_api import _api_key_info_to_dict
        from journal.models import ApiKeyInfo

        info = ApiKeyInfo(
            id=1,
            user_id=1,
            key_prefix="jnl_abcd",
            name="Test Key",
            created_at="2026-01-01T00:00:00Z",
            expires_at=None,
            last_used_at=None,
            revoked_at=None,
        )
        d = _api_key_info_to_dict(info)
        assert d["id"] == 1
        assert d["name"] == "Test Key"
        assert d["expires_at"] is None


# ---------------------------------------------------------------------------
# Services unavailable (503) tests
# ---------------------------------------------------------------------------


class TestServicesUnavailable:
    def test_login_503_when_services_not_initialized(self) -> None:
        from mcp.server.fastmcp import FastMCP

        from journal.auth_api import register_auth_routes

        test_mcp = FastMCP("test-503")
        register_auth_routes(test_mcp, lambda: None)
        app = test_mcp.streamable_http_app()

        with TestClient(app, raise_server_exceptions=False) as tc:
            resp = tc.post(
                "/api/auth/login",
                json={"email": "a@b.com", "password": "test"},
            )
            assert resp.status_code == 503
