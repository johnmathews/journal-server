"""Per-user preference routes.

- ``GET  /api/users/me/preferences`` — list preferences for the authenticated user.
- ``PATCH /api/users/me/preferences`` — partial update, key/value JSON body.

Admin-only preference keys (sourced from ``journal.services.notifications.TOPICS``)
return 403 when set by a non-admin caller.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

from journal.api._handler import JsonBody, handler
from journal.auth import get_authenticated_user

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.service_registry import ServicesDict

log = logging.getLogger(__name__)


def register_users_routes(
    mcp: FastMCP,
    services_getter: Callable[[], ServicesDict | None],
) -> None:
    """Register /api/users/me/preferences GET and PATCH."""

    @mcp.custom_route(
        "/api/users/me/preferences",
        methods=["GET"],
        name="api_preferences_get",
    )
    @handler(services_getter)
    def get_preferences(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        """Return all preferences for the authenticated user."""
        user = get_authenticated_user(request)
        user_repo = services["user_repo"]
        prefs = user_repo.get_preferences(user.user_id)
        return JSONResponse({"preferences": prefs})

    @mcp.custom_route(
        "/api/users/me/preferences",
        methods=["PATCH"],
        name="api_preferences_patch",
    )
    @handler(services_getter, parse_json=JsonBody(invalid_error="Invalid JSON"))
    def patch_preferences(
        request: Request, services: ServicesDict, body: dict
    ) -> JSONResponse:
        """Update one or more preferences for the authenticated user.

        Body: ``{"key": value, ...}`` — each key is a preference name,
        value is any JSON-serialisable object.
        """
        user = get_authenticated_user(request)
        user_repo = services["user_repo"]

        # Admin-only notification topics cannot be set by non-admin users.
        from journal.services.notifications import TOPICS
        _admin_only_keys = {t["key"] for t in TOPICS if t["admin_only"]}

        for key, value in body.items():
            if not isinstance(key, str):
                return JSONResponse(
                    {"error": "Preference keys must be strings"}, status_code=400,
                )
            if key in _admin_only_keys and not user.is_admin:
                return JSONResponse(
                    {"error": f"Preference {key!r} requires admin"}, status_code=403,
                )
            user_repo.set_preference(user.user_id, key, value)

        log.info(
            "PATCH /api/users/me/preferences — updated %s for user %d",
            list(body.keys()),
            user.user_id,
        )
        prefs = user_repo.get_preferences(user.user_id)
        return JSONResponse({"preferences": prefs})
