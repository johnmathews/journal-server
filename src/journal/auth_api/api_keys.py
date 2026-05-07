"""API key CRUD routes.

Three operations on programmatic credentials owned by the current user:

- ``POST /api/auth/api-keys`` — generate a new key (returned exactly once).
- ``GET  /api/auth/api-keys`` — list the user's keys (no secret material).
- ``DELETE /api/auth/api-keys/{id}`` — revoke a key.

The POST/GET pair shares a single ``@mcp.custom_route`` registration with
in-handler method dispatch; this matches the original module's shape so
existing route names (``api_auth_api_keys`` / ``api_auth_api_key_revoke``)
stay stable.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

from journal.auth import get_authenticated_user
from journal.auth_api._shared import _api_key_info_to_dict, _services_or_503

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.services.auth import AuthService

log = logging.getLogger(__name__)


def register_api_keys_routes(
    mcp: FastMCP,
    services_getter: Callable[[], dict | None],
) -> None:
    """Register API key CRUD routes on the MCP server."""

    # ── POST /api/auth/api-keys ────────────────────────────────────────

    @mcp.custom_route(
        "/api/auth/api-keys",
        methods=["POST", "GET"],
        name="api_auth_api_keys",
    )
    async def auth_api_keys(request: Request) -> JSONResponse:
        """Create (POST) or list (GET) API keys for the authenticated user."""
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        auth_service: AuthService = services["auth_service"]
        user = get_authenticated_user(request)

        if request.method == "POST":
            return await _create_api_key(request, auth_service, user.user_id)
        else:
            return _list_api_keys(auth_service, user.user_id)

    async def _create_api_key(
        request: Request,
        auth_service: AuthService,
        user_id: int,
    ) -> JSONResponse:
        """Handle POST /api/auth/api-keys — generate a new API key."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": "invalid_body", "message": "Invalid JSON body"},
                status_code=400,
            )

        name = body.get("name", "").strip()
        if not name:
            return JSONResponse(
                {"error": "missing_fields", "message": "API key name is required"},
                status_code=400,
            )

        expires_days: int | None = body.get("expires_days")
        if expires_days is not None:
            try:
                expires_days = int(expires_days)
                if expires_days < 1:
                    raise ValueError("expires_days must be positive")
            except (TypeError, ValueError):
                return JSONResponse(
                    {
                        "error": "invalid_field",
                        "message": "expires_days must be a positive integer",
                    },
                    status_code=400,
                )

        full_key, key_info = auth_service.create_api_key(user_id, name, expires_days)

        response_data = _api_key_info_to_dict(key_info)
        response_data["key"] = full_key  # Full key shown exactly once

        log.info("Created API key '%s' for user %d", name, user_id)
        return JSONResponse(response_data, status_code=201)

    def _list_api_keys(auth_service: AuthService, user_id: int) -> JSONResponse:
        """Handle GET /api/auth/api-keys — list all API keys for the user."""
        keys = auth_service.list_api_keys(user_id)
        return JSONResponse({"items": [_api_key_info_to_dict(k) for k in keys]})

    # ── DELETE /api/auth/api-keys/{id} ─────────────────────────────────

    @mcp.custom_route(
        "/api/auth/api-keys/{key_id:int}",
        methods=["DELETE"],
        name="api_auth_api_key_revoke",
    )
    async def auth_api_key_revoke(request: Request) -> JSONResponse:
        """Revoke an API key owned by the authenticated user."""
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        auth_service: AuthService = services["auth_service"]
        user = get_authenticated_user(request)
        key_id = int(request.path_params["key_id"])

        revoked = auth_service.revoke_api_key(key_id, user.user_id)
        if not revoked:
            return JSONResponse(
                {"error": "not_found", "message": "API key not found or already revoked"},
                status_code=404,
            )

        log.info("Revoked API key %d for user %d", key_id, user.user_id)
        return JSONResponse({"ok": True})
