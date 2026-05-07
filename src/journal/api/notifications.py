"""Notification routes (Pushover-backed).

- ``GET /api/notifications/topics`` — topics with per-user toggle state.
- ``GET /api/notifications/status`` — whether the user has Pushover credentials.
- ``POST /api/notifications/validate`` — validate credentials and save them.
- ``POST /api/notifications/test`` — send a test notification.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

from journal.auth import get_authenticated_user

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

log = logging.getLogger(__name__)


def register_notifications_routes(
    mcp: FastMCP,
    services_getter: Callable[[], dict | None],
) -> None:
    """Register /api/notifications/* routes."""

    @mcp.custom_route(
        "/api/notifications/topics",
        methods=["GET"],
        name="api_notif_topics",
    )
    async def get_notification_topics(request: Request) -> JSONResponse:
        """Return notification topics with the user's current toggle state."""
        user = get_authenticated_user(request)
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        notif = services.get("notification_service")
        if notif is None:
            return JSONResponse({"error": "Notification service not configured"}, status_code=503)
        topics = notif.get_topics_for_user(user.user_id, user.is_admin)
        return JSONResponse({"topics": topics})

    @mcp.custom_route(
        "/api/notifications/status",
        methods=["GET"],
        name="api_notif_status",
    )
    async def get_notification_status(request: Request) -> JSONResponse:
        """Return whether the user has Pushover credentials configured."""
        user = get_authenticated_user(request)
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        notif = services.get("notification_service")
        if notif is None:
            return JSONResponse({"configured": False})
        return JSONResponse({"configured": notif.has_credentials(user.user_id)})

    @mcp.custom_route(
        "/api/notifications/validate",
        methods=["POST"],
        name="api_notif_validate",
    )
    async def validate_notification_credentials(request: Request) -> JSONResponse:
        """Validate Pushover credentials and save them if valid."""
        user = get_authenticated_user(request)
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        notif = services.get("notification_service")
        if notif is None:
            return JSONResponse({"error": "Notification service not configured"}, status_code=503)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        user_key = body.get("user_key", "")
        app_token = body.get("app_token", "")
        if not user_key or not app_token:
            return JSONResponse(
                {"valid": False, "error": "Both user_key and app_token are required"},
                status_code=400,
            )

        result = notif.validate_credentials(user_key, app_token)
        if result.sent:
            # Credentials valid — save them to user preferences
            user_repo = services["user_repo"]
            user_repo.set_preference(user.user_id, "pushover_user_key", user_key)
            user_repo.set_preference(user.user_id, "pushover_app_token", app_token)
            log.info(
                "POST /api/notifications/validate — valid, saved for user %d",
                user.user_id,
            )

        return JSONResponse({
            "valid": result.sent,
            "error": result.error,
        })

    @mcp.custom_route(
        "/api/notifications/test",
        methods=["POST"],
        name="api_notif_test",
    )
    async def send_test_notification(request: Request) -> JSONResponse:
        """Send a test Pushover notification using the user's saved credentials."""
        user = get_authenticated_user(request)
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        notif = services.get("notification_service")
        if notif is None:
            return JSONResponse({"error": "Notification service not configured"}, status_code=503)

        result = notif.send_test_notification(user.user_id)
        if not result.sent and result.error == "No Pushover credentials configured":
            return JSONResponse(
                {"sent": False, "error": result.error}, status_code=400,
            )
        return JSONResponse({
            "sent": result.sent,
            "error": result.error,
        })
