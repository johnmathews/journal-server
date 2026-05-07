"""Core auth routes: session lifecycle + current-user reads + public auth-config flag.

Four routes:

- ``POST /api/auth/login`` — authenticate, issue session cookie.
- ``POST /api/auth/logout`` — clear the session cookie and revoke server-side state.
- ``GET  /api/auth/me`` — return the currently authenticated user.
- ``GET  /api/auth/config`` — return the public registration-enabled flag (no auth required).

Profile mutations (``PATCH /api/auth/me``) live in ``profile.py``; account
lifecycle (register / verify / reset) lives in ``account.py``. The auth-config
flag is here rather than in account.py because it's a session/auth
infrastructure read — even though it's anonymous and the registration toggle
is read before ``/register`` is called.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

from journal.api import _runtime_get
from journal.auth import clear_session_cookie, get_authenticated_user, set_session_cookie
from journal.auth_api._shared import _services_or_503, _user_to_dict

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.services.auth import AuthService

log = logging.getLogger(__name__)


def register_core_routes(
    mcp: FastMCP,
    services_getter: Callable[[], dict | None],
) -> None:
    """Register session + current-user + auth-config routes on the MCP server."""

    # ── POST /api/auth/login ───────────────────────────────────────────

    @mcp.custom_route("/api/auth/login", methods=["POST"], name="api_auth_login")
    async def auth_login(request: Request) -> JSONResponse:
        """Authenticate with email + password, return user JSON + session cookie."""
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        auth_service: AuthService = services["auth_service"]

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": "invalid_body", "message": "Invalid JSON body"},
                status_code=400,
            )

        email = body.get("email", "").strip()
        password = body.get("password", "")

        if not email or not password:
            return JSONResponse(
                {"error": "missing_fields", "message": "Email and password are required"},
                status_code=400,
            )

        try:
            user = auth_service.authenticate(email, password)
        except ValueError as exc:
            log.info("Login failed for %s: %s", email, exc)
            return JSONResponse(
                {"error": "invalid_credentials", "message": str(exc)},
                status_code=401,
            )

        session_id = auth_service.create_session(
            user.id,
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )

        response = JSONResponse({"user": _user_to_dict(user)})
        set_session_cookie(response, session_id)
        log.info("Login succeeded for user %d (%s)", user.id, user.email)
        return response

    # ── POST /api/auth/logout ──────────────────────────────────────────

    @mcp.custom_route("/api/auth/logout", methods=["POST"], name="api_auth_logout")
    async def auth_logout(request: Request) -> JSONResponse:
        """Log out the current session."""
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        auth_service: AuthService = services["auth_service"]
        session_id = request.cookies.get("session_id")

        if session_id:
            auth_service.logout(session_id)

        response = JSONResponse({"ok": True})
        clear_session_cookie(response)
        return response

    # ── GET /api/auth/me ───────────────────────────────────────────────

    @mcp.custom_route("/api/auth/me", methods=["GET"], name="api_auth_me")
    async def auth_me(request: Request) -> JSONResponse:
        """Return the currently authenticated user."""
        user = get_authenticated_user(request)
        return JSONResponse({"user": _user_to_dict(user)})

    # ── GET /api/auth/config ───────────────────────────────────────────

    @mcp.custom_route("/api/auth/config", methods=["GET"], name="api_auth_config")
    async def auth_config(request: Request) -> JSONResponse:
        """Return public auth configuration (no auth required)."""
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        reg = _runtime_get(services, "registration_enabled")
        return JSONResponse({"registration_enabled": reg})
