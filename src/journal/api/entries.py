"""Entry routes — list, detail, verify-doubts, chunks, tokens, entry/entities.

Six routes under ``/api/entries/...``:

- ``GET    /api/entries`` — paginated list with date filters.
- ``GET    /api/entries/{id}`` — entry detail with uncertain spans.
- ``PATCH  /api/entries/{id}`` — update final_text, entry_date, and/or
  content-window fields (content_start_char / content_end_char).
- ``DELETE /api/entries/{id}`` — soft-blocked while entry has active jobs.
- ``POST   /api/entries/{id}/verify-doubts`` — mark all OCR doubts verified.
- ``GET    /api/entries/{id}/chunks`` — persisted chunks with offsets.
- ``GET    /api/entries/{id}/tokens`` — on-demand tiktoken cl100k tokens.
- ``GET    /api/entries/{id}/entities`` — entities mentioned in this entry
  (cross-resource handler placed here because the URL prefix root is
  ``entries``).

Entry *creation* lives in ``ingestion.py`` per the responsibility-override
routing rule (see ``_shared.py`` / ``ingestion.py`` docstrings).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse

from journal.api._handler import handler
from journal.api._shared import (
    _TOKEN_ENCODING_NAME,
    _TOKEN_MODEL_HINT,
    _entity_summary,
    _entry_summary,
    _entry_to_dict,
    _runtime_get,
    _token_encoder,
)
from journal.auth import get_authenticated_user
from journal.services.entry_dates import EntryDateError

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.entitystore.store import EntityStore
    from journal.service_registry import ServicesDict
    from journal.services.ingestion import IngestionService
    from journal.services.jobs import JobRunner
    from journal.services.query import QueryService

log = logging.getLogger(__name__)


def register_entries_routes(
    mcp: FastMCP,
    services_getter: Callable[[], ServicesDict | None],
) -> None:
    """Register the ``/api/entries/...`` read/CRUD routes."""

    @mcp.custom_route("/api/entries", methods=["GET"], name="api_list_entries")
    @handler(services_getter)
    def list_entries(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        """List journal entries with pagination and optional date filtering."""
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
        total = query_svc.count_entries(start_date, end_date, user_id=user_id)

        items = []
        for entry in entries:
            page_count = query_svc.get_page_count(entry.id)
            span_count = query_svc.get_uncertain_span_count(entry.id)
            entity_count = query_svc.get_entity_mention_count(entry.id)
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
    @handler(services_getter, parse_json="raw")
    def entry_detail(
        request: Request, services: ServicesDict, raw: bytes
    ) -> JSONResponse:
        """Get, update, or delete a single journal entry."""
        entry_id = int(request.path_params["entry_id"])
        user = get_authenticated_user(request)
        user_id = user.user_id

        if request.method == "GET":
            return _get_entry(services, entry_id, user_id)
        elif request.method == "PATCH":
            return _patch_entry(raw, services, entry_id, user_id)
        elif request.method == "DELETE":
            return _delete_entry(services, entry_id, user_id)
        else:
            return JSONResponse({"error": "Method not allowed"}, status_code=405)

    def _get_entry(
        services: ServicesDict, entry_id: int, user_id: int
    ) -> JSONResponse:
        query_svc: QueryService = services["query"]
        entry = query_svc.get_entry(entry_id, user_id=user_id)
        if entry is None:
            log.warning("GET /api/entries/%d — not found", entry_id)
            return JSONResponse({"error": f"Entry {entry_id} not found"}, status_code=404)
        page_count = query_svc.get_page_count(entry_id)
        uncertain_spans = query_svc.get_uncertain_spans(entry_id)
        log.info("GET /api/entries/%d — %s, %d words", entry_id, entry.entry_date, entry.word_count)
        return JSONResponse(_entry_to_dict(entry, page_count, uncertain_spans))

    def _patch_entry(
        raw: bytes, services: ServicesDict, entry_id: int, user_id: int
    ) -> JSONResponse:
        query_svc: QueryService = services["query"]
        ingestion_svc: IngestionService = services["ingestion"]

        # Verify entry exists
        entry = query_svc.get_entry(entry_id, user_id=user_id)
        if entry is None:
            return JSONResponse({"error": f"Entry {entry_id} not found"}, status_code=404)

        # Parse request body ("raw" mode: parse must stay after the
        # entry-404 check above, and a non-dict body must keep its
        # historical behavior).
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        final_text = body.get("final_text")
        new_date = body.get("entry_date")

        # Content-window fields — must be provided together.
        has_start = "content_start_char" in body
        has_end = "content_end_char" in body
        start = body.get("content_start_char")
        end = body.get("content_end_char")

        if final_text is None and new_date is None and not (has_start or has_end):
            return JSONResponse(
                {
                    "error": (
                        "At least one of 'final_text', 'entry_date', or "
                        "'content_start_char'/'content_end_char' is required"
                    )
                },
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
            try:
                updated = ingestion_svc.update_entry_date(
                    entry_id, new_date, user_id=user_id,
                )
            except EntryDateError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)

        # Update text if provided
        entity_extraction_job_id: str | None = None
        reprocess_job_id: str | None = None
        mood_job_id: str | None = None
        pipeline_job_id: str | None = None
        should_queue_pipeline = False

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
            should_queue_pipeline = True

        # Apply content window if the fields were present.
        # NOTE: when both final_text and window fields are sent in the same
        # request, the window update runs *after* save_final_text and
        # re-derives final_text from the raw_text slice.  The window-derived
        # text therefore takes precedence over any manually supplied
        # final_text in that request.
        if has_start or has_end:
            if not (has_start and has_end):
                return JSONResponse(
                    {
                        "error": (
                            "content_start_char and content_end_char must be "
                            "provided together"
                        )
                    },
                    status_code=400,
                )
            if start is None and end is None:
                # null/null — clear the window, re-derive from full raw_text.
                try:
                    updated = ingestion_svc.update_content_window(
                        entry_id, None, None, user_id=user_id,
                    )
                except ValueError as e:
                    log.warning("PATCH /api/entries/%d — window clear error: %s", entry_id, e)
                    return JSONResponse({"error": str(e)}, status_code=400)
                should_queue_pipeline = True
            elif start is None or end is None:
                # Exactly one of the two values is null — this is ambiguous
                # (not a clear, not a valid range).  Reject with an explicit
                # message before falling into the range-validation branch,
                # which would otherwise produce a misleading type error.
                return JSONResponse(
                    {
                        "error": (
                            "content_start_char and content_end_char must both be "
                            "integers, or both null to clear the window"
                        )
                    },
                    status_code=400,
                )
            else:
                if (
                    not isinstance(start, int)
                    or not isinstance(end, int)
                    or not (0 <= start < end <= len(entry.raw_text or ""))
                ):
                    return JSONResponse(
                        {
                            "error": (
                                "content window must satisfy "
                                "0 <= start < end <= len(raw_text)"
                            )
                        },
                        status_code=400,
                    )
                try:
                    updated = ingestion_svc.update_content_window(
                        entry_id, start, end, user_id=user_id,
                    )
                except ValueError as e:
                    log.warning("PATCH /api/entries/%d — window update error: %s", entry_id, e)
                    return JSONResponse({"error": str(e)}, status_code=400)
                should_queue_pipeline = True

        # Queue the save-entry pipeline once — regardless of whether the
        # trigger was a final_text change, a window change, or both.
        # The single `should_queue_pipeline` flag prevents double-submission.
        if should_queue_pipeline:
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

        page_count = query_svc.get_page_count(entry_id)
        uncertain_spans = query_svc.get_uncertain_spans(entry_id)
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

    def _delete_entry(
        services: ServicesDict, entry_id: int, user_id: int
    ) -> JSONResponse:
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
    @handler(services_getter)
    def verify_doubts(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        """Mark all OCR doubts on an entry as verified.

        Sets doubts_verified=1 on the entry. The underlying uncertain
        span rows are preserved for future analysis. After verification,
        GET and list endpoints return uncertain_span_count=0 and an
        empty uncertain_spans array for this entry.
        """
        query_svc: QueryService = services["query"]
        ingestion_svc: IngestionService = services["ingestion"]
        user = get_authenticated_user(request)
        user_id = user.user_id
        entry_id = int(request.path_params["entry_id"])

        ok = ingestion_svc.verify_doubts(entry_id, user_id=user_id)
        if not ok:
            log.warning("POST /api/entries/%d/verify-doubts — not found", entry_id)
            return JSONResponse({"error": f"Entry {entry_id} not found"}, status_code=404)

        log.info("POST /api/entries/%d/verify-doubts — doubts verified", entry_id)
        entry = query_svc.get_entry(entry_id, user_id=user_id)
        page_count = query_svc.get_page_count(entry_id)
        return JSONResponse(_entry_to_dict(entry, page_count, uncertain_spans=[]))

    @mcp.custom_route(
        "/api/entries/{entry_id:int}/chunks",
        methods=["GET"],
        name="api_entry_chunks",
    )
    @handler(services_getter)
    def entry_chunks(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        """Return the persisted chunks for an entry, with source offsets.

        Used by the webapp overlay to draw chunk boundaries on top of
        the entry text. The 404 `chunks_not_backfilled` response is
        distinguished from `entry_not_found` so the webapp can surface
        a clear message telling the user to re-ingest or run backfill.
        """
        query_svc: QueryService = services["query"]
        user = get_authenticated_user(request)
        user_id = user.user_id
        entry_id = int(request.path_params["entry_id"])

        entry = query_svc.get_entry(entry_id, user_id=user_id)
        if entry is None:
            log.warning("GET /api/entries/%d/chunks — entry not found", entry_id)
            return JSONResponse(
                {
                    "error": "entry_not_found",
                    "message": f"Entry {entry_id} not found",
                },
                status_code=404,
            )

        chunks = query_svc.get_chunks(entry_id)
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
    @handler(services_getter)
    def entry_tokens(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        """Tokenise an entry's text on demand using tiktoken `cl100k_base`.

        Returns per-token `{index, token_id, text, char_start, char_end}`
        where the character offsets are positions in `final_text` (or
        `raw_text` as fallback). Valid UTF-8 text round-trips through
        tiktoken exactly, so the offsets slice the original text without
        any loss. Computed per request — the call is cheap (< 10 ms for
        journal-scale text) and avoids any cache invalidation logic
        when `final_text` is edited.
        """
        query_svc: QueryService = services["query"]
        user = get_authenticated_user(request)
        user_id = user.user_id
        entry_id = int(request.path_params["entry_id"])

        entry = query_svc.get_entry(entry_id, user_id=user_id)
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

    # ---- per-entry entity lookups ---------------------------------------

    @mcp.custom_route(
        "/api/entries/{entry_id:int}/entities",
        methods=["GET"],
        name="api_entry_entities",
    )
    @handler(services_getter)
    def entry_entities(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        entity_store: EntityStore = services["entity_store"]
        query_svc: QueryService = services["query"]
        user = get_authenticated_user(request)
        user_id = user.user_id
        entry_id = int(request.path_params["entry_id"])

        entry = query_svc.get_entry(entry_id, user_id=user_id)
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
