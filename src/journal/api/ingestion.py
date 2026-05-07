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
from typing import TYPE_CHECKING

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

        page_count = ingestion_svc._repo.get_page_count(entry.id)
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
        ingestion_svc._store_source_file(
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

        page_count = ingestion_svc._repo.get_page_count(entry.id)
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
