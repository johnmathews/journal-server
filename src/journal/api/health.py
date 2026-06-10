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
    check_fitness_freshness,
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

    def _build_health_payload(
        services: dict,
        *,
        fitness_user_id: int | None,
        include_stats: bool,
    ) -> dict[str, Any]:
        """Shared payload builder for `/health` and `/api/health`.

        `include_stats` is True only on the authenticated route
        (`/api/health`): the `ingestion` (corpus counts, per-table rows,
        recent activity) and `queries` (counts + latency) blocks are
        usage statistics, not liveness data, so the unauthenticated
        `/health` must not carry them (W7/B15) — an anonymous probe
        would otherwise learn how much and how recently the user
        journals. `/health` keeps only `status` + `checks`.

        `fitness_user_id` is likewise non-None only on the authenticated
        route. When set, the per-user fitness block is added and
        `check_fitness_freshness` participates in the overall rollup.
        The unauthenticated `/health` deliberately does not include it —
        `auth_broken_since` is per-user state and an anonymous probe
        would be able to enumerate which users have configured
        Strava/Garmin and when their auth broke (W12).
        """
        query_svc: QueryService = services["query"]
        config = services.get("config")
        stats_collector = services.get("stats")

        payload: dict[str, Any] = {}

        if include_stats:
            try:
                ingestion = query_svc.get_ingestion_stats(now=datetime.now(UTC))
                ingestion_dict: dict[str, Any] = asdict(ingestion)
            except sqlite3.Error as e:
                log.warning("GET /health — ingestion stats failed: %s", e)
                ingestion_dict = {"error": str(e)}

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

            payload["ingestion"] = ingestion_dict
            payload["queries"] = queries_dict

        checks = [
            check_sqlite(query_svc.connection),
            check_chromadb(query_svc.vector_store),
        ]
        if config is not None:
            checks.append(check_api_key("anthropic", config.anthropic_api_key))
            checks.append(check_api_key("openai", config.openai_api_key))

        # Fitness block only on the authenticated path. Omitted entirely
        # when the user has no auth_state and no sync_runs — do not
        # emit a stub of nulls.
        if fitness_user_id is not None:
            fitness_repo = services.get("fitness_repo")
            if fitness_repo is not None:
                summary = fitness_repo.get_health_summary(
                    user_id=fitness_user_id,
                )
                if summary:
                    payload["fitness"] = {
                        row["source"]: {
                            "auth_status": row["auth_status"],
                            "last_success_at": row["last_success_at"],
                            "auth_broken_since": row["auth_broken_since"],
                        }
                        for row in summary
                    }
                    threshold = (
                        config.fitness_health_broken_degraded_hours
                        if config is not None
                        else 48
                    )
                    checks.append(
                        check_fitness_freshness(
                            summary=summary,
                            threshold_hours=threshold,
                        ),
                    )

        payload["status"] = overall_status(checks)
        payload["checks"] = [asdict(c) for c in checks]
        return payload

    @mcp.custom_route("/health", methods=["GET"], name="api_health")
    async def get_health(request: Request) -> JSONResponse:
        """Operational health endpoint. Bypasses bearer auth.

        Liveness only:

        - `status`: worst-of rollup across the component checks.
        - `checks`: per-component check list (sqlite, chromadb,
          anthropic, openai).

        Corpus statistics (`ingestion`) and query stats (`queries`)
        are deliberately NOT included here — this endpoint is
        unauthenticated, and entry counts / recent-activity numbers
        would tell an anonymous probe how much and how recently the
        user journals. Search terms and per-user fitness state are
        similarly omitted. See the authenticated `/api/health` for
        the full payload.
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

        payload = _build_health_payload(
            services, fitness_user_id=None, include_stats=False,
        )
        log.info("GET /health — status=%s", payload["status"])
        return JSONResponse(payload)

    @mcp.custom_route("/api/health", methods=["GET"], name="api_health_authed")
    async def get_health_authed(request: Request) -> JSONResponse:
        """Authenticated mirror of `/health` for the webapp.

        Carries the liveness fields from `/health` plus the full stats
        payload: `ingestion` (corpus stats: total/last-7d/last-30d
        counts, by source type, avg words/chunks, last ingestion
        timestamp, per-table row counts) and `queries` (per-query-type
        counts and p50/p95/p99 latency, plus server uptime and start
        timestamp) — and the per-user `fitness` block:
        one entry per source the user has configured (auth_state row)
        or interacted with (sync_runs), carrying `auth_status`,
        `last_success_at`, `auth_broken_since`. The overall status
        downgrades to `degraded` if any source has been broken for
        longer than `FITNESS_HEALTH_BROKEN_DEGRADED_HOURS` (default
        48h).
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

        user = get_authenticated_user(request)
        payload = _build_health_payload(
            services, fitness_user_id=user.user_id, include_stats=True,
        )
        log.info(
            "GET /api/health — status=%s user_id=%d fitness_sources=%d",
            payload["status"],
            user.user_id,
            len(payload.get("fitness", {})),
        )
        return JSONResponse(payload)

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
