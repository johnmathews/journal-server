"""Legacy single-file home for routes still awaiting per-resource extraction.

Each unit of the api.py split moves a resource group out of this module
into its own module under `journal/api/`. When this file is empty it
will be deleted.

The function defined here is private to the package — `__init__.py`
calls it from `register_api_routes`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse

from journal.api._shared import (
    _TOKEN_ENCODING_NAME,
    _TOKEN_MODEL_HINT,
    _convert_heic_to_jpeg,
    _entity_summary,
    _entry_summary,
    _entry_to_dict,
    _runtime_get,
    _token_encoder,
)
from journal.auth import get_authenticated_user

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.entitystore.store import EntityStore
    from journal.services.ingestion import IngestionService
    from journal.services.jobs import JobRunner
    from journal.services.query import QueryService

log = logging.getLogger(__name__)


def _register_legacy_routes(
    mcp: FastMCP,
    services_getter: Callable[[], dict | None],
) -> None:
    """Register the routes that have not yet been extracted to per-resource modules."""

    @mcp.custom_route("/api/entries", methods=["GET"], name="api_list_entries")
    async def list_entries(request: Request) -> JSONResponse:
        """List journal entries with pagination and optional date filtering."""
        services = services_getter()
        if services is None:
            log.error("GET /api/entries — services not initialized")
            return JSONResponse({"error": "Server not initialized"}, status_code=503)

        query_svc: QueryService = services["query"]
        user = get_authenticated_user(request)
        user_id = user.user_id

        # Parse query params
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        try:
            limit = min(int(request.query_params.get("limit", "20")), 100)
        except ValueError:
            limit = 20
        try:
            offset = max(int(request.query_params.get("offset", "0")), 0)
        except ValueError:
            offset = 0

        entries = query_svc.list_entries(start_date, end_date, limit, offset, user_id=user_id)
        total = query_svc._repo.count_entries(start_date, end_date, user_id=user_id)

        items = []
        for entry in entries:
            page_count = query_svc._repo.get_page_count(entry.id)
            span_count = query_svc._repo.get_uncertain_span_count(entry.id)
            entity_count = query_svc._repo.get_entity_mention_count(entry.id)
            items.append(_entry_summary(entry, page_count, span_count, entity_count))

        log.info("GET /api/entries — returned %d/%d entries (offset=%d)", len(items), total, offset)
        return JSONResponse(
            {
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    @mcp.custom_route(
        "/api/entries/{entry_id:int}",
        methods=["GET", "PATCH", "DELETE"],
        name="api_entry_detail",
    )
    async def entry_detail(request: Request) -> JSONResponse:
        """Get, update, or delete a single journal entry."""
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)

        entry_id = int(request.path_params["entry_id"])
        user = get_authenticated_user(request)
        user_id = user.user_id

        if request.method == "GET":
            return await _get_entry(services, entry_id, user_id)
        elif request.method == "PATCH":
            return await _patch_entry(request, services, entry_id, user_id)
        elif request.method == "DELETE":
            return await _delete_entry(services, entry_id, user_id)
        else:
            return JSONResponse({"error": "Method not allowed"}, status_code=405)

    async def _get_entry(services: dict, entry_id: int, user_id: int) -> JSONResponse:
        query_svc: QueryService = services["query"]
        entry = query_svc._repo.get_entry(entry_id, user_id=user_id)
        if entry is None:
            log.warning("GET /api/entries/%d — not found", entry_id)
            return JSONResponse({"error": f"Entry {entry_id} not found"}, status_code=404)
        page_count = query_svc._repo.get_page_count(entry_id)
        uncertain_spans = query_svc._repo.get_uncertain_spans(entry_id)
        log.info("GET /api/entries/%d — %s, %d words", entry_id, entry.entry_date, entry.word_count)
        return JSONResponse(_entry_to_dict(entry, page_count, uncertain_spans))

    async def _patch_entry(
        request: Request, services: dict, entry_id: int, user_id: int
    ) -> JSONResponse:
        query_svc: QueryService = services["query"]
        ingestion_svc: IngestionService = services["ingestion"]

        # Verify entry exists
        entry = query_svc._repo.get_entry(entry_id, user_id=user_id)
        if entry is None:
            return JSONResponse({"error": f"Entry {entry_id} not found"}, status_code=404)

        # Parse request body
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        final_text = body.get("final_text")
        new_date = body.get("entry_date")

        if final_text is None and new_date is None:
            return JSONResponse(
                {"error": "At least one of 'final_text' or 'entry_date' is required"},
                status_code=400,
            )

        updated = entry

        # Update date if provided
        if new_date is not None:
            if not isinstance(new_date, str) or not new_date.strip():
                return JSONResponse(
                    {"error": "'entry_date' must be a non-empty string"},
                    status_code=400,
                )
            try:
                import datetime as dt

                dt.date.fromisoformat(new_date)
            except ValueError:
                return JSONResponse(
                    {"error": "'entry_date' must be a valid ISO 8601 date (YYYY-MM-DD)"},
                    status_code=400,
                )
            updated = query_svc._repo.update_entry_date(entry_id, new_date, user_id=user_id)

        # Update text if provided
        entity_extraction_job_id: str | None = None
        reprocess_job_id: str | None = None
        mood_job_id: str | None = None
        pipeline_job_id: str | None = None
        if final_text is not None:
            if not isinstance(final_text, str):
                return JSONResponse(
                    {"error": "'final_text' must be a string"},
                    status_code=400,
                )
            if not final_text.strip():
                return JSONResponse(
                    {"error": "'final_text' must not be empty"},
                    status_code=400,
                )
            try:
                updated = ingestion_svc.save_final_text(entry_id, final_text, user_id=user_id)
            except ValueError as e:
                log.warning("PATCH /api/entries/%d — error: %s", entry_id, e)
                return JSONResponse({"error": str(e)}, status_code=400)

            # Queue the save-entry pipeline: one synthetic parent job
            # holds three children (reprocess_embeddings, entity_extraction,
            # mood_score_entry) and emits a SINGLE consolidated Pushover
            # covering all three. See submit_save_entry_pipeline for the
            # design.
            job_runner: JobRunner | None = services.get("job_runner")
            if job_runner is not None:
                try:
                    parent, follow_ups = job_runner.submit_save_entry_pipeline(
                        entry_id=entry_id,
                        user_id=user_id,
                        enable_mood_scoring=bool(
                            _runtime_get(services, "enable_mood_scoring"),
                        ),
                    )
                    pipeline_job_id = parent.id
                    reprocess_job_id = follow_ups.get("reprocess_embeddings")
                    entity_extraction_job_id = follow_ups.get("entity_extraction")
                    mood_job_id = follow_ups.get("mood_scoring")
                    log.info(
                        "PATCH /api/entries/%d — queued save-entry pipeline %s "
                        "(reprocess=%s, entity=%s, mood=%s)",
                        entry_id, parent.id,
                        reprocess_job_id, entity_extraction_job_id, mood_job_id,
                    )
                except Exception:
                    log.warning(
                        "PATCH /api/entries/%d — failed to queue save-entry pipeline",
                        entry_id,
                        exc_info=True,
                    )

        page_count = query_svc._repo.get_page_count(entry_id)
        uncertain_spans = query_svc._repo.get_uncertain_spans(entry_id)
        log.info("PATCH /api/entries/%d — updated", entry_id)
        resp = _entry_to_dict(updated, page_count, uncertain_spans)
        if entity_extraction_job_id is not None:
            resp["entity_extraction_job_id"] = entity_extraction_job_id
        if reprocess_job_id is not None:
            resp["reprocess_job_id"] = reprocess_job_id
        if mood_job_id is not None:
            resp["mood_job_id"] = mood_job_id
        if pipeline_job_id is not None:
            resp["pipeline_job_id"] = pipeline_job_id
        return JSONResponse(resp)

    async def _delete_entry(services: dict, entry_id: int, user_id: int) -> JSONResponse:
        job_repo = services["job_repository"]
        active_jobs = job_repo.has_active_jobs_for_entry(entry_id)
        if active_jobs:
            job_ids = [j.id for j in active_jobs]
            log.warning(
                "DELETE /api/entries/%d — blocked by %d active job(s): %s",
                entry_id, len(active_jobs), job_ids,
            )
            return JSONResponse(
                {
                    "error": "Entry has active jobs",
                    "message": (
                        f"Entry {entry_id} has {len(active_jobs)} running/queued "
                        "job(s). Wait for them to finish before deleting."
                    ),
                    "job_ids": job_ids,
                },
                status_code=409,
            )

        ingestion_svc: IngestionService = services["ingestion"]
        entity_store: EntityStore = services["entity_store"]

        # Snapshot entity IDs linked to this entry before deletion.
        # The CASCADE on entity_mentions will remove the mention rows,
        # but the entity records themselves need explicit cleanup.
        prior_entity_ids = [
            m.entity_id for m in entity_store.get_mentions_for_entry(entry_id)
        ]

        deleted = ingestion_svc.delete_entry(entry_id, user_id=user_id)
        if not deleted:
            log.warning("DELETE /api/entries/%d — not found", entry_id)
            return JSONResponse({"error": f"Entry {entry_id} not found"}, status_code=404)

        # Prune entities that lost all mentions due to this deletion.
        orphans_deleted = 0
        if prior_entity_ids:
            orphans_deleted = entity_store.delete_orphaned_entities(
                list(set(prior_entity_ids))
            )
        log.info(
            "DELETE /api/entries/%d — deleted (%d orphaned entities pruned)",
            entry_id, orphans_deleted,
        )
        return JSONResponse({"deleted": True, "id": entry_id})

    @mcp.custom_route(
        "/api/entries/{entry_id:int}/verify-doubts",
        methods=["POST"],
        name="api_verify_doubts",
    )
    async def verify_doubts(request: Request) -> JSONResponse:
        """Mark all OCR doubts on an entry as verified.

        Sets doubts_verified=1 on the entry. The underlying uncertain
        span rows are preserved for future analysis. After verification,
        GET and list endpoints return uncertain_span_count=0 and an
        empty uncertain_spans array for this entry.
        """
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)

        query_svc: QueryService = services["query"]
        user = get_authenticated_user(request)
        user_id = user.user_id
        entry_id = int(request.path_params["entry_id"])

        ok = query_svc._repo.verify_doubts(entry_id, user_id=user_id)
        if not ok:
            log.warning("POST /api/entries/%d/verify-doubts — not found", entry_id)
            return JSONResponse({"error": f"Entry {entry_id} not found"}, status_code=404)

        log.info("POST /api/entries/%d/verify-doubts — doubts verified", entry_id)
        entry = query_svc._repo.get_entry(entry_id, user_id=user_id)
        page_count = query_svc._repo.get_page_count(entry_id)
        return JSONResponse(_entry_to_dict(entry, page_count, uncertain_spans=[]))

    @mcp.custom_route(
        "/api/entries/{entry_id:int}/chunks",
        methods=["GET"],
        name="api_entry_chunks",
    )
    async def entry_chunks(request: Request) -> JSONResponse:
        """Return the persisted chunks for an entry, with source offsets.

        Used by the webapp overlay to draw chunk boundaries on top of
        the entry text. The 404 `chunks_not_backfilled` response is
        distinguished from `entry_not_found` so the webapp can surface
        a clear message telling the user to re-ingest or run backfill.
        """
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)

        query_svc: QueryService = services["query"]
        user = get_authenticated_user(request)
        user_id = user.user_id
        entry_id = int(request.path_params["entry_id"])

        entry = query_svc._repo.get_entry(entry_id, user_id=user_id)
        if entry is None:
            log.warning("GET /api/entries/%d/chunks — entry not found", entry_id)
            return JSONResponse(
                {
                    "error": "entry_not_found",
                    "message": f"Entry {entry_id} not found",
                },
                status_code=404,
            )

        chunks = query_svc._repo.get_chunks(entry_id)
        if not chunks:
            log.info(
                "GET /api/entries/%d/chunks — no chunks persisted (pre-backfill entry)",
                entry_id,
            )
            return JSONResponse(
                {
                    "error": "chunks_not_backfilled",
                    "message": (
                        "This entry was ingested before chunk persistence was "
                        "available. Re-ingest the entry or run the backfill "
                        "service to populate chunks."
                    ),
                },
                status_code=404,
            )

        payload = {
            "entry_id": entry_id,
            "chunks": [
                {
                    "index": i,
                    "text": c.text,
                    "char_start": c.char_start,
                    "char_end": c.char_end,
                    "token_count": c.token_count,
                }
                for i, c in enumerate(chunks)
            ],
        }
        log.info("GET /api/entries/%d/chunks — %d chunks", entry_id, len(chunks))
        return JSONResponse(payload)

    @mcp.custom_route(
        "/api/entries/{entry_id:int}/tokens",
        methods=["GET"],
        name="api_entry_tokens",
    )
    async def entry_tokens(request: Request) -> JSONResponse:
        """Tokenise an entry's text on demand using tiktoken `cl100k_base`.

        Returns per-token `{index, token_id, text, char_start, char_end}`
        where the character offsets are positions in `final_text` (or
        `raw_text` as fallback). Valid UTF-8 text round-trips through
        tiktoken exactly, so the offsets slice the original text without
        any loss. Computed per request — the call is cheap (< 10 ms for
        journal-scale text) and avoids any cache invalidation logic
        when `final_text` is edited.
        """
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)

        query_svc: QueryService = services["query"]
        user = get_authenticated_user(request)
        user_id = user.user_id
        entry_id = int(request.path_params["entry_id"])

        entry = query_svc._repo.get_entry(entry_id, user_id=user_id)
        if entry is None:
            log.warning("GET /api/entries/%d/tokens — entry not found", entry_id)
            return JSONResponse(
                {
                    "error": "entry_not_found",
                    "message": f"Entry {entry_id} not found",
                },
                status_code=404,
            )

        text = entry.final_text or entry.raw_text or ""
        token_ids = _token_encoder.encode(text)
        # `decode_with_offsets` returns (decoded_str, offsets) where each
        # offset is the character index in the decoded string where the
        # corresponding token begins. For valid UTF-8 input the decoded
        # string equals the input, so these offsets are positions in the
        # original text the webapp will render.
        decoded, starts = _token_encoder.decode_with_offsets(token_ids)
        tokens: list[dict[str, Any]] = []
        for i, (tid, start) in enumerate(zip(token_ids, starts, strict=True)):
            end = starts[i + 1] if i + 1 < len(starts) else len(decoded)
            tokens.append(
                {
                    "index": i,
                    "token_id": int(tid),
                    "text": decoded[start:end],
                    "char_start": int(start),
                    "char_end": int(end),
                }
            )

        log.info("GET /api/entries/%d/tokens — %d tokens", entry_id, len(tokens))
        return JSONResponse(
            {
                "entry_id": entry_id,
                "encoding": _TOKEN_ENCODING_NAME,
                "model_hint": _TOKEN_MODEL_HINT,
                "token_count": len(tokens),
                "tokens": tokens,
            }
        )

    # -----------------------------------------------------------------
    # Ingestion routes (entry creation)
    # -----------------------------------------------------------------


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

    # ---- per-entry entity lookups ---------------------------------------

    @mcp.custom_route(
        "/api/entries/{entry_id:int}/entities",
        methods=["GET"],
        name="api_entry_entities",
    )
    async def entry_entities(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        entity_store: EntityStore = services["entity_store"]
        query_svc: QueryService = services["query"]
        user = get_authenticated_user(request)
        user_id = user.user_id
        entry_id = int(request.path_params["entry_id"])

        entry = query_svc._repo.get_entry(entry_id, user_id=user_id)
        if entry is None:
            return JSONResponse({"error": f"Entry {entry_id} not found"}, status_code=404)

        entities = entity_store.get_entities_for_entry(entry_id)
        mentions = entity_store.get_mentions_for_entry(entry_id)
        mentions_by_entity: dict[int, int] = {}
        quotes_by_entity: dict[int, list[str]] = {}
        for m in mentions:
            mentions_by_entity[m.entity_id] = mentions_by_entity.get(m.entity_id, 0) + 1
            quotes_by_entity.setdefault(m.entity_id, [])
            if m.quote not in quotes_by_entity[m.entity_id]:
                quotes_by_entity[m.entity_id].append(m.quote)
        items = [
            _entity_summary(
                e,
                mentions_by_entity.get(e.id, 0),
                quotes=quotes_by_entity.get(e.id, []),
            )
            for e in entities
        ]
        log.info("GET /api/entries/%d/entities — %d entities", entry_id, len(items))
        return JSONResponse({"entry_id": entry_id, "items": items, "total": len(items)})
