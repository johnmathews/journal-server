"""Storyline write/job-creation routes.

This module is part of the write/job-creation routing override documented
in ``code-quality-principles.md`` § "Routing rules for src/journal/api/".
The storyline write routes are job-creation routes (create kicks off a
bootstrap job; refresh/unpublish queue one explicitly), so they follow
the override rather than living beside the reads in ``api/storylines.py``.

Concretely, this module owns:

- ``POST /api/storylines``                                        — create + auto-queue bootstrap
- ``POST /api/storylines/{storyline_id}/refresh``                 — queue a draft re-narration job
- ``POST /api/storylines/{sid}/chapters/{cid}/read``               — mark a published chapter read
- ``POST /api/storylines/{sid}/chapters/{cid}/unread``             — mark a published chapter unread
- ``PATCH /api/storylines/{sid}/chapters/{cid}``                   — rename a chapter (title only)
- ``POST /api/storylines/{storyline_id}/chapters/unpublish``       — fold the newest published
                                                                      chapter back into the draft
- ``PATCH /api/storylines/{storyline_id}``                         — update editable metadata
                                                                      (name and/or status)
- ``DELETE /api/storylines/{storyline_id}``                        — delete a storyline
- ``PUT /api/storylines/{storyline_id}/anchors``                   — replace the anchor set

Read routes (``GET /api/storylines*``) live in ``api/storylines.py`` per
the default URL-resource rule.

**Rule for new routes.** If a new storyline route's primary effect is
"create a job that does work" or a long-running write, it goes here;
reads go in ``api/storylines.py``. New deviation categories require
updating ``code-quality-principles.md`` and ``api/_shared.py``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

from journal.api._handler import handler
from journal.api.storylines import (
    _anchors_for,
    _chapter_meta_to_dict,
    _storyline_to_dict,
    _unread_count,
)
from journal.auth import get_authenticated_user

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.service_registry import ServicesDict
    from journal.services.jobs import JobRunner

log = logging.getLogger(__name__)

#: Storylines are anchored on 1..MAX_ANCHORS entities. Previously lived
#: in ``services/storylines/service.py`` (deleted in the storylines
#: redesign — see docs/superpowers/specs/2026-07-12-storylines-redesign-design.md §8).
MAX_ANCHORS = 15

#: Statuses allowed by the ``storylines.status`` CHECK constraint
#: (``db/migrations/0027_storylines.sql`` and friends).
_VALID_STATUSES = ("active", "archived")


def register_storylines_write_routes(
    mcp: FastMCP,
    services_getter: Callable[[], ServicesDict | None],
) -> None:
    """Register the storyline write/job-creation routes (see module docstring)."""

    @mcp.custom_route(
        "/api/storylines",
        methods=["POST"],
        name="api_create_storyline",
    )
    @handler(services_getter, parse_json="raw")
    def create_storyline(
        request: Request, services: ServicesDict, raw: bytes
    ) -> JSONResponse:
        """Create a new storyline and queue its bootstrap job.

        Body: ``{entity_ids: list[int], name: str, description?: str}``.
        ``entity_ids`` must have 1..MAX_ANCHORS unique entries (current
        cap = 15). Returns 201 with ``{"storyline": <detail shape>,
        "bootstrap_job_id"}``. 409 if a storyline with the same name and
        the same exact anchor set already exists. 400 on malformed
        input; 422 if the anchor count is 0 or exceeds the cap. 503 if
        storylines are not wired.

        On successful create, immediately queues a ``storyline_update``
        bootstrap job so the new storyline's chapters start populating
        without a separate refresh call. A missing/unwired
        ``StorylineEngine`` is tolerated here (mirrors the historical
        behavior): the storyline is still created, ``bootstrap_job_id``
        is ``None``, and a warning is logged — the caller can retry via
        ``POST /{id}/refresh`` once the engine is wired.
        """
        repo = services.get("storyline_repository")
        job_runner: JobRunner | None = services.get("job_runner")
        if repo is None or job_runner is None:
            return JSONResponse(
                {"error": "Storylines feature not configured"},
                status_code=503,
            )
        entity_store = services.get("entity_store")
        user = get_authenticated_user(request)

        # Parse in-body ("raw" mode): the repo-503 check above must
        # keep precedence over body-shape 400s.
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "Request body must be a JSON object"},
                status_code=400,
            )
        entity_ids = body.get("entity_ids")
        name = (body.get("name") or "").strip()
        if (
            not isinstance(entity_ids, list)
            or not all(isinstance(e, int) for e in entity_ids)
            or not name
        ):
            return JSONResponse(
                {
                    "error": (
                        "entity_ids (list[int]) and name (str) are required"
                    ),
                },
                status_code=400,
            )
        unique_entity_ids = sorted(set(entity_ids))
        if not (1 <= len(unique_entity_ids) <= MAX_ANCHORS):
            return JSONResponse(
                {
                    "error": (
                        f"entity_ids must have 1..{MAX_ANCHORS} unique "
                        f"anchors (got {len(unique_entity_ids)})"
                    ),
                },
                status_code=422,
            )
        description = body.get("description", "") or ""

        # Refuse if a storyline with this exact name + anchor set
        # already exists for this user — caller can GET the existing
        # one or refresh it.
        existing = repo.find_by_anchor_set(
            user_id=user.user_id,
            entity_ids=unique_entity_ids,
            name=name,
        )
        if existing is not None:
            return JSONResponse(
                {
                    "error": "Storyline already exists",
                    "storyline_id": existing.id,
                },
                status_code=409,
            )
        storyline = repo.create_storyline(
            user_id=user.user_id,
            entity_ids=unique_entity_ids,
            name=name,
            description=description,
        )

        bootstrap_job_id: str | None = None
        try:
            job = job_runner.submit_storyline_update(
                storyline.id, user_id=user.user_id, bootstrap=True,
            )
            bootstrap_job_id = job.id
        except RuntimeError as exc:
            log.warning(
                "POST /api/storylines — created storyline %d but "
                "could not queue bootstrap: %s",
                storyline.id, exc,
            )

        log.info(
            "POST /api/storylines — created storyline %d "
            "(anchors=%s, bootstrap_job_id=%s)",
            storyline.id, unique_entity_ids, bootstrap_job_id,
        )
        chapters = repo.list_chapters(storyline.id)
        anchors = _anchors_for(repo, entity_store, storyline.id)
        detail = {
            **_storyline_to_dict(
                storyline, anchors, _unread_count(chapters), len(chapters),
            ),
            "chapters": [_chapter_meta_to_dict(c) for c in chapters],
        }
        return JSONResponse(
            {"storyline": detail, "bootstrap_job_id": bootstrap_job_id},
            status_code=201,
        )

    @mcp.custom_route(
        "/api/storylines/{storyline_id:int}/refresh",
        methods=["POST"],
        name="api_refresh_storyline",
    )
    @handler(services_getter)
    def refresh_storyline(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        """Queue a re-narration of the storyline's draft chapter.

        Returns 202 with ``{"job_id", "status"}``. Clients poll
        ``GET /api/jobs/{job_id}`` to observe progress. 404 if the
        storyline doesn't belong to the caller; 503 if the
        StorylineEngine isn't wired; 400 if the job runner rejects
        the request.
        """
        repo = services.get("storyline_repository")
        job_runner: JobRunner | None = services.get("job_runner")
        if repo is None or job_runner is None:
            return JSONResponse(
                {"error": "Storylines feature not configured"},
                status_code=503,
            )
        user = get_authenticated_user(request)
        sid = int(request.path_params["storyline_id"])
        storyline = repo.get_storyline(sid, user_id=user.user_id)
        if storyline is None:
            return JSONResponse(
                {"error": "Storyline not found"}, status_code=404,
            )
        try:
            job = job_runner.submit_storyline_update(
                sid, user_id=user.user_id, refresh_only=True,
            )
        except ValueError as e:
            log.warning("POST /api/storylines/%d/refresh — %s", sid, e)
            return JSONResponse({"error": str(e)}, status_code=400)
        except RuntimeError as e:
            log.warning("POST /api/storylines/%d/refresh — %s", sid, e)
            return JSONResponse({"error": str(e)}, status_code=503)
        log.info(
            "POST /api/storylines/%d/refresh — queued job %s", sid, job.id,
        )
        return JSONResponse(
            {"job_id": job.id, "status": job.status}, status_code=202,
        )

    @mcp.custom_route(
        "/api/storylines/{storyline_id:int}/chapters/unpublish",
        methods=["POST"],
        name="api_unpublish_storyline_chapter",
    )
    @handler(services_getter)
    def unpublish_storyline_chapter(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        """Fold the newest published chapter back into the draft.

        Validates that a published chapter exists before queueing (400
        if not — the repo-level fold happens inside the worker, see
        Task 8). Returns 202 with ``{"job_id", "status"}``. 404 if the
        storyline doesn't belong to the caller; 503 if the
        StorylineEngine isn't wired.
        """
        repo = services.get("storyline_repository")
        job_runner: JobRunner | None = services.get("job_runner")
        if repo is None or job_runner is None:
            return JSONResponse(
                {"error": "Storylines feature not configured"},
                status_code=503,
            )
        user = get_authenticated_user(request)
        sid = int(request.path_params["storyline_id"])
        storyline = repo.get_storyline(sid, user_id=user.user_id)
        if storyline is None:
            return JSONResponse(
                {"error": "Storyline not found"}, status_code=404,
            )
        chapters = repo.list_chapters(sid)
        if not any(c.state == "published" for c in chapters):
            return JSONResponse(
                {"error": "Storyline has no published chapter to unpublish"},
                status_code=400,
            )
        try:
            job = job_runner.submit_storyline_update(
                sid, user_id=user.user_id, unpublish=True,
            )
        except ValueError as e:
            log.warning(
                "POST /api/storylines/%d/chapters/unpublish — %s", sid, e,
            )
            return JSONResponse({"error": str(e)}, status_code=400)
        except RuntimeError as e:
            log.warning(
                "POST /api/storylines/%d/chapters/unpublish — %s", sid, e,
            )
            return JSONResponse({"error": str(e)}, status_code=503)
        log.info(
            "POST /api/storylines/%d/chapters/unpublish — queued job %s",
            sid, job.id,
        )
        return JSONResponse(
            {"job_id": job.id, "status": job.status}, status_code=202,
        )

    @mcp.custom_route(
        "/api/storylines/{storyline_id:int}/chapters/{chapter_id:int}/read",
        methods=["POST"],
        name="api_read_storyline_chapter",
    )
    @handler(services_getter)
    def read_storyline_chapter(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        """Mark a published chapter read. Returns the updated chapter meta."""
        return _set_chapter_read(request, services, read=True)

    @mcp.custom_route(
        "/api/storylines/{storyline_id:int}/chapters/{chapter_id:int}/unread",
        methods=["POST"],
        name="api_unread_storyline_chapter",
    )
    @handler(services_getter)
    def unread_storyline_chapter(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        """Mark a published chapter unread. Returns the updated chapter meta."""
        return _set_chapter_read(request, services, read=False)

    def _set_chapter_read(
        request: Request, services: ServicesDict, *, read: bool
    ) -> JSONResponse:
        repo = services.get("storyline_repository")
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
            return JSONResponse(
                {"error": "Storyline not found"}, status_code=404,
            )
        chapter = repo.get_chapter(cid)
        if chapter is None or chapter.storyline_id != sid:
            return JSONResponse(
                {"error": "Chapter not found"}, status_code=404,
            )
        try:
            updated = repo.set_read(cid, read)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        assert updated is not None
        log.info(
            "POST /api/storylines/%d/chapters/%d/%s",
            sid, cid, "read" if read else "unread",
        )
        return JSONResponse(_chapter_meta_to_dict(updated), status_code=200)

    @mcp.custom_route(
        "/api/storylines/{storyline_id:int}/chapters/{chapter_id:int}",
        methods=["PATCH"],
        name="api_rename_storyline_chapter",
    )
    @handler(services_getter, parse_json="raw")
    def rename_storyline_chapter(
        request: Request, services: ServicesDict, raw: bytes
    ) -> JSONResponse:
        """Rename a single chapter's title.

        Body: ``{title: str}`` — non-empty after trimming. Returns 200
        with the chapter meta dict. 404 if the chapter isn't found for
        this user, 400 on a missing/empty title, 503 if storylines
        aren't wired.
        """
        repo = services.get("storyline_repository")
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
            return JSONResponse(
                {"error": "Storyline not found"}, status_code=404,
            )
        chapter = repo.get_chapter(cid)
        if chapter is None or chapter.storyline_id != sid:
            return JSONResponse(
                {"error": "Chapter not found"}, status_code=404,
            )
        # Parse in-body ("raw" mode): the 503/404 checks above must keep
        # precedence over body-shape 400s.
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            parsed = {}
        if not isinstance(parsed, dict):
            return JSONResponse(
                {"error": "Request body must be a JSON object"},
                status_code=400,
            )
        title = (parsed.get("title") or "").strip()
        if not title:
            return JSONResponse(
                {"error": "title (non-empty str) is required"},
                status_code=400,
            )
        updated = repo.rename_chapter(cid, title)
        if updated is None:
            return JSONResponse(
                {"error": "Chapter not found"}, status_code=404,
            )
        log.info(
            "PATCH /api/storylines/%d/chapters/%d — title=%r", sid, cid, title,
        )
        return JSONResponse(_chapter_meta_to_dict(updated), status_code=200)

    @mcp.custom_route(
        "/api/storylines/{storyline_id:int}",
        methods=["PATCH"],
        name="api_update_storyline",
    )
    @handler(services_getter, parse_json="raw")
    def update_storyline(
        request: Request, services: ServicesDict, raw: bytes
    ) -> JSONResponse:
        """Update a storyline's editable metadata.

        Body: ``{name?: str, status?: "active" | "archived"}`` — at
        least one of the two. ``name`` must be non-empty after
        trimming; a rename is metadata-only and does NOT touch chapters
        or kick a job. ``status`` must be one of the two CHECK-
        constrained values.

        Returns 200 with the updated storyline summary. 404 if the
        storyline is not found for this user, 400 on a malformed body,
        empty name, or invalid status, 503 if storylines are not wired.
        """
        repo = services.get("storyline_repository")
        if repo is None:
            return JSONResponse(
                {"error": "Storylines feature not configured"},
                status_code=503,
            )
        entity_store = services.get("entity_store")
        user = get_authenticated_user(request)
        sid = int(request.path_params["storyline_id"])
        storyline = repo.get_storyline(sid, user_id=user.user_id)
        if storyline is None:
            return JSONResponse(
                {"error": "Storyline not found"}, status_code=404,
            )
        # Parse in-body ("raw" mode): the 503/404 checks above must keep
        # precedence over body-shape 400s.
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "Request body must be a JSON object"},
                status_code=400,
            )
        has_name = "name" in body
        has_status = "status" in body
        if not has_name and not has_status:
            return JSONResponse(
                {"error": "provide name and/or status"}, status_code=400,
            )
        if has_name:
            name = (body.get("name") or "").strip()
            if not name:
                return JSONResponse(
                    {"error": "name (non-empty str) is required"},
                    status_code=400,
                )
            updated = repo.update_storyline_name(sid, name, user_id=user.user_id)
            assert updated is not None  # 404 already handled above
            storyline = updated
        if has_status:
            status = body.get("status")
            if status not in _VALID_STATUSES:
                return JSONResponse(
                    {
                        "error": (
                            "status must be one of "
                            f"{', '.join(_VALID_STATUSES)}"
                        ),
                    },
                    status_code=400,
                )
            updated = repo.update_storyline_status(
                sid, status, user_id=user.user_id,
            )
            assert updated is not None  # 404 already handled above
            storyline = updated
        chapters = repo.list_chapters(sid)
        anchors = _anchors_for(repo, entity_store, sid)
        log.info(
            "PATCH /api/storylines/%d — name=%r status=%r",
            sid, body.get("name"), body.get("status"),
        )
        return JSONResponse(
            _storyline_to_dict(
                storyline, anchors, _unread_count(chapters), len(chapters),
            ),
            status_code=200,
        )

    @mcp.custom_route(
        "/api/storylines/{storyline_id:int}",
        methods=["DELETE"],
        name="api_delete_storyline",
    )
    @handler(services_getter)
    def delete_storyline(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        repo = services.get("storyline_repository")
        if repo is None:
            return JSONResponse(
                {"error": "Storylines feature not configured"},
                status_code=503,
            )
        user = get_authenticated_user(request)
        sid = int(request.path_params["storyline_id"])
        deleted = repo.delete_storyline(sid, user_id=user.user_id)
        if not deleted:
            return JSONResponse(
                {"error": "Storyline not found"}, status_code=404,
            )
        log.info(
            "DELETE /api/storylines/%d — removed", sid,
        )
        return JSONResponse({"deleted": True}, status_code=200)

    @mcp.custom_route(
        "/api/storylines/{storyline_id:int}/anchors",
        methods=["PUT"],
        name="api_set_storyline_anchors",
    )
    @handler(services_getter, parse_json="raw")
    def set_storyline_anchors(
        request: Request, services: ServicesDict, raw: bytes
    ) -> JSONResponse:
        """Replace the anchor set on an existing storyline.

        Body: ``{entity_ids: list[int]}`` — 1..MAX_ANCHORS entries.
        Returns 200 with ``{"id", "anchors"}``. 404 if the storyline is
        not found for this user, 400 on malformed/empty body, 422 if
        the cap is exceeded, 503 if storylines are not wired.
        """
        repo = services.get("storyline_repository")
        if repo is None:
            return JSONResponse(
                {"error": "Storylines feature not configured"},
                status_code=503,
            )
        entity_store = services.get("entity_store")
        user = get_authenticated_user(request)
        sid = int(request.path_params["storyline_id"])
        storyline = repo.get_storyline(sid, user_id=user.user_id)
        if storyline is None:
            return JSONResponse(
                {"error": "Storyline not found"}, status_code=404,
            )
        # Parse in-body ("raw" mode): the 503/404 checks above must keep
        # precedence over body-shape 400s.
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "Request body must be a JSON object"},
                status_code=400,
            )
        entity_ids = body.get("entity_ids")
        if (
            not isinstance(entity_ids, list)
            or not entity_ids
            or not all(isinstance(e, int) for e in entity_ids)
        ):
            return JSONResponse(
                {"error": "entity_ids must be a non-empty list of integers"},
                status_code=400,
            )
        unique_entity_ids = sorted(set(entity_ids))
        if len(unique_entity_ids) > MAX_ANCHORS:
            return JSONResponse(
                {
                    "error": (
                        f"entity_ids has {len(unique_entity_ids)} unique "
                        f"anchors; the cap is {MAX_ANCHORS}"
                    ),
                },
                status_code=422,
            )
        repo.set_anchors(sid, unique_entity_ids)
        anchors = _anchors_for(repo, entity_store, sid)
        log.info(
            "PUT /api/storylines/%d/anchors — anchors=%s",
            sid, unique_entity_ids,
        )
        return JSONResponse(
            {"id": sid, "anchors": anchors},
            status_code=200,
        )
