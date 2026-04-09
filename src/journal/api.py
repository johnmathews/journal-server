"""REST API endpoints for the journal webapp."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.services.ingestion import IngestionService
    from journal.services.query import QueryService

log = logging.getLogger(__name__)


def _entry_to_dict(entry: Any, page_count: int = 0) -> dict[str, Any]:
    """Convert an Entry to a JSON-serializable dict."""
    return {
        "id": entry.id,
        "entry_date": entry.entry_date,
        "source_type": entry.source_type,
        "raw_text": entry.raw_text,
        "final_text": entry.final_text,
        "word_count": entry.word_count,
        "chunk_count": entry.chunk_count,
        "page_count": page_count,
        "language": entry.language,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
    }


def _entry_summary(entry: Any, page_count: int = 0) -> dict[str, Any]:
    """Convert an Entry to a summary dict (no text fields)."""
    return {
        "id": entry.id,
        "entry_date": entry.entry_date,
        "source_type": entry.source_type,
        "word_count": entry.word_count,
        "chunk_count": entry.chunk_count,
        "page_count": page_count,
        "created_at": entry.created_at,
    }


def register_api_routes(
    mcp: FastMCP,
    services_getter: Callable[[], dict | None],
) -> None:
    """Register REST API routes on the MCP server.

    Args:
        mcp: The FastMCP instance.
        services_getter: A callable that returns the services dict
            (with 'query' and 'ingestion' keys).
    """

    @mcp.custom_route("/api/entries", methods=["GET"], name="api_list_entries")
    async def list_entries(request: Request) -> JSONResponse:
        """List journal entries with pagination and optional date filtering."""
        services = services_getter()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )

        query_svc: QueryService = services["query"]

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

        entries = query_svc.list_entries(start_date, end_date, limit, offset)
        total = query_svc._repo.count_entries(start_date, end_date)

        items = []
        for entry in entries:
            page_count = query_svc._repo.get_page_count(entry.id)
            items.append(_entry_summary(entry, page_count))

        return JSONResponse({
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        })

    @mcp.custom_route(
        "/api/entries/{entry_id:int}",
        methods=["GET", "PATCH"],
        name="api_entry_detail",
    )
    async def entry_detail(request: Request) -> JSONResponse:
        """Get or update a single journal entry."""
        services = services_getter()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )

        entry_id = int(request.path_params["entry_id"])

        if request.method == "GET":
            return await _get_entry(services, entry_id)
        elif request.method == "PATCH":
            return await _patch_entry(request, services, entry_id)
        else:
            return JSONResponse(
                {"error": "Method not allowed"}, status_code=405
            )

    async def _get_entry(services: dict, entry_id: int) -> JSONResponse:
        query_svc: QueryService = services["query"]
        entry = query_svc._repo.get_entry(entry_id)
        if entry is None:
            return JSONResponse(
                {"error": f"Entry {entry_id} not found"}, status_code=404
            )
        page_count = query_svc._repo.get_page_count(entry_id)
        return JSONResponse(_entry_to_dict(entry, page_count))

    async def _patch_entry(
        request: Request, services: dict, entry_id: int
    ) -> JSONResponse:
        query_svc: QueryService = services["query"]
        ingestion_svc: IngestionService = services["ingestion"]

        # Verify entry exists
        entry = query_svc._repo.get_entry(entry_id)
        if entry is None:
            return JSONResponse(
                {"error": f"Entry {entry_id} not found"}, status_code=404
            )

        # Parse request body
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": "Invalid JSON body"}, status_code=400
            )

        final_text = body.get("final_text")
        if final_text is None or not isinstance(final_text, str):
            return JSONResponse(
                {"error": "'final_text' is required and must be a string"},
                status_code=400,
            )

        if not final_text.strip():
            return JSONResponse(
                {"error": "'final_text' must not be empty"},
                status_code=400,
            )

        try:
            updated = ingestion_svc.update_entry_text(entry_id, final_text)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        page_count = query_svc._repo.get_page_count(entry_id)
        return JSONResponse(_entry_to_dict(updated, page_count))

    @mcp.custom_route("/api/stats", methods=["GET"], name="api_stats")
    async def get_stats(request: Request) -> JSONResponse:
        """Get journal statistics."""
        services = services_getter()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )

        query_svc: QueryService = services["query"]

        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        stats = query_svc.get_statistics(start_date, end_date)
        return JSONResponse(asdict(stats))
