"""Fitness pipeline read-side routes.

Owns the four GET endpoints under ``/api/fitness/``:

- ``GET /api/fitness/activities?start=&end=&type=`` — windowed activities.
- ``GET /api/fitness/daily?start=&end=`` — windowed daily rollups.
- ``GET /api/fitness/sync/status`` — per-source auth + last-runs snapshot.
- ``GET /api/fitness/integrity`` — soft-pointer orphan report.

Job creation (``POST /api/fitness/sync/{source}``) lives in
``api/ingestion.py`` per the routing override (write/job creation —
see ``api/_shared.py``'s docstring). This module only reads.

Auth is enforced by ``RequireAuthMiddleware``: every route below assumes
``request.user`` is an :class:`AuthenticatedUser`. The per-route
``get_authenticated_user`` call extracts the user_id for query scoping.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse

from journal.auth import get_authenticated_user
from journal.db.fitness_integrity import check_fitness_integrity

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.db.fitness_repository import FitnessRepository
    from journal.models import (
        FitnessActivity,
        FitnessAuthState,
        FitnessDaily,
        FitnessSyncRun,
    )

log = logging.getLogger(__name__)

_VALID_SOURCES = ("strava", "garmin")


def _activity_to_dict(a: FitnessActivity) -> dict[str, Any]:
    return {
        "id": a.id,
        "user_id": a.user_id,
        "source": a.source,
        "source_id": a.source_id,
        "activity_type": a.activity_type,
        "source_subtype": a.source_subtype,
        "start_time": a.start_time,
        "local_date": a.local_date,
        "duration_s": a.duration_s,
        "moving_time_s": a.moving_time_s,
        "distance_m": a.distance_m,
        "elevation_gain_m": a.elevation_gain_m,
        "avg_hr_bpm": a.avg_hr_bpm,
        "max_hr_bpm": a.max_hr_bpm,
        "avg_pace_s_per_km": a.avg_pace_s_per_km,
        "calories_kcal": a.calories_kcal,
        "perceived_exertion": a.perceived_exertion,
        "extras": a.extras,
        "raw_ref_id": a.raw_ref_id,
        "normalized_at": a.normalized_at,
    }


def _daily_to_dict(d: FitnessDaily) -> dict[str, Any]:
    return {
        "id": d.id,
        "user_id": d.user_id,
        "source": d.source,
        "local_date": d.local_date,
        "sleep_score": d.sleep_score,
        "sleep_duration_s": d.sleep_duration_s,
        "sleep_efficiency_pct": d.sleep_efficiency_pct,
        "hrv_overnight_ms": d.hrv_overnight_ms,
        "resting_hr_bpm": d.resting_hr_bpm,
        "body_battery_high": d.body_battery_high,
        "body_battery_low": d.body_battery_low,
        "stress_avg": d.stress_avg,
        "training_load_acute": d.training_load_acute,
        "training_load_chronic": d.training_load_chronic,
        "training_readiness": d.training_readiness,
        "extras": d.extras,
        "raw_ref_ids": d.raw_ref_ids,
        "normalized_at": d.normalized_at,
    }


def _sync_run_to_dict(run: FitnessSyncRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "status": run.status,
        "rows_fetched": run.rows_fetched,
        "rows_normalized": run.rows_normalized,
        "error_class": run.error_class,
        "error_message": run.error_message,
    }


def _per_source_status(
    repo: FitnessRepository,
    *,
    user_id: int,
    source: str,
) -> dict[str, Any] | None:
    """Return the status payload for *source*, or ``None`` if this user
    has never had any fitness activity on this source — i.e. no
    ``fitness_auth_state`` row AND no ``fitness_sync_runs`` rows.

    Returning ``None`` (rather than a default-populated dict) lets the
    webapp tell "first-use, never connected" apart from "configured but
    no successful sync yet" — only the first deserves the connect CTA.
    """
    auth: FitnessAuthState | None = repo.get_auth_state(
        user_id=user_id, source=source,
    )
    last_runs = repo.list_recent_sync_runs(
        user_id=user_id, source=source, limit=10,
    )
    if auth is None and not last_runs:
        return None
    last_success_at = repo.last_successful_sync_at(
        user_id=user_id, source=source,
    )
    return {
        "auth_status": auth.auth_status if auth is not None else "unknown",
        "auth_broken_since": auth.auth_broken_since if auth is not None else None,
        "last_success_at": last_success_at,
        "last_runs": [_sync_run_to_dict(r) for r in last_runs],
    }


def _missing_param(name: str) -> JSONResponse:
    return JSONResponse(
        {"error": f"Query parameter '{name}' is required"},
        status_code=400,
    )


def register_fitness_routes(
    mcp: FastMCP,
    services_getter: Callable[[], dict | None],
) -> None:
    """Register the four ``GET /api/fitness/*`` read routes.

    The job-creation companion (``POST /api/fitness/sync/{source}``)
    is registered by ``register_ingestion_routes`` per the routing
    override.
    """

    @mcp.custom_route(
        "/api/fitness/activities",
        methods=["GET"],
        name="api_fitness_list_activities",
    )
    async def list_activities(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        user = get_authenticated_user(request)
        repo: FitnessRepository = services["fitness_repo"]

        start = request.query_params.get("start")
        end = request.query_params.get("end")
        if not start:
            return _missing_param("start")
        if not end:
            return _missing_param("end")
        activity_type = request.query_params.get("type")

        activities = repo.list_activities(
            user_id=user.user_id,
            start=start,
            end=end,
            activity_type=activity_type,
        )
        log.info(
            "GET /api/fitness/activities — %d items (%s..%s, type=%s)",
            len(activities), start, end, activity_type,
        )
        return JSONResponse({"items": [_activity_to_dict(a) for a in activities]})

    @mcp.custom_route(
        "/api/fitness/daily",
        methods=["GET"],
        name="api_fitness_list_daily",
    )
    async def list_daily(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        user = get_authenticated_user(request)
        repo: FitnessRepository = services["fitness_repo"]

        start = request.query_params.get("start")
        end = request.query_params.get("end")
        if not start:
            return _missing_param("start")
        if not end:
            return _missing_param("end")

        daily = repo.list_daily(user_id=user.user_id, start=start, end=end)
        log.info(
            "GET /api/fitness/daily — %d items (%s..%s)",
            len(daily), start, end,
        )
        return JSONResponse({"items": [_daily_to_dict(d) for d in daily]})

    @mcp.custom_route(
        "/api/fitness/sync/status",
        methods=["GET"],
        name="api_fitness_sync_status",
    )
    async def sync_status(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        user = get_authenticated_user(request)
        repo: FitnessRepository = services["fitness_repo"]

        body = {
            source: _per_source_status(repo, user_id=user.user_id, source=source)
            for source in _VALID_SOURCES
        }
        log.info(
            "GET /api/fitness/sync/status — strava=%s garmin=%s",
            "configured" if body["strava"] else "null",
            "configured" if body["garmin"] else "null",
        )
        return JSONResponse(body)

    @mcp.custom_route(
        "/api/fitness/integrity",
        methods=["GET"],
        name="api_fitness_integrity",
    )
    async def integrity(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        # Auth required even though the integrity check itself is
        # global — there is no per-user filter on raw orphans, so an
        # un-authed read would leak the existence of orphans across
        # tenants. (The pipeline is single-user today, but keeping the
        # gate consistent with every other route avoids a regression
        # if a second tenant is ever added.)
        get_authenticated_user(request)
        conn: sqlite3.Connection = services["db_conn"]

        report = check_fitness_integrity(conn)
        body = {
            "activities": [asdict(o) for o in report.activities],
            "daily": [asdict(o) for o in report.daily],
        }
        log.info(
            "GET /api/fitness/integrity — %d activity orphans, %d daily orphans",
            len(report.activities), len(report.daily),
        )
        return JSONResponse(body)
