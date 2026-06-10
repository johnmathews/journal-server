"""Tests for the session + API key authentication middleware."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from journal.auth import (
    PUBLIC_PATHS,
    VERIFICATION_EXEMPT_PATHS,
    AuthenticatedUser,
    SessionOrKeyBackend,
    build_auth_middleware_stack,
    clear_session_cookie,
    get_authenticated_user,
    set_session_cookie,
)

if TYPE_CHECKING:
    from starlette.requests import Request


# ---------------------------------------------------------------------------
# Fake user / auth service for testing
# ---------------------------------------------------------------------------


@dataclass
class FakeUser:
    id: int
    email: str
    display_name: str
    is_admin: bool = False
    is_active: bool = True
    email_verified: bool = True


class FakeAuthService:
    """In-memory auth service for tests."""

    def __init__(self) -> None:
        self.sessions: dict[str, FakeUser] = {}
        self.api_keys: dict[str, FakeUser] = {}

    def validate_session(self, session_id: str) -> FakeUser | None:
        return self.sessions.get(session_id)

    def validate_api_key(self, key: str) -> FakeUser | None:
        return self.api_keys.get(key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_USER = FakeUser(
    id=1,
    email="alice@example.com",
    display_name="Alice",
    is_admin=False,
    is_active=True,
    email_verified=True,
)


def _build_app(
    auth_service: FakeAuthService | None = None,
    *,
    extra_routes: list[Route] | None = None,
) -> TestClient:
    """Build a Starlette app with the full auth middleware stack."""

    async def ok(request: Request) -> JSONResponse:
        user = request.user
        if hasattr(user, "user_id"):
            return JSONResponse(
                {"ok": True, "user_id": user.user_id, "email": user.email}
            )
        return JSONResponse({"ok": True})

    routes = [
        Route("/api/entries", ok, methods=["GET", "OPTIONS"]),
        Route("/api/entries/{entry_id:int}", ok, methods=["PATCH"]),
        Route("/mcp", ok, methods=["POST"]),
        Route("/health", ok, methods=["GET"]),
        Route("/api/auth/login", ok, methods=["POST"]),
        Route("/api/auth/register", ok, methods=["POST"]),
        Route("/api/auth/config", ok, methods=["GET"]),
        Route("/api/auth/forgot-password", ok, methods=["POST"]),
        Route("/api/auth/verify-reset-token", ok, methods=["POST"]),
        Route("/api/auth/reset-password", ok, methods=["POST"]),
        Route("/api/auth/verify-email", ok, methods=["GET"]),
        Route("/api/auth/me", ok, methods=["GET"]),
        Route("/api/auth/logout", ok, methods=["POST"]),
        Route("/api/auth/resend-verification", ok, methods=["POST"]),
        *(extra_routes or []),
    ]

    svc = auth_service or FakeAuthService()
    inner_app = Starlette(routes=routes)
    app = build_auth_middleware_stack(inner_app, svc)  # type: ignore[arg-type]

    return TestClient(app, raise_server_exceptions=False)


def _build_auth_service_with_user(
    user: FakeUser | None = None,
    session_id: str = "sess-123",
    api_key: str = "jnl_testkey",
) -> FakeAuthService:
    svc = FakeAuthService()
    u = user or _DEFAULT_USER
    svc.sessions[session_id] = u
    svc.api_keys[api_key] = u
    return svc


# ---------------------------------------------------------------------------
# AuthenticatedUser tests
# ---------------------------------------------------------------------------


class TestAuthenticatedUser:
    def test_is_authenticated(self) -> None:
        user = AuthenticatedUser(
            user_id=1,
            email="a@b.com",
            display_name="A",
            is_admin=False,
            is_active=True,
            email_verified=True,
        )
        assert user.is_authenticated is True

    def test_identity_returns_str_user_id(self) -> None:
        user = AuthenticatedUser(
            user_id=42,
            email="a@b.com",
            display_name="A",
            is_admin=False,
            is_active=True,
            email_verified=True,
        )
        assert user.identity == "42"

    def test_display_name_stored_as_attribute(self) -> None:
        user = AuthenticatedUser(
            user_id=1,
            email="a@b.com",
            display_name="Alice Test",
            is_admin=False,
            is_active=True,
            email_verified=True,
        )
        assert user.display_name == "Alice Test"

    def test_all_fields_stored(self) -> None:
        user = AuthenticatedUser(
            user_id=7,
            email="z@z.com",
            display_name="Zara",
            is_admin=True,
            is_active=False,
            email_verified=False,
        )
        assert user.user_id == 7
        assert user.email == "z@z.com"
        assert user.is_admin is True
        assert user.is_active is False
        assert user.email_verified is False


# ---------------------------------------------------------------------------
# SessionOrKeyBackend tests
# ---------------------------------------------------------------------------


class TestSessionOrKeyBackend:
    def test_returns_none_when_no_credentials(self) -> None:
        svc = FakeAuthService()
        backend = SessionOrKeyBackend(svc)  # type: ignore[arg-type]

        import asyncio

        from starlette.requests import HTTPConnection

        scope: dict[str, Any] = {
            "type": "http",
            "headers": [],
            "method": "GET",
            "path": "/api/entries",
        }
        conn = HTTPConnection(scope)
        result = asyncio.run(backend.authenticate(conn))
        assert result is None

    def test_authenticates_via_session_cookie(self) -> None:
        svc = _build_auth_service_with_user()
        backend = SessionOrKeyBackend(svc)  # type: ignore[arg-type]

        import asyncio

        from starlette.requests import HTTPConnection

        scope: dict[str, Any] = {
            "type": "http",
            "headers": [
                (b"cookie", b"session_id=sess-123"),
            ],
            "method": "GET",
            "path": "/api/entries",
        }
        conn = HTTPConnection(scope)
        result = asyncio.run(backend.authenticate(conn))
        assert result is not None
        creds, user = result
        assert "authenticated" in creds.scopes
        assert user.user_id == 1
        assert user.email == "alice@example.com"

    def test_authenticates_via_bearer_token(self) -> None:
        svc = _build_auth_service_with_user()
        backend = SessionOrKeyBackend(svc)  # type: ignore[arg-type]

        import asyncio

        from starlette.requests import HTTPConnection

        scope: dict[str, Any] = {
            "type": "http",
            "headers": [
                (b"authorization", b"Bearer jnl_testkey"),
            ],
            "method": "GET",
            "path": "/api/entries",
        }
        conn = HTTPConnection(scope)
        result = asyncio.run(backend.authenticate(conn))
        assert result is not None
        creds, user = result
        assert "authenticated" in creds.scopes
        assert user.user_id == 1

    def test_session_takes_priority_over_bearer(self) -> None:
        """When both a session cookie and bearer token are present, the
        session cookie is used (it's checked first)."""
        svc = FakeAuthService()
        user_a = FakeUser(id=1, email="a@b.com", display_name="A")
        user_b = FakeUser(id=2, email="b@b.com", display_name="B")
        svc.sessions["sess-A"] = user_a
        svc.api_keys["key-B"] = user_b

        backend = SessionOrKeyBackend(svc)  # type: ignore[arg-type]

        import asyncio

        from starlette.requests import HTTPConnection

        scope: dict[str, Any] = {
            "type": "http",
            "headers": [
                (b"cookie", b"session_id=sess-A"),
                (b"authorization", b"Bearer key-B"),
            ],
            "method": "GET",
            "path": "/api/entries",
        }
        conn = HTTPConnection(scope)
        result = asyncio.run(backend.authenticate(conn))
        assert result is not None
        _, user = result
        assert user.user_id == 1  # user_a from session, not user_b from key

    def test_admin_scope_added_for_admin_user(self) -> None:
        admin = FakeUser(
            id=99, email="admin@x.com", display_name="Admin", is_admin=True
        )
        svc = _build_auth_service_with_user(admin)
        backend = SessionOrKeyBackend(svc)  # type: ignore[arg-type]

        import asyncio

        from starlette.requests import HTTPConnection

        scope: dict[str, Any] = {
            "type": "http",
            "headers": [(b"cookie", b"session_id=sess-123")],
            "method": "GET",
            "path": "/api/entries",
        }
        conn = HTTPConnection(scope)
        result = asyncio.run(backend.authenticate(conn))
        assert result is not None
        creds, _ = result
        assert "admin" in creds.scopes

    def test_invalid_session_falls_through_to_bearer(self) -> None:
        """Invalid session cookie should not block bearer token auth."""
        svc = FakeAuthService()
        user = FakeUser(id=5, email="e@e.com", display_name="E")
        svc.api_keys["key-good"] = user
        # No sessions registered, so cookie will fail

        backend = SessionOrKeyBackend(svc)  # type: ignore[arg-type]

        import asyncio

        from starlette.requests import HTTPConnection

        scope: dict[str, Any] = {
            "type": "http",
            "headers": [
                (b"cookie", b"session_id=bad-session"),
                (b"authorization", b"Bearer key-good"),
            ],
            "method": "GET",
            "path": "/api/entries",
        }
        conn = HTTPConnection(scope)
        result = asyncio.run(backend.authenticate(conn))
        assert result is not None
        _, auth_user = result
        assert auth_user.user_id == 5

    def test_bearer_with_trailing_whitespace(self) -> None:
        svc = _build_auth_service_with_user()
        backend = SessionOrKeyBackend(svc)  # type: ignore[arg-type]

        import asyncio

        from starlette.requests import HTTPConnection

        scope: dict[str, Any] = {
            "type": "http",
            "headers": [
                (b"authorization", b"Bearer jnl_testkey  "),
            ],
            "method": "GET",
            "path": "/api/entries",
        }
        conn = HTTPConnection(scope)
        result = asyncio.run(backend.authenticate(conn))
        assert result is not None

    def test_non_bearer_scheme_ignored(self) -> None:
        svc = _build_auth_service_with_user()
        backend = SessionOrKeyBackend(svc)  # type: ignore[arg-type]

        import asyncio

        from starlette.requests import HTTPConnection

        scope: dict[str, Any] = {
            "type": "http",
            "headers": [
                (b"authorization", b"Basic c2VjcmV0"),
            ],
            "method": "GET",
            "path": "/api/entries",
        }
        conn = HTTPConnection(scope)
        result = asyncio.run(backend.authenticate(conn))
        assert result is None


# ---------------------------------------------------------------------------
# RequireAuthMiddleware integration tests (full stack via TestClient)
# ---------------------------------------------------------------------------


class TestPublicPaths:
    """Public paths should be accessible without authentication."""

    def test_health_no_auth(self) -> None:
        client = _build_app()
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_login_no_auth(self) -> None:
        client = _build_app()
        resp = client.post("/api/auth/login")
        assert resp.status_code == 200

    def test_register_no_auth(self) -> None:
        client = _build_app()
        resp = client.post("/api/auth/register")
        assert resp.status_code == 200

    def test_config_no_auth(self) -> None:
        client = _build_app()
        resp = client.get("/api/auth/config")
        assert resp.status_code == 200

    def test_forgot_password_no_auth(self) -> None:
        client = _build_app()
        resp = client.post("/api/auth/forgot-password")
        assert resp.status_code == 200

    def test_verify_reset_token_no_auth(self) -> None:
        client = _build_app()
        resp = client.post("/api/auth/verify-reset-token")
        assert resp.status_code == 200

    def test_reset_password_no_auth(self) -> None:
        client = _build_app()
        resp = client.post("/api/auth/reset-password")
        assert resp.status_code == 200

    def test_verify_email_no_auth(self) -> None:
        client = _build_app()
        resp = client.get("/api/auth/verify-email")
        assert resp.status_code == 200

    def test_options_always_allowed(self) -> None:
        client = _build_app()
        resp = client.options("/api/entries")
        assert resp.status_code in (200, 204)


class TestUnauthenticatedRejection:
    """Non-public paths must reject unauthenticated requests with 401."""

    def test_api_entries_requires_auth(self) -> None:
        client = _build_app()
        resp = client.get("/api/entries")
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"] == "unauthorized"
        assert "Authentication required" in body["message"]

    def test_mcp_requires_auth(self) -> None:
        client = _build_app()
        resp = client.post("/mcp")
        assert resp.status_code == 401

    def test_patch_requires_auth(self) -> None:
        client = _build_app()
        resp = client.patch("/api/entries/1", json={"final_text": "new"})
        assert resp.status_code == 401


class TestCookieSessionAuth:
    """Cookie-based session authentication end-to-end."""

    def test_valid_session_cookie(self) -> None:
        svc = _build_auth_service_with_user()
        client = _build_app(svc)
        client.cookies.set("session_id", "sess-123")
        resp = client.get("/api/entries")
        assert resp.status_code == 200
        body = resp.json()
        assert body["user_id"] == 1
        assert body["email"] == "alice@example.com"

    def test_invalid_session_cookie(self) -> None:
        svc = _build_auth_service_with_user()
        client = _build_app(svc)
        client.cookies.set("session_id", "bad-session")
        resp = client.get("/api/entries")
        assert resp.status_code == 401


class TestBearerTokenAuth:
    """Bearer token API key authentication end-to-end."""

    def test_valid_bearer_token(self) -> None:
        svc = _build_auth_service_with_user()
        client = _build_app(svc)
        resp = client.get(
            "/api/entries",
            headers={"Authorization": "Bearer jnl_testkey"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["user_id"] == 1

    def test_invalid_bearer_token(self) -> None:
        svc = _build_auth_service_with_user()
        client = _build_app(svc)
        resp = client.get(
            "/api/entries",
            headers={"Authorization": "Bearer wrong_key"},
        )
        assert resp.status_code == 401

    def test_non_bearer_scheme_rejected(self) -> None:
        svc = _build_auth_service_with_user()
        client = _build_app(svc)
        resp = client.get(
            "/api/entries",
            headers={"Authorization": "Basic c2VjcmV0"},
        )
        assert resp.status_code == 401


class TestInactiveAccount:
    """Inactive accounts get 403 even with valid auth."""

    def test_inactive_user_gets_403(self) -> None:
        inactive = FakeUser(
            id=2,
            email="b@b.com",
            display_name="B",
            is_active=False,
            email_verified=True,
        )
        svc = _build_auth_service_with_user(inactive)
        client = _build_app(svc)
        client.cookies.set("session_id", "sess-123")
        resp = client.get("/api/entries")
        assert resp.status_code == 403
        assert resp.json()["message"] == "Account is disabled"


class TestEmailVerification:
    """Unverified email enforcement."""

    def test_unverified_blocked_on_protected_path(self) -> None:
        unverified = FakeUser(
            id=3,
            email="c@c.com",
            display_name="C",
            email_verified=False,
        )
        svc = _build_auth_service_with_user(unverified)
        client = _build_app(svc)
        client.cookies.set("session_id", "sess-123")
        resp = client.get("/api/entries")
        assert resp.status_code == 403
        assert resp.json()["message"] == "Please verify your email"

    def test_unverified_can_access_me(self) -> None:
        unverified = FakeUser(
            id=3,
            email="c@c.com",
            display_name="C",
            email_verified=False,
        )
        svc = _build_auth_service_with_user(unverified)
        client = _build_app(svc)
        client.cookies.set("session_id", "sess-123")
        resp = client.get("/api/auth/me")
        assert resp.status_code == 200

    def test_unverified_can_access_logout(self) -> None:
        unverified = FakeUser(
            id=3,
            email="c@c.com",
            display_name="C",
            email_verified=False,
        )
        svc = _build_auth_service_with_user(unverified)
        client = _build_app(svc)
        client.cookies.set("session_id", "sess-123")
        resp = client.post("/api/auth/logout")
        assert resp.status_code == 200

    def test_unverified_can_access_resend_verification(self) -> None:
        unverified = FakeUser(
            id=3,
            email="c@c.com",
            display_name="C",
            email_verified=False,
        )
        svc = _build_auth_service_with_user(unverified)
        client = _build_app(svc)
        client.cookies.set("session_id", "sess-123")
        resp = client.post("/api/auth/resend-verification")
        assert resp.status_code == 200

    def test_verified_user_passes_through(self) -> None:
        svc = _build_auth_service_with_user()  # default user is verified
        client = _build_app(svc)
        client.cookies.set("session_id", "sess-123")
        resp = client.get("/api/entries")
        assert resp.status_code == 200


class TestInactiveBeforeUnverified:
    """Inactive check takes priority over unverified check."""

    def test_inactive_and_unverified_gets_disabled_message(self) -> None:
        user = FakeUser(
            id=4,
            email="d@d.com",
            display_name="D",
            is_active=False,
            email_verified=False,
        )
        svc = _build_auth_service_with_user(user)
        client = _build_app(svc)
        client.cookies.set("session_id", "sess-123")
        resp = client.get("/api/entries")
        assert resp.status_code == 403
        assert resp.json()["message"] == "Account is disabled"


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestGetAuthenticatedUser:
    def test_returns_user_when_authenticated(self) -> None:
        from starlette.testclient import TestClient

        async def handler(request: Request) -> JSONResponse:
            user = get_authenticated_user(request)
            return JSONResponse({"user_id": user.user_id})

        svc = _build_auth_service_with_user()
        inner = Starlette(routes=[Route("/test", handler, methods=["GET"])])
        app = build_auth_middleware_stack(inner, svc)  # type: ignore[arg-type]
        client = TestClient(app, raise_server_exceptions=False)
        client.cookies.set("session_id", "sess-123")
        resp = client.get("/test")
        assert resp.status_code == 200
        assert resp.json()["user_id"] == 1

    def test_raises_when_not_authenticated(self) -> None:
        from starlette.testclient import TestClient

        async def handler(request: Request) -> JSONResponse:
            user = get_authenticated_user(request)
            return JSONResponse({"user_id": user.user_id})

        svc = FakeAuthService()
        inner = Starlette(routes=[Route("/test", handler, methods=["GET"])])
        # Use only the AuthenticationMiddleware (not RequireAuth) so the
        # request reaches the handler as unauthenticated.
        from starlette.middleware.authentication import AuthenticationMiddleware

        from journal.auth import SessionOrKeyBackend, _on_auth_error

        backend = SessionOrKeyBackend(svc)  # type: ignore[arg-type]
        app = AuthenticationMiddleware(
            inner, backend=backend, on_error=_on_auth_error
        )
        client = TestClient(app, raise_server_exceptions=True)
        with pytest.raises(RuntimeError, match="No authenticated user"):
            client.get("/test")


class TestSessionCookieHelpers:
    def test_set_session_cookie(self) -> None:
        resp = JSONResponse({"ok": True})
        result = set_session_cookie(resp, "sess-abc", max_age=3600)
        assert result is resp  # returns the same response object

        # Check the Set-Cookie header was added
        cookie_header = resp.headers.get("set-cookie", "")
        assert "session_id=sess-abc" in cookie_header
        assert "httponly" in cookie_header.lower()
        assert "secure" in cookie_header.lower()
        assert "samesite=lax" in cookie_header.lower()
        assert "max-age=3600" in cookie_header.lower()
        assert "path=/" in cookie_header.lower()

    def test_set_session_cookie_default_max_age(self) -> None:
        resp = JSONResponse({"ok": True})
        set_session_cookie(resp, "sess-xyz")
        cookie_header = resp.headers.get("set-cookie", "")
        assert "max-age=604800" in cookie_header.lower()

    def test_clear_session_cookie(self) -> None:
        resp = JSONResponse({"ok": True})
        result = clear_session_cookie(resp)
        assert result is resp
        # Starlette's delete_cookie sets max-age=0 or uses expires in the past
        cookie_header = resp.headers.get("set-cookie", "")
        assert "session_id=" in cookie_header
        # The cookie should be expired (max-age=0 or empty value)
        assert 'max-age=0' in cookie_header.lower() or '"0"' in cookie_header


# ---------------------------------------------------------------------------
# Path set completeness tests
# ---------------------------------------------------------------------------


class TestPathSets:
    def test_public_paths_frozen(self) -> None:
        assert isinstance(PUBLIC_PATHS, frozenset)

    def test_verification_exempt_paths_frozen(self) -> None:
        assert isinstance(VERIFICATION_EXEMPT_PATHS, frozenset)

    def test_expected_public_paths(self) -> None:
        expected = {
            "/health",
            "/api/auth/login",
            "/api/auth/register",
            "/api/auth/config",
            "/api/auth/forgot-password",
            "/api/auth/verify-reset-token",
            "/api/auth/reset-password",
            "/api/auth/verify-email",
        }
        assert expected == PUBLIC_PATHS

    def test_expected_verification_exempt_paths(self) -> None:
        expected = {
            "/api/auth/me",
            "/api/auth/logout",
            "/api/auth/resend-verification",
        }
        assert expected == VERIFICATION_EXEMPT_PATHS
