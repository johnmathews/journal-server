"""Job inspection routes.

- ``GET /api/jobs`` — list jobs with optional filters, newest first.
- ``GET /api/jobs/{job_id}`` — fetch a single job's current state.

Job *creation* lives in ``ingestion.py`` (write/job-creation override of
the URL-prefix routing rule). This module only reads.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

from journal.api._handler import handler
from journal.api._shared import _job_to_dict
from journal.auth import get_authenticated_user

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.db.jobs_repository import SQLiteJobRepository
    from journal.service_registry import ServicesDict

log = logging.getLogger(__name__)


def register_jobs_routes(
    mcp: FastMCP,
    services_getter: Callable[[], ServicesDict | None],
) -> None:
    """Register /api/jobs and /api/jobs/{job_id}."""

    @mcp.custom_route(
        "/api/jobs",
        methods=["GET"],
        name="api_list_jobs",
    )
    @handler(services_getter)
    def list_jobs(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        """List jobs with optional filters, ordered newest first."""
        user = get_authenticated_user(request)
        user_id = None if user.is_admin else user.user_id
        job_repository: SQLiteJobRepository = services["job_repository"]

        status = request.query_params.get("status")
        job_type = request.query_params.get("type")
        try:
            limit = int(request.query_params.get("limit", "50"))
            offset = int(request.query_params.get("offset", "0"))
        except ValueError:
            return JSONResponse(
                {"error": "limit and offset must be integers"},
                status_code=400,
            )

        jobs, total = job_repository.list_jobs(
            status=status,
            job_type=job_type,
            limit=limit,
            offset=offset,
            user_id=user_id,
        )
        log.info(
            "GET /api/jobs — %d jobs (total %d, offset %d)",
            len(jobs),
            total,
            offset,
        )
        return JSONResponse(
            {
                "items": [_job_to_dict(j) for j in jobs],
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    @mcp.custom_route(
        "/api/jobs/{job_id:str}",
        methods=["GET"],
        name="api_job_detail",
    )
    @handler(services_getter)
    def job_detail(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        """Return the current state of a batch job by id.

        404 if the job id is unknown. Otherwise returns the full
        serialised job dict (``_job_to_dict`` shape).
        """
        user = get_authenticated_user(request)
        user_id = None if user.is_admin else user.user_id
        job_repository: SQLiteJobRepository = services["job_repository"]
        job_id = str(request.path_params["job_id"])
        job = job_repository.get(job_id, user_id=user_id)
        if job is None:
            log.info("GET /api/jobs/%s — not found", job_id)
            return JSONResponse({"error": "Job not found"}, status_code=404)
        log.info(
            "GET /api/jobs/%s — status=%s progress=%d/%d",
            job_id,
            job.status,
            job.progress_current,
            job.progress_total,
        )
        return JSONResponse(_job_to_dict(job))
