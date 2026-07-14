"""Fitness job-creation routes (sync + backfill).

This module is part of the write/job-creation routing override documented
in ``code-quality-principles.md`` § "Routing rules for src/journal/api/".
Both routes queue background fetch jobs, so they follow the override
rather than living beside the reads in ``api/fitness.py``. They were
originally bundled into ``api/ingestion.py`` with the rest of the
override family, and were carved into this sibling module when that file
outgrew the ~800-line size rule — a sanctioned split of the override,
not a new deviation category.

Concretely, this module owns:

- ``POST /api/fitness/sync/{source}``     — async fitness fetch+normalize job
- ``POST /api/fitness/backfill/{source}`` — async fitness historical backfill job

Read routes (``GET /api/fitness/*``) live in ``api/fitness.py``; the
per-user auth flows live in ``api/fitness_garmin.py`` and
``api/fitness_strava.py``.

**Rule for new routes.** If a new fitness route's primary effect is
"create a job that does work", it goes here; reads go in
``api/fitness.py``. New deviation categories require updating
``code-quality-principles.md`` and ``api/_shared.py``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

from journal.api._handler import handler
from journal.api._shared import STRAVA_DISABLED_ERROR, _strava_enabled
from journal.auth import get_authenticated_user

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.service_registry import ServicesDict
    from journal.services.jobs import JobRunner

log = logging.getLogger(__name__)


def register_fitness_jobs_routes(
    mcp: FastMCP,
    services_getter: Callable[[], ServicesDict | None],
) -> None:
    """Register the fitness job-creation routes (see module docstring)."""

    @mcp.custom_route(
        "/api/fitness/sync/{source:str}",
        methods=["POST"],
        name="api_fitness_sync",
    )
    @handler(services_getter)
    def fitness_sync(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
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
        user = get_authenticated_user(request)
        user_id = user.user_id
        source = str(request.path_params["source"])
        if source not in ("strava", "garmin"):
            return JSONResponse(
                {"error": f"Unknown fitness source: {source!r}"},
                status_code=400,
            )
        # W1 strava-mothball: with STRAVA_ENABLED=false the Strava job
        # surface is gone (404), Garmin is untouched.
        if source == "strava" and not _strava_enabled(services):
            return JSONResponse(
                {"error": STRAVA_DISABLED_ERROR}, status_code=404,
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
    @handler(services_getter, parse_json="raw")
    def fitness_backfill(
        request: Request, services: ServicesDict, raw: bytes
    ) -> JSONResponse:
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
        user = get_authenticated_user(request)
        user_id = user.user_id
        source = str(request.path_params["source"])
        if source not in ("strava", "garmin"):
            return JSONResponse(
                {"error": f"Unknown fitness source: {source!r}"},
                status_code=400,
            )
        # W1 strava-mothball: same 404 gate as the sync route above.
        if source == "strava" and not _strava_enabled(services):
            return JSONResponse(
                {"error": STRAVA_DISABLED_ERROR}, status_code=404,
            )

        # Parse in-body ("raw" mode): the source-validation 400 above
        # must keep precedence over body-shape 400s.
        try:
            body = json.loads(raw)
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
