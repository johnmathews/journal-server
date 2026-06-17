"""Hybrid search route.

``GET /api/search`` runs BM25 (SQLite FTS5) + dense (embedding) retrieval,
fuses with Reciprocal Rank Fusion, and reranks the top fan-out with the
configured reranker. Bearer-authenticated via the app-wide middleware.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

from journal.api._handler import handler
from journal.api._shared import _search_result_dict
from journal.auth import get_authenticated_user
from journal.providers.answerer import AnswerUnavailable

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.service_registry import ServicesDict
    from journal.services.query import QueryService

log = logging.getLogger(__name__)


def register_search_routes(
    mcp: FastMCP,
    services_getter: Callable[[], ServicesDict | None],
) -> None:
    """Register /api/search."""

    @mcp.custom_route("/api/search", methods=["GET"], name="api_search")
    @handler(services_getter)
    def search(request: Request, services: ServicesDict, body: None) -> JSONResponse:
        """Hybrid search across journal entries.

        Combines BM25 (SQLite FTS5) and dense (embedding) retrieval,
        fuses the candidates with Reciprocal Rank Fusion, then reranks
        the top fan-out with the configured reranker. Bearer-
        authenticated via the app-wide auth middleware.

        Each result item populates `snippet` (when BM25 contributed —
        an FTS5 excerpt with `\\x02`/`\\x03` control chars wrapping
        matched terms) and `matching_chunks` (when dense retrieval
        contributed — chunks carry `char_start`/`char_end`/`chunk_index`
        for in-place highlight rendering). Either or both may be
        present per item.

        The `mode` query parameter has been retired. Passing it is a
        client bug — the response is 400 `mode_removed` so the bug is
        visible.

        Runs as a sync body on a worker thread via the ``handler``
        decorator — the embed + rerank pipeline must not block the
        event loop.
        """
        query_svc: QueryService = services["query"]
        user = get_authenticated_user(request)
        user_id = user.user_id

        q = (request.query_params.get("q") or "").strip()
        if not q:
            return JSONResponse(
                {
                    "error": "missing_query",
                    "message": "'q' query parameter is required",
                },
                status_code=400,
            )

        if "mode" in request.query_params:
            return JSONResponse(
                {
                    "error": "mode_removed",
                    "message": (
                        "The 'mode' parameter was removed when hybrid search "
                        "shipped. Drop it from your request — every search "
                        "now combines keyword and semantic retrieval."
                    ),
                },
                status_code=400,
            )

        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        try:
            limit = min(max(int(request.query_params.get("limit", "10")), 1), 50)
        except ValueError:
            limit = 10
        try:
            offset = max(int(request.query_params.get("offset", "0")), 0)
        except ValueError:
            offset = 0

        sort = request.query_params.get("sort", "relevance")
        if sort not in ("relevance", "date_desc", "date_asc"):
            return JSONResponse(
                {
                    "error": "invalid_sort",
                    "message": (
                        "'sort' must be one of: relevance, date_desc, date_asc"
                    ),
                },
                status_code=400,
            )

        try:
            results = query_svc.search_entries(
                query=q,
                start_date=start_date,
                end_date=end_date,
                limit=limit,
                offset=offset,
                user_id=user_id,
                sort=sort,
            )
        except sqlite3.OperationalError as e:
            # FTS5 raises this on malformed queries (unterminated
            # quotes, bare operators like `AND`, etc.). Surface as a
            # 400 rather than a 500 so clients can tell the user.
            log.info("GET /api/search — invalid FTS5 query %r: %s", q, e)
            return JSONResponse(
                {
                    "error": "invalid_query",
                    "message": f"Query could not be parsed: {e}",
                },
                status_code=400,
            )

        # The reranker name comes from the running service so clients
        # can tell which L2 stage produced the order — useful for
        # debugging and for cache busting on the webapp side.
        reranker_name = type(query_svc.hybrid.reranker).__name__

        log.info(
            "GET /api/search — q=%r reranker=%s returned %d results",
            q, reranker_name, len(results),
        )
        return JSONResponse(
            {
                "query": q,
                "limit": limit,
                "offset": offset,
                "sort": sort,
                "reranker": reranker_name,
                "items": [_search_result_dict(r) for r in results],
            }
        )

    @mcp.custom_route(
        "/api/search/answer", methods=["POST"], name="api_search_answer"
    )
    @handler(services_getter, parse_json=True)
    def search_answer(
        request: Request, services: ServicesDict, body: dict
    ) -> JSONResponse:
        """Synthesize a grounded, cited answer to a question.

        Body: ``{q: str, start_date?: ISO, end_date?: ISO}``. Reuses the
        hybrid search top-N as grounding, then asks the configured
        answerer for a strictly-grounded answer. Returns
        ``{question, answer, answered, citations[], model}``. ``answered``
        is false (with a fixed message) when the journal doesn't cover the
        question. 400 ``missing_query`` if ``q`` is empty; 502
        ``answer_unavailable`` if synthesis fails; 503 if not wired.
        """
        answer_svc = services.get("answer")
        if answer_svc is None:
            return JSONResponse(
                {
                    "error": "answer_unavailable",
                    "message": "Answer synthesis is not configured.",
                },
                status_code=503,
            )

        user = get_authenticated_user(request)
        user_id = user.user_id

        q = (body.get("q") or "").strip()
        if not q:
            return JSONResponse(
                {"error": "missing_query", "message": "'q' field is required"},
                status_code=400,
            )

        start_date = body.get("start_date")
        end_date = body.get("end_date")

        try:
            result = answer_svc.answer_question(
                q, start_date=start_date, end_date=end_date, user_id=user_id
            )
        except AnswerUnavailable as e:
            log.info("POST /api/search/answer — answer unavailable for %r: %s", q, e)
            return JSONResponse(
                {
                    "error": "answer_unavailable",
                    "message": "Could not generate an answer right now.",
                },
                status_code=502,
            )

        return JSONResponse(
            {
                "question": result.question,
                "answer": result.answer,
                "answered": result.answered,
                "is_question": result.is_question,
                "citations": [
                    {
                        "entry_id": c.entry_id,
                        "entry_date": c.entry_date,
                        "snippet": c.snippet,
                    }
                    for c in result.citations
                ],
                "model": result.model,
            }
        )
