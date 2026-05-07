"""Profile-mutation routes.

Currently a single route — ``PATCH /api/auth/me`` — that updates the
current user's display name. Lives in its own module rather than folded
into ``core.py`` so that "where do profile mutations live?" is answerable
on first grep, and so future profile fields (avatar, locale, timezone, …)
have an obvious home without growing core.py.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

from journal.auth import get_authenticated_user
from journal.auth_api._shared import _services_or_503, _user_to_dict

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.db.user_repository import SQLiteUserRepository

log = logging.getLogger(__name__)


def register_profile_routes(
    mcp: FastMCP,
    services_getter: Callable[[], dict | None],
) -> None:
    """Register profile-mutation routes on the MCP server."""

    # ── PATCH /api/auth/me ────────────────────────────────────────────

    @mcp.custom_route("/api/auth/me", methods=["PATCH"], name="api_auth_me_update")
    async def auth_me_update(request: Request) -> JSONResponse:
        """Update the currently authenticated user's profile (display_name)."""
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        user_repo: SQLiteUserRepository = services["user_repo"]
        user = get_authenticated_user(request)

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": "invalid_body", "message": "Invalid JSON body"},
                status_code=400,
            )

        display_name = (
            body.get("display_name", "").strip()
            if isinstance(body.get("display_name"), str)
            else ""
        )
        if not display_name:
            return JSONResponse(
                {
                    "error": "missing_fields",
                    "message": "display_name is required and must be non-empty",
                },
                status_code=400,
            )

        updated = user_repo.update_user(user.user_id, display_name=display_name)
        if updated is None:
            return JSONResponse(
                {"error": "not_found", "message": "User not found"},
                status_code=404,
            )

        log.info("User %d updated display_name to %r", user.user_id, display_name)
        return JSONResponse({"user": _user_to_dict(updated)})
