"""Write/job-creation routes (the responsibility-override of URL-prefix routing).

This module exists to honour the routing rule documented in
``code-quality-principles.md`` § "Routing rules for src/journal/api/":

> **Override — responsibility (write/job creation).** Routes whose primary
> effect is to create a job or perform a long-running write live in
> ``api/ingestion.py``, regardless of URL prefix.

Concretely, this module owns:

- ``POST /api/entries/ingest/text``  — sync ingest from a text body
- ``POST /api/entries/ingest/file``  — sync ingest from .md/.txt upload
- ``POST /api/entries/ingest/images`` — async OCR ingest job
- ``POST /api/entries/ingest/audio``  — async transcription ingest job
- ``POST /api/entities/extract``     — async entity-extraction batch job
- ``POST /api/mood/backfill``        — async mood-score backfill job
- ``POST /api/fitness/sync/{source}`` — async fitness fetch+normalize job
- ``POST /api/fitness/backfill/{source}`` — async fitness historical backfill job

Why these and not their URL-prefix neighbours? They share a dependency
cluster (``IngestionService``, ``JobRunner``, OCR / transcription /
extraction providers) that the read paths in ``entries.py`` /
``entities.py`` / ``jobs.py`` never touch. Bundling them with their
URL roots would push those modules past the readable-context budget
and mix two categories of code agents reason about differently.

**Rule for new routes.** If a new route's primary effect is "create a
job that does work", it goes here even if its URL nests under another
resource. New deviation categories require updating both
``code-quality-principles.md`` and ``code-quality-refactor-plan.md``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse

from journal.api._shared import _convert_heic_to_jpeg, _entry_to_dict
from journal.auth import get_authenticated_user

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.services.ingestion import IngestionService
    from journal.services.jobs import JobRunner

log = logging.getLogger(__name__)


def register_ingestion_routes(
    mcp: FastMCP,
    services_getter: Callable[[], dict | None],
) -> None:
    """Register write/job-creation routes (see module docstring for the rule)."""

    @mcp.custom_route(
        "/api/entries/ingest/text",
        methods=["POST"],
        name="api_ingest_text",
    )
    async def ingest_text(request: Request) -> JSONResponse:
        """Create a journal entry from plain text (no OCR).

        Request body (JSON):
            text (str, required): The entry content.
            entry_date (str, optional): ISO-8601 date, defaults to today.
            source_type (str, optional): defaults to "text_entry".

        Returns 201 with the created entry and an optional mood_job_id.
        """
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)

        ingestion_svc: IngestionService = services["ingestion"]
        user = get_authenticated_user(request)
        user_id = user.user_id

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "Request body must be a JSON object"},
                status_code=400,
            )

        text = body.get("text")
        if text is None or not isinstance(text, str):
            return JSONResponse(
                {"error": "'text' is required and must be a string"},
                status_code=400,
            )
        if not text.strip():
            return JSONResponse(
                {"error": "'text' must not be empty"},
                status_code=400,
            )

        entry_date = body.get("entry_date") or datetime.now(UTC).strftime("%Y-%m-%d")
        source_type = body.get("source_type", "text_entry")

        try:
            entry = ingestion_svc.ingest_text(
                text,
                entry_date,
                source_type,
                skip_mood=True,
                user_id=user_id,
            )
        except ValueError as e:
            log.warning("POST /api/entries/ingest/text — %s", e)
            return JSONResponse({"error": str(e)}, status_code=400)

        # Fire async post-ingestion jobs
        mood_job_id = None
        entity_extraction_job_id = None
        config = services.get("config")
        job_runner: JobRunner = services["job_runner"]
        if config and config.enable_mood_scoring:
            try:
                mood_job = job_runner.submit_mood_score_entry(entry.id, user_id=user_id)
                mood_job_id = mood_job.id
            except Exception:
                log.warning(
                    "POST /api/entries/ingest/text — failed to queue mood scoring",
                    exc_info=True,
                )
        try:
            ej = job_runner.submit_entity_extraction({"entry_id": entry.id}, user_id=user_id)
            entity_extraction_job_id = ej.id
        except Exception:
            log.warning(
                "POST /api/entries/ingest/text — failed to queue entity extraction",
                exc_info=True,
            )

        page_count = ingestion_svc.get_page_count(entry.id)
        log.info(
            "POST /api/entries/ingest/text — created entry %d (%d words)",
            entry.id,
            entry.word_count,
        )
        return JSONResponse(
            {
                "entry": _entry_to_dict(entry, page_count),
                "mood_job_id": mood_job_id,
                "entity_extraction_job_id": entity_extraction_job_id,
            },
            status_code=201,
        )

    @mcp.custom_route(
        "/api/entries/ingest/file",
        methods=["POST"],
        name="api_ingest_file",
    )
    async def ingest_file(request: Request) -> JSONResponse:
        """Create a journal entry from an uploaded .md or .txt file.

        Expects multipart/form-data with:
            file: a single .md or .txt file
            entry_date (optional): ISO-8601 date, defaults to today.

        Returns 201 with the created entry and an optional mood_job_id.
        """
        from journal.api_utils import parse_multipart_request

        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)

        ingestion_svc: IngestionService = services["ingestion"]
        user = get_authenticated_user(request)
        user_id = user.user_id

        try:
            fields, files = await parse_multipart_request(request)
        except Exception as e:
            log.warning("POST /api/entries/ingest/file — parse error: %s", e)
            return JSONResponse(
                {"error": f"Failed to parse multipart request: {e}"},
                status_code=400,
            )

        file_list = files.get("file", [])
        if len(file_list) != 1:
            return JSONResponse(
                {"error": "Exactly one file is required in the 'file' field"},
                status_code=400,
            )

        uploaded = file_list[0]
        filename_lower = uploaded.filename.lower()
        if not (filename_lower.endswith(".md") or filename_lower.endswith(".txt")):
            return JSONResponse(
                {"error": "File must be .md or .txt"},
                status_code=400,
            )

        try:
            content = uploaded.data.decode("utf-8")
        except UnicodeDecodeError:
            return JSONResponse(
                {"error": "File must be valid UTF-8 text"},
                status_code=400,
            )

        if not content.strip():
            return JSONResponse(
                {"error": "File is empty"},
                status_code=400,
            )

        # Date inference: content text > filename > user-provided > today
        from journal.services.date_extraction import (
            extract_date_from_filename,
            extract_date_from_text,
        )

        user_date = fields.get("entry_date")
        text_date = extract_date_from_text(content)
        filename_date = extract_date_from_filename(uploaded.filename)
        entry_date = (
            text_date or filename_date or user_date or datetime.now(UTC).strftime("%Y-%m-%d")
        )

        try:
            entry = ingestion_svc.ingest_text(
                content,
                entry_date,
                "imported_text_file",
                skip_mood=True,
                user_id=user_id,
            )
        except ValueError as e:
            log.warning("POST /api/entries/ingest/file — %s", e)
            return JSONResponse({"error": str(e)}, status_code=400)

        # Store source file metadata for dedup
        file_hash = hashlib.sha256(uploaded.data).hexdigest()
        ingestion_svc.store_source_file(
            entry.id,
            f"upload:{uploaded.filename}",
            uploaded.content_type,
            file_hash,
        )

        # Fire async post-ingestion jobs
        mood_job_id = None
        entity_extraction_job_id = None
        config = services.get("config")
        job_runner: JobRunner = services["job_runner"]
        if config and config.enable_mood_scoring:
            try:
                mood_job = job_runner.submit_mood_score_entry(entry.id, user_id=user_id)
                mood_job_id = mood_job.id
            except Exception:
                log.warning(
                    "POST /api/entries/ingest/file — failed to queue mood scoring",
                    exc_info=True,
                )
        try:
            ej = job_runner.submit_entity_extraction({"entry_id": entry.id}, user_id=user_id)
            entity_extraction_job_id = ej.id
        except Exception:
            log.warning(
                "POST /api/entries/ingest/file — failed to queue entity extraction",
                exc_info=True,
            )

        page_count = ingestion_svc.get_page_count(entry.id)
        log.info(
            "POST /api/entries/ingest/file — created entry %d from %s (%d words)",
            entry.id,
            uploaded.filename,
            entry.word_count,
        )
        return JSONResponse(
            {
                "entry": _entry_to_dict(entry, page_count),
                "mood_job_id": mood_job_id,
                "entity_extraction_job_id": entity_extraction_job_id,
            },
            status_code=201,
        )

    @mcp.custom_route(
        "/api/entries/ingest/images",
        methods=["POST"],
        name="api_ingest_images",
    )
    async def ingest_images(request: Request) -> JSONResponse:
        """Upload one or more journal page images for OCR ingestion.

        Expects multipart/form-data with:
            images: one or more image files (jpeg, png, gif, webp)
            entry_date (optional): ISO-8601 date, defaults to today.

        Returns 202 with a job_id for async processing.
        """
        from journal.api_utils import parse_multipart_request

        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        user = get_authenticated_user(request)
        user_id = user.user_id

        try:
            fields, files = await parse_multipart_request(request)
        except Exception as e:
            log.warning("POST /api/entries/ingest/images — parse error: %s", e)
            return JSONResponse(
                {"error": f"Failed to parse multipart request: {e}"},
                status_code=400,
            )

        image_list = files.get("images", [])
        if not image_list:
            return JSONResponse(
                {"error": "At least one image is required in the 'images' field"},
                status_code=400,
            )

        allowed_types = {
            "image/jpeg",
            "image/png",
            "image/gif",
            "image/webp",
            "image/heic",
            "image/heif",
        }
        heic_types = {"image/heic", "image/heif"}
        max_file_size = 10 * 1024 * 1024  # 10 MB per file
        max_total_size = 50 * 1024 * 1024  # 50 MB total

        total_size = 0
        images: list[tuple[bytes, str, str]] = []
        for uploaded in image_list:
            if uploaded.content_type not in allowed_types:
                return JSONResponse(
                    {
                        "error": f"File '{uploaded.filename}' has unsupported type "
                        f"'{uploaded.content_type}'. "
                        f"Allowed: JPEG, PNG, GIF, WebP, HEIC."
                    },
                    status_code=400,
                )
            if len(uploaded.data) > max_file_size:
                return JSONResponse(
                    {"error": f"File '{uploaded.filename}' exceeds 10 MB limit"},
                    status_code=400,
                )
            total_size += len(uploaded.data)
            if total_size > max_total_size:
                return JSONResponse(
                    {"error": "Total upload size exceeds 50 MB limit"},
                    status_code=413,
                )

            data = uploaded.data
            content_type = uploaded.content_type
            if content_type in heic_types:
                data, content_type = _convert_heic_to_jpeg(data)

            images.append((data, content_type, uploaded.filename))

        entry_date = fields.get("entry_date") or datetime.now(UTC).strftime("%Y-%m-%d")

        job_runner: JobRunner = services["job_runner"]
        try:
            job = job_runner.submit_image_ingestion(images, entry_date, user_id=user_id)
        except ValueError as e:
            log.warning("POST /api/entries/ingest/images — %s", e)
            return JSONResponse({"error": str(e)}, status_code=400)

        log.info(
            "POST /api/entries/ingest/images — queued job %s (%d images)",
            job.id,
            len(images),
        )
        return JSONResponse(
            {"job_id": job.id, "status": job.status},
            status_code=202,
        )

    @mcp.custom_route(
        "/api/entries/ingest/audio",
        methods=["POST"],
        name="api_ingest_audio",
    )
    async def ingest_audio(request: Request) -> JSONResponse:
        """Upload one or more audio recordings for transcription ingestion.

        Expects multipart/form-data with:
            audio: one or more audio files (mp3, mp4, wav, webm, ogg, flac, m4a)
            entry_date (optional): ISO-8601 date, defaults to today.
            source_type (optional): defaults to "voice" (live recording).
                Use "imported_audio_file" for uploaded audio files.

        Returns 202 with a job_id for async processing.
        """
        from journal.api_utils import parse_multipart_request

        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        user = get_authenticated_user(request)
        user_id = user.user_id

        try:
            fields, files = await parse_multipart_request(request)
        except Exception as e:
            log.warning("POST /api/entries/ingest/audio — parse error: %s", e)
            return JSONResponse(
                {"error": f"Failed to parse multipart request: {e}"},
                status_code=400,
            )

        audio_list = files.get("audio", [])
        if not audio_list:
            return JSONResponse(
                {"error": "At least one audio file is required in the 'audio' field"},
                status_code=400,
            )

        allowed_types = {
            "audio/mpeg",
            "audio/mp3",
            "audio/mp4",
            "audio/wav",
            "audio/x-wav",
            "audio/webm",
            "audio/ogg",
            "audio/flac",
            "audio/x-m4a",
            "audio/m4a",
        }
        max_file_size = 100 * 1024 * 1024  # 100 MB per file (recordings can be long)
        max_total_size = 500 * 1024 * 1024  # 500 MB total

        total_size = 0
        recordings: list[tuple[bytes, str, str]] = []
        for uploaded in audio_list:
            if uploaded.content_type not in allowed_types:
                return JSONResponse(
                    {
                        "error": f"File '{uploaded.filename}' has unsupported type "
                        f"'{uploaded.content_type}'. Allowed: MP3, MP4, WAV, WebM, OGG, FLAC, M4A."
                    },
                    status_code=400,
                )
            if len(uploaded.data) > max_file_size:
                return JSONResponse(
                    {"error": f"File '{uploaded.filename}' exceeds 100 MB limit"},
                    status_code=400,
                )
            total_size += len(uploaded.data)
            if total_size > max_total_size:
                return JSONResponse(
                    {"error": "Total upload size exceeds 500 MB limit"},
                    status_code=413,
                )
            recordings.append((uploaded.data, uploaded.content_type, uploaded.filename))

        entry_date = fields.get("entry_date") or datetime.now(UTC).strftime("%Y-%m-%d")
        source_type = fields.get("source_type", "voice")

        job_runner: JobRunner = services["job_runner"]
        try:
            job = job_runner.submit_audio_ingestion(
                recordings,
                entry_date,
                source_type=source_type,
                user_id=user_id,
            )
        except ValueError as e:
            log.warning("POST /api/entries/ingest/audio — %s", e)
            return JSONResponse({"error": str(e)}, status_code=400)

        log.info(
            "POST /api/entries/ingest/audio — queued job %s (%d recordings)",
            job.id,
            len(recordings),
        )
        return JSONResponse(
            {"job_id": job.id, "status": job.status},
            status_code=202,
        )

    # -----------------------------------------------------------------
    # Entity routes
    # -----------------------------------------------------------------

    @mcp.custom_route(
        "/api/entities/extract",
        methods=["POST"],
        name="api_entities_extract",
    )
    async def extract_entities(request: Request) -> JSONResponse:
        """Submit an entity-extraction batch job.

        Request body matches the legacy synchronous shape —
        ``{entry_id?, start_date?, end_date?, stale_only?}`` — so
        existing clients continue to compile. Validation (unknown
        keys, bad types) happens in the JobRunner and bubbles up as
        a 400. The single-entry ``entry_id`` path also goes through
        the jobs table now; there is no synchronous path.

        Returns 202 with ``{"job_id", "status"}``. Clients should
        poll ``GET /api/jobs/{job_id}`` to observe progress and
        result.
        """
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        user = get_authenticated_user(request)
        user_id = user.user_id
        job_runner: JobRunner = services["job_runner"]

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "Request body must be a JSON object"},
                status_code=400,
            )
        try:
            job = job_runner.submit_entity_extraction(body, user_id=user_id)
        except ValueError as e:
            log.warning("POST /api/entities/extract — %s", e)
            return JSONResponse({"error": str(e)}, status_code=400)

        log.info("POST /api/entities/extract — queued job %s", job.id)
        return JSONResponse(
            {"job_id": job.id, "status": job.status},
            status_code=202,
        )

    # -----------------------------------------------------------------
    # Storyline routes (write/job-creation only — reads in api/storylines.py)
    # -----------------------------------------------------------------

    @mcp.custom_route(
        "/api/storylines",
        methods=["POST"],
        name="api_create_storyline",
    )
    async def create_storyline(request: Request) -> JSONResponse:
        """Create a new storyline.

        Body: ``{entity_id: int, name: str, description?: str,
        start_date?: ISO, end_date?: ISO}``. Returns 201 with the
        new storyline dict (plus ``generation_job_id`` if a generation
        job was kicked off), 409 if (user, entity, name) already
        exists, 400 on bad input, 503 if storylines are not wired.

        On successful create, immediately queues a
        ``storyline_generation`` job so the new storyline's panels
        start populating without a separate regenerate call (W7).
        """
        services = services_getter()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503,
            )
        repo = services.get("storyline_repository")
        if repo is None:
            return JSONResponse(
                {"error": "Storylines feature not configured"},
                status_code=503,
            )
        user = get_authenticated_user(request)

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "Request body must be a JSON object"},
                status_code=400,
            )
        entity_id = body.get("entity_id")
        name = (body.get("name") or "").strip()
        if not isinstance(entity_id, int) or not name:
            return JSONResponse(
                {"error": "entity_id (int) and name (str) are required"},
                status_code=400,
            )
        description = body.get("description", "") or ""
        start_date = body.get("start_date")
        end_date = body.get("end_date")

        # Refuse if (user, entity, name) already exists — caller can
        # GET the existing one or regenerate.
        existing = repo.find_by_entity(
            user_id=user.user_id, entity_id=entity_id, name=name,
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
            entity_id=entity_id,
            name=name,
            description=description,
            start_date=start_date,
            end_date=end_date,
        )

        # Auto-kick the generation job (W7). The job runner refuses
        # to queue when the StorylineGenerationService isn't wired —
        # we treat that as a soft failure and still return the
        # created storyline, just without a job_id. This lets a
        # server with the storylines write-path enabled but no
        # ANTHROPIC_API_KEY still expose the create endpoint to
        # admins seeding state by hand.
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
            "(entity_id=%d, generation_job_id=%s)",
            storyline.id, entity_id, generation_job_id,
        )
        response_body: dict[str, Any] = {
            "id": storyline.id,
            "user_id": storyline.user_id,
            "entity_id": storyline.entity_id,
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
    async def regenerate_storyline(request: Request) -> JSONResponse:
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
        services = services_getter()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503,
            )
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

        # Parse and validate optional body. Missing body / empty
        # body == replace mode with the storyline row's stored
        # window — same shape as the original W6 contract.
        try:
            raw_body = await request.body()
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
        "/api/storylines/{storyline_id:int}",
        methods=["DELETE"],
        name="api_delete_storyline",
    )
    async def delete_storyline(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503,
            )
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
        "/api/mood/backfill",
        methods=["POST"],
        name="api_mood_backfill",
    )
    async def mood_backfill(request: Request) -> JSONResponse:
        """Submit a mood-score backfill batch job.

        Request body: ``{mode, start_date?, end_date?}`` where
        ``mode`` is ``"stale-only"`` or ``"force"``. Unknown keys
        or bad types return 400. Returns 202 with
        ``{"job_id", "status"}``.
        """
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        user = get_authenticated_user(request)
        user_id = user.user_id
        job_runner: JobRunner = services["job_runner"]

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "Request body must be a JSON object"},
                status_code=400,
            )
        try:
            job = job_runner.submit_mood_backfill(body, user_id=user_id)
        except ValueError as e:
            log.warning("POST /api/mood/backfill — %s", e)
            return JSONResponse({"error": str(e)}, status_code=400)

        log.info("POST /api/mood/backfill — queued job %s", job.id)
        return JSONResponse(
            {"job_id": job.id, "status": job.status},
            status_code=202,
        )

    @mcp.custom_route(
        "/api/fitness/sync/{source:str}",
        methods=["POST"],
        name="api_fitness_sync",
    )
    async def fitness_sync(request: Request) -> JSONResponse:
        """Trigger a fitness fetch+normalize job for the given source.

        ``source`` must be ``"strava"`` or ``"garmin"``. Returns 202 with
        ``{job_id, status}`` on success. If a fitness sync for this user
        and source is already in flight (``queued`` or ``running``), the
        existing job_id is returned with ``already_running: true`` —
        the W6 fetch service has its own single-run guard, but
        deduping here keeps the operator-facing audit trail clean (one
        job row per real sync, not one per button-press).

        Returns 503 if the source isn't configured on this server
        (no ``STRAVA_CLIENT_ID`` / ``STRAVA_CLIENT_SECRET`` for Strava).
        Garmin is always wired post-W6 — per-user creds in
        ``fitness_auth_state`` are the source of truth — so a user
        without a Garmin auth row produces a clean ``auth_broken`` sync
        rather than a 503.
        """
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        user = get_authenticated_user(request)
        user_id = user.user_id
        source = str(request.path_params["source"])
        if source not in ("strava", "garmin"):
            return JSONResponse(
                {"error": f"Unknown fitness source: {source!r}"},
                status_code=400,
            )

        job_repository = services["job_repository"]
        # W5: dedup spans both worker classes — a sync submitted while a
        # backfill is in flight (or vice versa) returns the existing
        # job_id. See SQLiteJobRepository.find_active_fitness_fetch_job.
        in_flight = job_repository.find_active_fitness_fetch_job(
            user_id=user_id, source=source,
        )
        if in_flight is not None:
            log.info(
                "POST /api/fitness/sync/%s — returning existing in-flight "
                "fetch job %s (type=%s, status=%s)",
                source, in_flight.id, in_flight.type, in_flight.status,
            )
            return JSONResponse(
                {
                    "job_id": in_flight.id,
                    "status": in_flight.status,
                    "already_running": True,
                },
                status_code=202,
            )

        job_runner: JobRunner = services["job_runner"]
        submit = (
            job_runner.submit_fitness_sync_strava
            if source == "strava"
            else job_runner.submit_fitness_sync_garmin
        )
        try:
            job = submit(user_id=user_id)
        except RuntimeError as e:
            # Source not configured on this server — fail-loud at submit
            # time per W8 decision #2.
            log.warning("POST /api/fitness/sync/%s — %s", source, e)
            return JSONResponse({"error": str(e)}, status_code=503)
        except ValueError as e:
            log.warning("POST /api/fitness/sync/%s — %s", source, e)
            return JSONResponse({"error": str(e)}, status_code=400)

        log.info("POST /api/fitness/sync/%s — queued job %s", source, job.id)
        return JSONResponse(
            {"job_id": job.id, "status": job.status},
            status_code=202,
        )

    @mcp.custom_route(
        "/api/fitness/backfill/{source:str}",
        methods=["POST"],
        name="api_fitness_backfill",
    )
    async def fitness_backfill(request: Request) -> JSONResponse:
        """Queue a historical backfill job for ``source`` (W5).

        Body: ``{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"?}``. Returns
        ``{job_id, status}`` on 202. Shares the W5 spanning idempotency
        with ``POST /api/fitness/sync/{source}`` — only one fetch job
        per ``(user_id, source)`` may be in flight at once, and a sync
        in flight blocks a backfill (and vice versa). When a colliding
        job is found, the existing ``{job_id, status, already_running:
        true}`` is returned instead of queueing a duplicate.

        The job runs the same orchestrator that backs the
        ``journal fitness-backfill`` CLI, so resume / abort /
        transient-streak semantics are identical.
        """
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        user = get_authenticated_user(request)
        user_id = user.user_id
        source = str(request.path_params["source"])
        if source not in ("strava", "garmin"):
            return JSONResponse(
                {"error": f"Unknown fitness source: {source!r}"},
                status_code=400,
            )

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                {"error": "Request body must be valid JSON"},
                status_code=400,
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "Request body must be a JSON object"},
                status_code=400,
            )

        start = body.get("start")
        end = body.get("end")
        if not isinstance(start, str) or not start:
            return JSONResponse(
                {"error": "'start' is required and must be a YYYY-MM-DD string"},
                status_code=400,
            )
        if end is not None and (not isinstance(end, str) or not end):
            return JSONResponse(
                {"error": "'end' must be a YYYY-MM-DD string when provided"},
                status_code=400,
            )

        # Validate date strings + ordering. ISO parsing surfaces typos
        # before we burn a job row that would crash inside the worker.
        try:
            start_d = datetime.strptime(start, "%Y-%m-%d").date()
        except ValueError:
            return JSONResponse(
                {"error": f"'start' must be YYYY-MM-DD, got {start!r}"},
                status_code=400,
            )
        if end is not None:
            try:
                end_d = datetime.strptime(end, "%Y-%m-%d").date()
            except ValueError:
                return JSONResponse(
                    {"error": f"'end' must be YYYY-MM-DD, got {end!r}"},
                    status_code=400,
                )
            if end_d < start_d:
                return JSONResponse(
                    {"error": "'end' must be on or after 'start'"},
                    status_code=400,
                )

        job_repository = services["job_repository"]
        in_flight = job_repository.find_active_fitness_fetch_job(
            user_id=user_id, source=source,
        )
        if in_flight is not None:
            log.info(
                "POST /api/fitness/backfill/%s — returning existing in-flight "
                "fetch job %s (type=%s, status=%s)",
                source, in_flight.id, in_flight.type, in_flight.status,
            )
            return JSONResponse(
                {
                    "job_id": in_flight.id,
                    "status": in_flight.status,
                    "already_running": True,
                },
                status_code=202,
            )

        job_runner: JobRunner = services["job_runner"]
        submit = (
            job_runner.submit_fitness_backfill_strava
            if source == "strava"
            else job_runner.submit_fitness_backfill_garmin
        )
        try:
            job = submit(user_id=user_id, start=start, end=end)
        except RuntimeError as e:
            log.warning("POST /api/fitness/backfill/%s — %s", source, e)
            return JSONResponse({"error": str(e)}, status_code=503)
        except ValueError as e:
            log.warning("POST /api/fitness/backfill/%s — %s", source, e)
            return JSONResponse({"error": str(e)}, status_code=400)

        log.info(
            "POST /api/fitness/backfill/%s — queued job %s (start=%s end=%s)",
            source, job.id, start, end,
        )
        return JSONResponse(
            {"job_id": job.id, "status": job.status},
            status_code=202,
        )
