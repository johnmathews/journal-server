"""Authentication middleware supporting cookie sessions and API key bearer tokens.

Two auth mechanisms, checked in order:
1. Cookie ``session_id`` -- used by the webapp (httpOnly, Secure, SameSite=Lax)
2. Header ``Authorization: Bearer <key>`` -- used by MCP and API clients

The middleware is applied in ``mcp_server.main()`` and covers every route.
Public paths (login, register, health) are exempt.
"""

from __future__ import annotations

import contextvars
import logging
from typing import TYPE_CHECKING

from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
)
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection, Request
    from starlette.responses import Response
    from starlette.types import ASGIApp, Receive, Scope, Send

    from journal.services.auth import AuthService

log = logging.getLogger(__name__)

# ContextVar holding the authenticated user_id for the current request.
# Set by RequireAuthMiddleware after successful auth, read by MCP tool
# helpers via get_current_user_id(). Defaults to None (unauthenticated).
_current_user_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "_current_user_id", default=None
)

# ---------------------------------------------------------------------------
# Exempt path sets
# ---------------------------------------------------------------------------

#: Paths that require no authentication at all.
PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/api/auth/login",
        "/api/auth/register",
        "/api/auth/config",
        "/api/auth/forgot-password",
        "/api/auth/verify-reset-token",
        "/api/auth/reset-password",
        "/api/auth/verify-email",
    }
)

#: Paths accessible by authenticated users who have NOT yet verified their
#: email. Everything else requires ``email_verified=True``.
VERIFICATION_EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/api/auth/me",
        "/api/auth/logout",
        "/api/auth/resend-verification",
    }
)


# ---------------------------------------------------------------------------
# User identity object
# ---------------------------------------------------------------------------


class AuthenticatedUser:
    """User identity attached to ``request.user`` by the auth middleware.

    This class intentionally does **not** inherit from Starlette's
    :class:`~starlette.authentication.BaseUser` because ``BaseUser``
    defines ``display_name`` as a ``@property``, which conflicts with
    storing a user-supplied display name as a regular attribute (or
    constructor parameter of the same name).  Starlette's
    :class:`~starlette.middleware.authentication.AuthenticationMiddleware`
    only checks for ``is_authenticated``; it does not require ``BaseUser``
    inheritance.
    """

    def __init__(
        self,
        user_id: int,
        email: str,
        display_name: str,
        is_admin: bool,
        is_active: bool,
        email_verified: bool,
    ) -> None:
        self.user_id = user_id
        self.email = email
        self.display_name = display_name
        self.is_admin = is_admin
        self.is_active = is_active
        self.email_verified = email_verified

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def identity(self) -> str:
        return str(self.user_id)


# ---------------------------------------------------------------------------
# Authentication backend (populates request.user)
# ---------------------------------------------------------------------------


class SessionOrKeyBackend(AuthenticationBackend):
    """Try cookie session first, then bearer token API key."""

    def __init__(self, auth_service: AuthService) -> None:
        self._auth = auth_service

    async def authenticate(
        self, conn: HTTPConnection
    ) -> tuple[AuthCredentials, AuthenticatedUser] | None:
        # 1. Try cookie session
        session_id = conn.cookies.get("session_id")
        if session_id:
            user = self._auth.validate_session(session_id)
            if user:
                return self._make_result(user)

        # 2. Try bearer token (API key)
        auth_header = conn.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            key = auth_header.removeprefix("Bearer ").strip()
            if key:
                user = self._auth.validate_api_key(key)
                if user:
                    return self._make_result(user)

        # No valid auth found -- return None (unauthenticated)
        return None

    @staticmethod
    def _make_result(
        user: object,
    ) -> tuple[AuthCredentials, AuthenticatedUser]:
        """Build the ``(AuthCredentials, AuthenticatedUser)`` pair from a
        user record returned by the auth service.

        The *user* object is duck-typed -- it must expose ``id``, ``email``,
        ``display_name``, ``is_admin``, ``is_active``, and
        ``email_verified`` attributes.
        """
        scopes: list[str] = ["authenticated"]
        if getattr(user, "is_admin", False):
            scopes.append("admin")
        return AuthCredentials(scopes), AuthenticatedUser(
            user_id=user.id,  # type: ignore[attr-defined]
            email=user.email,  # type: ignore[attr-defined]
            display_name=user.display_name,  # type: ignore[attr-defined]
            is_admin=user.is_admin,  # type: ignore[attr-defined]
            is_active=user.is_active,  # type: ignore[attr-defined]
            email_verified=user.email_verified,  # type: ignore[attr-defined]
        )


# ---------------------------------------------------------------------------
# Auth enforcement middleware (ASGI, wraps AuthenticationMiddleware)
# ---------------------------------------------------------------------------


def _on_auth_error(
    conn: HTTPConnection, exc: AuthenticationError
) -> JSONResponse:
    """Return a 401 JSON body when the backend raises
    :class:`AuthenticationError`."""
    return JSONResponse(
        {"error": "unauthorized", "message": str(exc)},
        status_code=401,
    )


class RequireAuthMiddleware:
    """Pure-ASGI middleware that enforces authentication on non-public paths.

    Must be applied as the **inner** layer, wrapped by Starlette's
    :class:`AuthenticationMiddleware`, so that ``scope["user"]`` is already
    populated when this middleware inspects it.

    Request flow::

        client -> CORS -> AuthenticationMiddleware -> RequireAuth -> route

    Enforcement rules (checked in order):

    1. OPTIONS requests always pass (CORS preflight).
    2. Paths in :data:`PUBLIC_PATHS` always pass.
    3. Unauthenticated requests receive a **401**.
    4. Authenticated but **inactive** users receive a **403**.
    5. Authenticated but **email-unverified** users receive a **403**
       unless the path is in :data:`VERIFICATION_EXEMPT_PATHS`.
    6. Everything else passes through.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        exempt_paths: frozenset[str] = PUBLIC_PATHS,
        verification_exempt_paths: frozenset[str] = VERIFICATION_EXEMPT_PATHS,
    ) -> None:
        self.app = app
        self._exempt_paths = exempt_paths
        self._verification_exempt_paths = verification_exempt_paths

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path: str = scope["path"]
        method: str = scope.get("method", "")

        # 1. OPTIONS always pass (CORS preflight)
        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        # 2. Public paths pass without auth
        if path in self._exempt_paths:
            await self.app(scope, receive, send)
            return

        # At this point, scope["user"] has been set by the inner
        # AuthenticationMiddleware.  If it wasn't (e.g. middleware ordering
        # bug), we treat the request as unauthenticated.
        user = scope.get("user")

        # 3. Unauthenticated
        if user is None or not getattr(user, "is_authenticated", False):
            response = JSONResponse(
                {"error": "unauthorized", "message": "Authentication required"},
                status_code=401,
            )
            await response(scope, receive, send)
            return

        # 4. Account disabled
        if not getattr(user, "is_active", True):
            response = JSONResponse(
                {"error": "forbidden", "message": "Account is disabled"},
                status_code=403,
            )
            await response(scope, receive, send)
            return

        # 5. Email not verified (unless on an exempt path)
        if (
            not getattr(user, "email_verified", True)
            and path not in self._verification_exempt_paths
        ):
            response = JSONResponse(
                {
                    "error": "forbidden",
                    "message": "Please verify your email",
                },
                status_code=403,
            )
            await response(scope, receive, send)
            return

        # 6. All checks passed — propagate user_id via contextvar so
        # MCP tool functions (which lack a Request object) can read it.
        token = _current_user_id.set(getattr(user, "user_id", None))
        try:
            await self.app(scope, receive, send)
        finally:
            _current_user_id.reset(token)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def get_current_user_id() -> int:
    """Return the user_id for the current request (set by auth middleware).

    Raises :class:`RuntimeError` if called outside an authenticated request.
    Intended for MCP tool functions that receive a ``Context`` instead of a
    ``Request``.
    """
    uid = _current_user_id.get()
    if uid is None:
        raise RuntimeError("No authenticated user in current context")
    return uid


def get_authenticated_user(request: Request) -> AuthenticatedUser:
    """Extract the authenticated user from *request*.

    Raises :class:`RuntimeError` if the request does not carry an
    :class:`AuthenticatedUser` (i.e. the middleware is misconfigured or
    the route should have been exempt).
    """
    user = request.user
    if not isinstance(user, AuthenticatedUser):
        raise RuntimeError("No authenticated user on request")
    return user


def set_session_cookie(
    response: Response,
    session_id: str,
    max_age: int = 604800,
) -> Response:
    """Set the ``session_id`` cookie on *response*.

    Parameters
    ----------
    response:
        Any Starlette ``Response``.
    session_id:
        The opaque session identifier.
    max_age:
        Cookie lifetime in seconds.  Defaults to **7 days** (604 800 s).
    """
    response.set_cookie(
        key="session_id",
        value=session_id,
        max_age=max_age,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


def clear_session_cookie(response: Response) -> Response:
    """Delete the ``session_id`` cookie from *response*."""
    response.delete_cookie(key="session_id", path="/")
    return response


def build_auth_middleware_stack(
    app: ASGIApp,
    auth_service: AuthService,
    *,
    exempt_paths: frozenset[str] = PUBLIC_PATHS,
    verification_exempt_paths: frozenset[str] = VERIFICATION_EXEMPT_PATHS,
) -> AuthenticationMiddleware:
    """Convenience factory that wires up both middleware layers in the
    correct order.

    Returns the outermost middleware, ready to be used as the ASGI app.

    Layer order (outer to inner)::

        AuthenticationMiddleware -> RequireAuthMiddleware -> route

    ``AuthenticationMiddleware`` runs first and populates ``scope["user"]``
    (either an :class:`AuthenticatedUser` or Starlette's
    ``UnauthenticatedUser``).  ``RequireAuthMiddleware`` then reads
    ``scope["user"]`` and enforces access rules before the request
    reaches the route handler.
    """
    require_auth = RequireAuthMiddleware(
        app,
        exempt_paths=exempt_paths,
        verification_exempt_paths=verification_exempt_paths,
    )
    backend = SessionOrKeyBackend(auth_service)
    return AuthenticationMiddleware(
        require_auth,
        backend=backend,
        on_error=_on_auth_error,
    )
