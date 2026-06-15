"""Storyline write/job-creation routes.

This module is part of the write/job-creation routing override documented
in ``code-quality-principles.md`` § "Routing rules for src/journal/api/".
The storyline write routes are job-creation routes (create kicks off a
generation job; regenerate queues one explicitly), so they follow the
override rather than living beside the reads in ``api/storylines.py``.
They were originally bundled into ``api/ingestion.py`` with the rest of
the override family, and were carved into this sibling module when that
file outgrew the ~800-line size rule — a sanctioned split of the
override, not a new deviation category.

Concretely, this module owns:

- ``POST /api/storylines``                              — create + auto-queue generation
- ``POST /api/storylines/{storyline_id}/regenerate``    — queue a regeneration job
- ``PATCH /api/storylines/{storyline_id}``              — update editable metadata (name)
- ``DELETE /api/storylines/{storyline_id}``             — delete a storyline
- ``PUT /api/storylines/{storyline_id}/anchors``        — replace the anchor set

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
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse

from journal.api._handler import handler
from journal.api.storylines import _chapter_to_dict
from journal.auth import get_authenticated_user

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.service_registry import ServicesDict
    from journal.services.jobs import JobRunner

log = logging.getLogger(__name__)


def _enqueue_chapter_regens(
    job_runner,
    storyline_id: int,
    user_id: int,
    chapters: list,
) -> list[str]:
    """Queue one generation job per affected chapter; return job ids.

    Closed chapters regenerate with ``mode="replace"``; the open chapter
    omits ``mode`` so the worker's default append/replace behaviour applies
    (mirrors the per-chapter regenerate route).
    """
    job_ids: list[str] = []
    for ch in chapters:
        kwargs: dict[str, Any] = {"user_id": user_id, "chapter_id": ch.id}
        if ch.state != "open":
            kwargs["mode"] = "replace"
        try:
            job = job_runner.submit_storyline_generation(storyline_id, **kwargs)
            job_ids.append(job.id)
        except (ValueError, RuntimeError) as exc:
            log.warning("could not queue regen for chapter %d: %s", ch.id, exc)
    return job_ids


def _anchors_for(repo, entity_store, storyline_id: int) -> list[dict[str, Any]]:
    """Build the ``anchors`` field for an API response.

    Returns ``[{id, canonical_name}, ...]`` sorted by anchor id ASC.
    Missing entities (deleted out from under the storyline) render as
    an empty canonical_name so the response shape stays stable.
    """
    anchor_ids = repo.list_anchors(storyline_id)
    out: list[dict[str, Any]] = []
    for entity_id in anchor_ids:
        if entity_store is not None:
            entity = entity_store.get_entity(entity_id)
            canonical_name = entity.canonical_name if entity else ""
        else:
            canonical_name = ""
        out.append({"id": entity_id, "canonical_name": canonical_name})
    return out


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
        """Create a new storyline.

        Body: ``{entity_ids: list[int], name: str, description?: str,
        start_date?: ISO, end_date?: ISO}``. ``entity_ids`` must have
        1..MAX_ANCHORS entries (current cap = 15). Returns 201 with
        the new storyline dict including an ``anchors`` list (one
        entry per anchor with ``id`` + ``canonical_name``), plus
        ``generation_job_id`` if a generation job was kicked off.
        409 if a storyline with the same name and the same exact
        anchor set already exists. 400 on bad input. 503 if
        storylines are not wired.

        On successful create, immediately queues a
        ``storyline_generation`` job so the new storyline's panels
        start populating without a separate regenerate call.
        """
        repo = services.get("storyline_repository")
        if repo is None:
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
            or not entity_ids
            or not all(isinstance(e, int) for e in entity_ids)
            or not name
        ):
            return JSONResponse(
                {
                    "error": (
                        "entity_ids (non-empty list[int]) and name (str) "
                        "are required"
                    ),
                },
                status_code=400,
            )
        from journal.services.storylines.service import MAX_ANCHORS
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
        description = body.get("description", "") or ""
        start_date = body.get("start_date")
        end_date = body.get("end_date")

        # Refuse if a storyline with this exact name + anchor set
        # already exists for this user — caller can GET the existing
        # one or regenerate.
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
            start_date=start_date,
            end_date=end_date,
        )
        # Seed the storyline's first (open) chapter so the auto-kicked
        # generation job — which resolves the open chapter — has a
        # target. Migrated storylines get theirs via migration 0030.
        repo.create_chapter(
            storyline_id=storyline.id, seq=1, title=storyline.name,
            start_date=storyline.start_date, end_date=storyline.end_date,
            state="open",
        )

        generation_job_id: str | None = None
        job_runner: JobRunner | None = services.get("job_runner")
        if job_runner is not None:
            try:
                job = job_runner.submit_storyline_generation(
                    storyline.id, user_id=user.user_id,
                )
                generation_job_id = job.id
            except RuntimeError as exc:
                log.warning(
                    "POST /api/storylines — created storyline %d but "
                    "could not queue generation: %s",
                    storyline.id, exc,
                )

        log.info(
            "POST /api/storylines — created storyline %d "
            "(anchors=%s, generation_job_id=%s)",
            storyline.id, unique_entity_ids, generation_job_id,
        )
        anchors = _anchors_for(repo, entity_store, storyline.id)
        response_body: dict[str, Any] = {
            "id": storyline.id,
            "user_id": storyline.user_id,
            "anchors": anchors,
            "name": storyline.name,
            "description": storyline.description,
            "status": storyline.status,
            "created_at": storyline.created_at,
        }
        if generation_job_id is not None:
            response_body["generation_job_id"] = generation_job_id
        return JSONResponse(response_body, status_code=201)

    @mcp.custom_route(
        "/api/storylines/{storyline_id:int}/regenerate",
        methods=["POST"],
        name="api_regenerate_storyline",
    )
    @handler(services_getter, parse_json="raw")
    def regenerate_storyline(
        request: Request, services: ServicesDict, raw_body: bytes
    ) -> JSONResponse:
        """Queue a regeneration job for one storyline.

        Optional JSON body: ``{start_date?: ISO, end_date?: ISO,
        mode?: "replace" | "append"}``. Empty body (no JSON at all
        or ``{}``) is allowed and preserves the original
        replace-with-stored-window behavior. Returns 202 with
        ``{"job_id", "status"}``. Clients poll ``GET /api/jobs/{job_id}``
        to observe progress. 400 on a malformed body (wrong types or
        invalid mode); 404 if the storyline doesn't belong to the
        caller; 503 if the StorylineGenerationService isn't wired.
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

        # Parse and validate optional body ("raw" mode: parse must stay
        # after the 404 check above). Missing body / empty body ==
        # replace mode with the storyline row's stored window — same
        # shape as the original W6 contract.
        try:
            body: dict[str, Any]
            if not raw_body:
                body = {}
            else:
                parsed = json.loads(raw_body)
                if not isinstance(parsed, dict):
                    return JSONResponse(
                        {"error": "Request body must be a JSON object"},
                        status_code=400,
                    )
                body = parsed
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": "Request body must be valid JSON"},
                status_code=400,
            )

        submit_kwargs: dict[str, Any] = {"user_id": user.user_id}
        for key in ("start_date", "end_date", "mode"):
            if key not in body:
                continue
            value = body[key]
            if not isinstance(value, str):
                return JSONResponse(
                    {"error": f"{key} must be a string"},
                    status_code=400,
                )
            submit_kwargs[key] = value

        try:
            job = job_runner.submit_storyline_generation(sid, **submit_kwargs)
        except ValueError as e:
            log.warning("POST /api/storylines/%d/regenerate — %s", sid, e)
            return JSONResponse({"error": str(e)}, status_code=400)
        except RuntimeError as e:
            log.warning("POST /api/storylines/%d/regenerate — %s", sid, e)
            return JSONResponse({"error": str(e)}, status_code=503)
        log.info(
            "POST /api/storylines/%d/regenerate — queued job %s "
            "(start=%s, end=%s, mode=%s)",
            sid, job.id,
            submit_kwargs.get("start_date"),
            submit_kwargs.get("end_date"),
            submit_kwargs.get("mode"),
        )
        return JSONResponse(
            {"job_id": job.id, "status": job.status},
            status_code=202,
        )

    @mcp.custom_route(
        "/api/storylines/{storyline_id:int}/chapters/{chapter_id:int}/regenerate",
        methods=["POST"],
        name="api_regenerate_storyline_chapter",
    )
    @handler(services_getter)
    def regenerate_storyline_chapter(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        """Queue a regeneration job for a single chapter.

        The chapter's own date window is authoritative, so the worker
        always runs ``mode="replace"`` (``regenerate_chapter`` supports
        replace only). Returns 202 with ``{"job_id", "status"}``. 404
        if the chapter doesn't belong to the caller's storyline; 503 if
        storylines aren't wired.
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
            job = job_runner.submit_storyline_generation(
                sid, user_id=user.user_id, chapter_id=cid, mode="replace",
            )
        except ValueError as e:
            log.warning(
                "POST /api/storylines/%d/chapters/%d/regenerate — %s",
                sid, cid, e,
            )
            return JSONResponse({"error": str(e)}, status_code=400)
        except RuntimeError as e:
            log.warning(
                "POST /api/storylines/%d/chapters/%d/regenerate — %s",
                sid, cid, e,
            )
            return JSONResponse({"error": str(e)}, status_code=503)
        log.info(
            "POST /api/storylines/%d/chapters/%d/regenerate — queued job %s",
            sid, cid, job.id,
        )
        return JSONResponse(
            {"job_id": job.id, "status": job.status},
            status_code=202,
        )

    @mcp.custom_route(
        "/api/storylines/{storyline_id:int}/chapters/{chapter_id:int}",
        methods=["PATCH"],
        name="api_rename_storyline_chapter",
    )
    @handler(services_getter, parse_json="raw")
    def rename_storyline_chapter(
        request: Request, services: ServicesDict, raw: bytes
    ) -> JSONResponse:
        """Rename a single chapter (metadata-only).

        Body: ``{title: str}`` — non-empty after trimming. Does NOT
        touch the chapter's panels or kick a regeneration. Returns 200
        with the chapter dict. 404 if the chapter isn't found for this
        user, 400 on a malformed body or empty title, 503 if storylines
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
        return JSONResponse(_chapter_to_dict(repo, updated), status_code=200)

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

        Body: ``{name: str}`` — non-empty after trimming. Currently
        only the name (title) is editable; a rename is metadata-only
        and does NOT touch the stored panels or kick a regeneration,
        so the curated/narrative text is preserved across a rename.

        Returns 200 with the updated storyline summary (id, name,
        anchors, ...). 404 if the storyline is not found for this user,
        400 on a malformed body or empty name, 503 if storylines are
        not wired.
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
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse(
                {"error": "name (non-empty str) is required"},
                status_code=400,
            )
        updated = repo.update_storyline_name(sid, name, user_id=user.user_id)
        assert updated is not None  # 404 already handled above
        anchors = _anchors_for(repo, entity_store, sid)
        log.info("PATCH /api/storylines/%d — name=%r", sid, name)
        return JSONResponse(
            {
                "id": updated.id,
                "user_id": updated.user_id,
                "anchors": anchors,
                "name": updated.name,
                "description": updated.description,
                "status": updated.status,
                "created_at": updated.created_at,
                "updated_at": updated.updated_at,
            },
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
        "/api/storylines/{storyline_id:int}/chapters",
        methods=["POST"],
        name="api_add_storyline_chapter",
    )
    @handler(services_getter, parse_json="raw")
    def add_storyline_chapter(
        request: Request, services: ServicesDict, raw: bytes
    ) -> JSONResponse:
        """Add a chapter to an existing storyline.

        Body: ``{start_date: ISO, end_date?: ISO}``.
        New-latest flavor (no end_date): closes the current open chapter and
        appends a new open chapter. Ranged flavor (end_date present): inserts a
        closed chapter into a free date window.

        Returns 201 with ``{"chapter": ..., "job_ids": [...]}``. 503 if
        storylines are not wired; 404 if the storyline is not found for this
        user; 400 on missing/invalid body or if the repo rejects the window.
        """
        repo = services.get("storyline_repository")
        job_runner = services.get("job_runner")
        if repo is None or job_runner is None:
            return JSONResponse(
                {"error": "Storylines feature not configured"}, status_code=503,
            )
        user = get_authenticated_user(request)
        sid = int(request.path_params["storyline_id"])
        if repo.get_storyline(sid, user_id=user.user_id) is None:
            return JSONResponse({"error": "Storyline not found"}, status_code=404)
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict) or not isinstance(body.get("start_date"), str):
            return JSONResponse(
                {"error": "start_date (ISO str) is required"}, status_code=400,
            )
        end_date = body.get("end_date")
        if end_date is not None and not isinstance(end_date, str):
            return JSONResponse(
                {"error": "end_date must be a string"}, status_code=400,
            )
        try:
            chapter = repo.add_chapter(sid, body["start_date"], end_date)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        job_ids = _enqueue_chapter_regens(job_runner, sid, user.user_id, [chapter])
        log.info("POST /api/storylines/%d/chapters — added chapter %d", sid, chapter.id)
        return JSONResponse(
            {"chapter": _chapter_to_dict(repo, chapter), "job_ids": job_ids},
            status_code=201,
        )

    @mcp.custom_route(
        "/api/storylines/{storyline_id:int}/chapters/{chapter_id:int}/split",
        methods=["POST"],
        name="api_split_storyline_chapter",
    )
    @handler(services_getter, parse_json="raw")
    def split_storyline_chapter(
        request: Request, services: ServicesDict, raw: bytes
    ) -> JSONResponse:
        """Split a chapter into two at a given date.

        Body: ``{date: ISO}``.  Left half keeps the original row with
        ``end_date = day_before(date)``; right half is a new row starting at
        ``date``. Both halves are enqueued for regeneration.

        Returns 200 with ``{"chapters": [left, right], "job_ids": [...]}``.
        503 if storylines are not wired; 404 if the storyline or chapter are
        not found for this user; 400 on missing/invalid body or if the repo
        rejects the split date (out of window, etc.).
        """
        repo = services.get("storyline_repository")
        job_runner = services.get("job_runner")
        if repo is None or job_runner is None:
            return JSONResponse(
                {"error": "Storylines feature not configured"}, status_code=503,
            )
        user = get_authenticated_user(request)
        sid = int(request.path_params["storyline_id"])
        cid = int(request.path_params["chapter_id"])
        if repo.get_storyline(sid, user_id=user.user_id) is None:
            return JSONResponse({"error": "Storyline not found"}, status_code=404)
        chapter = repo.get_chapter(cid)
        if chapter is None or chapter.storyline_id != sid:
            return JSONResponse({"error": "Chapter not found"}, status_code=404)
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict) or not isinstance(body.get("date"), str):
            return JSONResponse(
                {"error": "date (ISO str) is required"}, status_code=400,
            )
        try:
            left, right = repo.split_chapter(cid, body["date"])
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        job_ids = _enqueue_chapter_regens(
            job_runner, sid, user.user_id, [left, right],
        )
        log.info(
            "POST /api/storylines/%d/chapters/%d/split @ %s",
            sid, cid, body["date"],
        )
        return JSONResponse(
            {
                "chapters": [
                    _chapter_to_dict(repo, left),
                    _chapter_to_dict(repo, right),
                ],
                "job_ids": job_ids,
            },
            status_code=200,
        )

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
        Returns 200 with the updated ``anchors`` list. 404 if the
        storyline is not found for this user, 400 on malformed body,
        422 if the cap is exceeded, 503 if storylines are not wired.

        Anchor *editing* on the webapp is deferred to a follow-up;
        this endpoint is available via REST + MCP so Claude and
        scripted clients can manage anchors today.
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
        from journal.services.storylines.service import MAX_ANCHORS
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
