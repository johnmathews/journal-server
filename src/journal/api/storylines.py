"""REST API endpoints for storylines (read-side).

Layout follows the URL-prefix routing rule:

* ``GET /api/storylines`` — list (paginated, scoped to caller)
* ``GET /api/storylines/{id}`` — single storyline + both panels

The write/job-creation routes (``POST /api/storylines``,
``POST /api/storylines/{id}/regenerate``, ``DELETE /api/storylines/{id}``)
live in ``ingestion.py`` per the project's routing convention.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse

from journal.auth import get_authenticated_user

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.db.storyline_repository import SQLiteStorylineRepository
    from journal.models import Storyline, StorylinePanel

log = logging.getLogger(__name__)


def register_storylines_routes(
    mcp: FastMCP,
    services_getter: Callable[[], dict | None],
) -> None:
    """Register the read-side storylines routes."""

    @mcp.custom_route(
        "/api/storylines", methods=["GET"], name="api_list_storylines",
    )
    async def list_storylines(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503,
            )
        repo: SQLiteStorylineRepository | None = services.get(
            "storyline_repository",
        )
        if repo is None:
            return JSONResponse(
                {"error": "Storylines feature not configured"},
                status_code=503,
            )
        user = get_authenticated_user(request)
        try:
            limit = int(request.query_params.get("limit", "50"))
            offset = int(request.query_params.get("offset", "0"))
        except ValueError:
            return JSONResponse(
                {"error": "limit and offset must be integers"},
                status_code=400,
            )
        status = request.query_params.get("status")
        rows = repo.list_storylines(
            user_id=user.user_id, status=status,
            limit=limit, offset=offset,
        )
        total = repo.count_storylines(user_id=user.user_id, status=status)
        return JSONResponse({
            "items": [_storyline_to_dict(s) for s in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        })

    @mcp.custom_route(
        "/api/storylines/{storyline_id:int}",
        methods=["GET"], name="api_storyline_detail",
    )
    async def storyline_detail(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503,
            )
        repo: SQLiteStorylineRepository | None = services.get(
            "storyline_repository",
        )
        if repo is None:
            return JSONResponse(
                {"error": "Storylines feature not configured"},
                status_code=503,
            )
        user = get_authenticated_user(request)
        sid = int(request.path_params["storyline_id"])
        storyline = repo.get_storyline(sid, user_id=user.user_id)
        if storyline is None:
            return JSONResponse({"error": "Storyline not found"}, status_code=404)
        panels = repo.list_panels(storyline.id)
        return JSONResponse({
            **_storyline_to_dict(storyline),
            "panels": {
                p.panel_kind: _panel_to_dict(p) for p in panels
            },
        })


def _storyline_to_dict(s: Storyline) -> dict[str, Any]:
    return {
        "id": s.id,
        "user_id": s.user_id,
        "entity_id": s.entity_id,
        "name": s.name,
        "description": s.description,
        "start_date": s.start_date,
        "end_date": s.end_date,
        "status": s.status,
        "last_generated_at": s.last_generated_at,
        "last_extension_check_at": s.last_extension_check_at,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
    }


def _panel_to_dict(p: StorylinePanel) -> dict[str, Any]:
    return {
        "panel_kind": p.panel_kind,
        "segments": p.segments,
        "source_entry_ids": p.source_entry_ids,
        "citation_count": p.citation_count,
        "model_used": p.model_used,
        "generated_at": p.generated_at,
    }
