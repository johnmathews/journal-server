"""Health and stats routes.

- ``GET /health`` — unauthenticated operational health (bypasses bearer auth).
- ``GET /api/health`` — authenticated mirror of ``/health`` for the webapp,
  whose nginx only proxies ``/api/*`` to the server.
- ``GET /api/stats`` — per-user journal statistics.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse

from journal.auth import get_authenticated_user
from journal.services.liveness import (
    check_api_key,
    check_chromadb,
    check_sqlite,
    overall_status,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.services.query import QueryService

log = logging.getLogger(__name__)


def register_health_routes(
    mcp: FastMCP,
    services_getter: Callable[[], dict | None],
) -> None:
    """Register /health, /api/health, and /api/stats routes."""

    @mcp.custom_route("/health", methods=["GET"], name="api_health")
    async def get_health(request: Request) -> JSONResponse:
        """Operational health endpoint. Bypasses bearer auth.

        Returns three blocks:

        - `ingestion`: corpus stats (total/last-7d/last-30d counts,
          by source type, avg words/chunks, last ingestion timestamp,
          per-table row counts).
        - `queries`: per-query-type counts and p50/p95/p99 latency,
          plus server uptime and start timestamp.
        - `status`: per-component check list (sqlite, chromadb,
          anthropic, openai) plus the worst-of rollup.

        Most-frequent search terms are deliberately NOT exposed —
        they'd leak what the user was curious about and `/health`
        is unauthenticated on loopback-only deployments. The query
        stats block carries counts-by-type only.
        """
        services = services_getter()
        if services is None:
            return JSONResponse(
                {
                    "status": "error",
                    "message": "Server not initialized",
                },
                status_code=503,
            )

        query_svc: QueryService = services["query"]
        config = services.get("config")
        stats_collector = services.get("stats")

        # Ingestion stats block — pure SQL aggregation.
        try:
            ingestion = query_svc._repo.get_ingestion_stats(now=datetime.now(UTC))
            ingestion_dict: dict[str, Any] = asdict(ingestion)
        except sqlite3.Error as e:
            log.warning("GET /health — ingestion stats failed: %s", e)
            ingestion_dict = {"error": str(e)}

        # Query stats block — from the in-memory collector.
        if stats_collector is not None:
            snap = stats_collector.snapshot()
            queries_dict: dict[str, Any] = {
                "total_queries": snap.total_queries,
                "uptime_seconds": snap.uptime_seconds,
                "started_at": snap.started_at,
                "by_type": {
                    name: {
                        "count": ts.count,
                        "latency": asdict(ts.latency),
                    }
                    for name, ts in snap.by_type.items()
                },
            }
        else:
            queries_dict = {
                "total_queries": 0,
                "uptime_seconds": 0.0,
                "started_at": None,
                "by_type": {},
            }

        # Liveness checks. Each is independent and returns a
        # ComponentCheck so the overall rollup can be computed at the end.
        checks = [
            check_sqlite(query_svc._repo._conn),
            check_chromadb(query_svc._vector_store),
        ]
        if config is not None:
            checks.append(check_api_key("anthropic", config.anthropic_api_key))
            checks.append(check_api_key("openai", config.openai_api_key))

        status = overall_status(checks)
        log.info(
            "GET /health — status=%s total_queries=%d total_entries=%s",
            status,
            queries_dict.get("total_queries", 0),
            ingestion_dict.get("total_entries", "n/a"),
        )
        return JSONResponse(
            {
                "status": status,
                "checks": [asdict(c) for c in checks],
                "ingestion": ingestion_dict,
                "queries": queries_dict,
            }
        )

    @mcp.custom_route("/api/health", methods=["GET"], name="api_health_authed")
    async def get_health_authed(request: Request) -> JSONResponse:
        """Authenticated mirror of /health for the webapp.

        The webapp's nginx only proxies /api/* to the server, so the
        unauthenticated /health path is unreachable from the browser.
        This route serves the same payload under /api/ (with auth).
        """
        return await get_health(request)

    @mcp.custom_route("/api/stats", methods=["GET"], name="api_stats")
    async def get_stats(request: Request) -> JSONResponse:
        """Get journal statistics."""
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)

        query_svc: QueryService = services["query"]
        user = get_authenticated_user(request)
        user_id = user.user_id

        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        stats = query_svc.get_statistics(start_date, end_date, user_id=user_id)
        log.info("GET /api/stats — %d entries, %d words", stats.total_entries, stats.total_words)
        return JSONResponse(asdict(stats))
