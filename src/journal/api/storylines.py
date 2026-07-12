"""REST API endpoints for storylines (read-side).

Layout follows the URL-prefix routing rule:

* ``GET /api/storylines`` — list (paginated, scoped to caller)
* ``GET /api/storylines/{id}`` — storyline summary + chapter meta
* ``GET /api/storylines/{id}/chapters/{cid}`` — chapter detail
  (narrative segments + addenda)

The write/job-creation routes (``POST /api/storylines``, ``PATCH
/api/storylines/{id}``, ``DELETE /api/storylines/{id}``, ``PUT
/api/storylines/{id}/anchors``, refresh/unpublish/read-state) live in
``storylines_write.py`` per the project's routing convention.

Each storyline has exactly one ``draft`` chapter (always last by
``seq``) plus zero or more ``published`` chapters — see
``db/storyline_repository.py`` for the schema notes. This module
carries the serializers (``_storyline_to_dict`` / ``_chapter_*_to_dict``
/ ``_anchors_for``) shared with ``storylines_write.py``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse

from journal.api._handler import handler
from journal.auth import get_authenticated_user

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.db.storyline_repository import SQLiteStorylineRepository
    from journal.models import Storyline, StorylineChapter
    from journal.service_registry import ServicesDict

log = logging.getLogger(__name__)


def register_storylines_routes(
    mcp: FastMCP,
    services_getter: Callable[[], ServicesDict | None],
) -> None:
    """Register the read-side storylines routes."""

    @mcp.custom_route(
        "/api/storylines", methods=["GET"], name="api_list_storylines",
    )
    @handler(services_getter)
    def list_storylines(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
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
        entity_store = services.get("entity_store")
        # Batch fetch unread and chapter counts — avoids N+1 queries
        unread = repo.unread_counts(user.user_id)
        chapter_counts = repo.chapter_counts(user.user_id)
        return JSONResponse({
            "items": [
                _storyline_to_dict(
                    s,
                    _anchors_for(repo, entity_store, s.id),
                    unread.get(s.id, 0),
                    chapter_counts.get(s.id, 0),
                )
                for s in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        })

    @mcp.custom_route(
        "/api/storylines/{storyline_id:int}",
        methods=["GET"], name="api_storyline_detail",
    )
    @handler(services_getter)
    def storyline_detail(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
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
        chapters = repo.list_chapters(sid)  # seq ASC — draft naturally last
        entity_store = services.get("entity_store")
        anchors = _anchors_for(repo, entity_store, sid)
        unread_count = _unread_count(chapters)
        return JSONResponse({
            **_storyline_to_dict(storyline, anchors, unread_count, len(chapters)),
            "chapters": [_chapter_meta_to_dict(c) for c in chapters],
        })

    @mcp.custom_route(
        "/api/storylines/{storyline_id:int}/chapters/{chapter_id:int}",
        methods=["GET"], name="api_storyline_chapter_detail",
    )
    @handler(services_getter)
    def storyline_chapter_detail(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
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
        cid = int(request.path_params["chapter_id"])
        storyline = repo.get_storyline(sid, user_id=user.user_id)
        if storyline is None:
            return JSONResponse({"error": "Storyline not found"}, status_code=404)
        chapter = repo.get_chapter(cid)
        if chapter is None or chapter.storyline_id != sid:
            return JSONResponse({"error": "Chapter not found"}, status_code=404)
        return JSONResponse(_chapter_detail_to_dict(chapter))


def _unread_count(chapters: list[StorylineChapter]) -> int:
    return sum(1 for c in chapters if c.state == "published" and c.read_at is None)


def _anchors_for(
    repo: SQLiteStorylineRepository,
    entity_store: Any,
    storyline_id: int,
) -> list[dict[str, Any]]:
    """Build the ``anchors`` field for an API response.

    Returns ``[{entity_id, canonical_name}, ...]`` sorted by entity id
    ASC. Missing entities (deleted out from under the storyline)
    render as an empty canonical_name so the response shape stays
    stable.
    """
    anchor_ids = repo.list_anchors(storyline_id)
    out: list[dict[str, Any]] = []
    for entity_id in anchor_ids:
        if entity_store is not None:
            entity = entity_store.get_entity(entity_id)
            canonical_name = entity.canonical_name if entity else ""
        else:
            canonical_name = ""
        out.append({"entity_id": entity_id, "canonical_name": canonical_name})
    return out


def _storyline_to_dict(
    s: Storyline,
    anchors: list[dict[str, Any]],
    unread_count: int,
    chapter_count: int,
) -> dict[str, Any]:
    return {
        "id": s.id,
        "name": s.name,
        "description": s.description,
        "status": s.status,
        "anchors": anchors,
        "unread_count": unread_count,
        "chapter_count": chapter_count,
        "updated_at": s.updated_at,
        "created_at": s.created_at,
    }


def _chapter_meta_to_dict(c: StorylineChapter) -> dict[str, Any]:
    return {
        "id": c.id,
        "seq": c.seq,
        "title": c.title,
        "state": c.state,
        "entry_count": c.entry_count,
        "first_entry_date": c.first_entry_date,
        "last_entry_date": c.last_entry_date,
        "published_at": c.published_at,
        "read_at": c.read_at,
        "citation_count": c.citation_count,
    }


def _chapter_detail_to_dict(c: StorylineChapter) -> dict[str, Any]:
    return {
        **_chapter_meta_to_dict(c),
        "segments": c.segments,
        "addenda": c.addenda,
        "model_used": c.model_used,
        "generated_at": c.generated_at,
    }
