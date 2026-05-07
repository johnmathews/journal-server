"""Admin-only routes: user management + dynamic-config reloads.

Six routes, all gated by ``user.is_admin``:

User management (Cluster E):

- ``GET   /api/admin/users`` — list users with stats.
- ``PATCH /api/admin/users/{id}`` — update is_admin / is_active flags.

Dynamic reloads (Cluster F) — operator-triggered re-reads of file-backed
config that the server otherwise reads only at startup:

- ``POST /api/admin/reload/ocr-context``
- ``POST /api/admin/reload/transcription-context``
- ``POST /api/admin/reload/mood-dimensions``
- ``POST /api/admin/reload/entity-casing``

Each reload route returns the helper's summary dict; admin uses it to
confirm the reload landed. ``RuntimeError`` from the mood-dimensions helper
(raised when the feature is disabled) is converted to a 409.

Both clusters share the admin gating pattern, the file stays under 250
lines, and splitting further would be over-fragmentation. If reload
endpoints grow into their own subsystem (rate-limited, audit-logged, etc.)
split then.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

from journal.auth import get_authenticated_user
from journal.auth_api._shared import _services_or_503, _user_to_dict
from journal.services.reload import (
    reload_entity_casing_exceptions,
    reload_mood_dimensions,
    reload_ocr_provider,
    reload_transcription_provider,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.config import Config
    from journal.db.user_repository import SQLiteUserRepository

log = logging.getLogger(__name__)


def register_admin_routes(
    mcp: FastMCP,
    services_getter: Callable[[], dict | None],
) -> None:
    """Register admin-only REST API routes on the MCP server."""

    # ── GET /api/admin/users ───────────────────────────────────────────

    @mcp.custom_route("/api/admin/users", methods=["GET"], name="api_admin_users_list")
    async def admin_list_users(request: Request) -> JSONResponse:
        """List all users with stats (admin only)."""
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        user = get_authenticated_user(request)
        if not user.is_admin:
            return JSONResponse(
                {"error": "forbidden", "message": "Admin access required"},
                status_code=403,
            )

        user_repo: SQLiteUserRepository = services["user_repo"]
        stats = user_repo.get_user_stats()

        log.info("Admin user %d listed %d users", user.user_id, len(stats))
        return JSONResponse({"items": stats})

    # ── PATCH /api/admin/users/{id} ────────────────────────────────────

    @mcp.custom_route(
        "/api/admin/users/{user_id:int}",
        methods=["PATCH"],
        name="api_admin_user_update",
    )
    async def admin_update_user(request: Request) -> JSONResponse:
        """Update a user's admin/active flags (admin only)."""
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        admin_user = get_authenticated_user(request)
        if not admin_user.is_admin:
            return JSONResponse(
                {"error": "forbidden", "message": "Admin access required"},
                status_code=403,
            )

        user_repo: SQLiteUserRepository = services["user_repo"]
        target_user_id = int(request.path_params["user_id"])

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": "invalid_body", "message": "Invalid JSON body"},
                status_code=400,
            )

        # Only allow updating is_active and is_admin
        allowed_fields: dict[str, type] = {"is_active": bool, "is_admin": bool}
        update_kwargs: dict[str, bool] = {}
        for field_name, field_type in allowed_fields.items():
            if field_name in body:
                value = body[field_name]
                if not isinstance(value, field_type):
                    return JSONResponse(
                        {
                            "error": "invalid_field",
                            "message": f"{field_name} must be a boolean",
                        },
                        status_code=400,
                    )
                update_kwargs[field_name] = value

        if not update_kwargs:
            return JSONResponse(
                {
                    "error": "missing_fields",
                    "message": "At least one of is_active or is_admin is required",
                },
                status_code=400,
            )

        updated_user = user_repo.update_user(target_user_id, **update_kwargs)
        if updated_user is None:
            return JSONResponse(
                {"error": "not_found", "message": "User not found"},
                status_code=404,
            )

        log.info(
            "Admin user %d updated user %d: %s",
            admin_user.user_id,
            target_user_id,
            update_kwargs,
        )
        return JSONResponse({"user": _user_to_dict(updated_user)})

    # ── POST /api/admin/reload/{resource} ──────────────────────────────

    def _reload_endpoint(
        request: Request,
        helper: Callable[[dict, Config], dict],
        label: str,
    ) -> JSONResponse:
        """Common scaffolding for the three reload endpoints.

        Resolves services + admin user, runs the helper, and converts
        ``RuntimeError`` (raised by the mood-dimensions helper when the
        feature is disabled) into a 409.
        """
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        user = get_authenticated_user(request)
        if not user.is_admin:
            return JSONResponse(
                {"error": "forbidden", "message": "Admin access required"},
                status_code=403,
            )

        try:
            summary = helper(services, services["config"])
        except RuntimeError as e:
            return JSONResponse(
                {"error": "reload_unavailable", "message": str(e)},
                status_code=409,
            )

        log.info(
            "Admin user %d reloaded %s: %s", user.user_id, label, summary,
        )
        return JSONResponse(summary)

    @mcp.custom_route(
        "/api/admin/reload/ocr-context",
        methods=["POST"],
        name="api_admin_reload_ocr_context",
    )
    async def admin_reload_ocr_context(request: Request) -> JSONResponse:
        """Re-read the OCR glossary directory and rebuild the OCR provider."""
        return _reload_endpoint(request, reload_ocr_provider, "ocr-context")

    @mcp.custom_route(
        "/api/admin/reload/transcription-context",
        methods=["POST"],
        name="api_admin_reload_transcription_context",
    )
    async def admin_reload_transcription_context(request: Request) -> JSONResponse:
        """Re-read the OCR glossary directory and rebuild the transcription stack."""
        return _reload_endpoint(
            request, reload_transcription_provider, "transcription-context",
        )

    @mcp.custom_route(
        "/api/admin/reload/mood-dimensions",
        methods=["POST"],
        name="api_admin_reload_mood_dimensions",
    )
    async def admin_reload_mood_dimensions(request: Request) -> JSONResponse:
        """Re-read the mood-dimensions TOML and rebuild the mood scoring service."""
        return _reload_endpoint(
            request, reload_mood_dimensions, "mood-dimensions",
        )

    @mcp.custom_route(
        "/api/admin/reload/entity-casing",
        methods=["POST"],
        name="api_admin_reload_entity_casing",
    )
    async def admin_reload_entity_casing(request: Request) -> JSONResponse:
        """Re-read the entity-casing exceptions TOML and rebind it on the entity store."""
        return _reload_endpoint(
            request, reload_entity_casing_exceptions, "entity-casing",
        )
