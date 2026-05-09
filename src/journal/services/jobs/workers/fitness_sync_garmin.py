"""Worker body: run one Garmin fitness sync (fetch + normalize).

Symmetric to ``fitness_sync_strava``. See that module's docstring for
why the worker branches on ``FitnessSyncResult.status`` rather than
catching ``FitnessAuthError`` / transient exceptions.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from journal.services.jobs.errors import friendly_error

if TYPE_CHECKING:
    from journal.services.jobs.workers import WorkerContext

log = logging.getLogger(__name__)


def run_fitness_sync_garmin(
    ctx: WorkerContext, job_id: str, params: dict[str, Any],
) -> None:
    """Execute one Garmin sync job from start to terminal state."""
    user_id = int(params["user_id"])
    try:
        ctx.jobs.mark_running(job_id)
        if ctx.fetch_garmin is None or ctx.normalize_garmin is None:
            ctx.jobs.mark_failed(
                job_id, "Garmin sync is not configured on this server",
            )
            return

        fetch_result = ctx.fetch_garmin(user_id=user_id)

        if fetch_result.status == "auth_broken":
            ctx.jobs.mark_failed(
                job_id,
                "Garmin authorization is broken — please re-authorize",
            )
            return
        if fetch_result.status == "transient_failure":
            ctx.jobs.mark_failed(
                job_id,
                "Garmin sync failed transiently — will retry on next run",
            )
            return
        if fetch_result.status == "running":
            ctx.jobs.mark_succeeded(
                job_id,
                {
                    "skipped": True,
                    "reason": "already_running",
                    "fetch": asdict(fetch_result),
                },
            )
            return

        normalize_result = ctx.normalize_garmin(user_id=user_id)
        result: dict[str, Any] = {
            "fetch": asdict(fetch_result),
            "normalize": asdict(normalize_result),
        }
        ctx.jobs.mark_succeeded(job_id, result)
        ctx.notifier.notify_success(user_id, "fitness_sync_garmin", result)
    except Exception as exc:  # noqa: BLE001 — terminal-state guard
        log.exception("Garmin fitness sync job %s failed", job_id)
        try:
            friendly = friendly_error(exc)
            ctx.jobs.mark_failed(job_id, friendly)
            ctx.notifier.notify_failed(
                user_id, "fitness_sync_garmin", friendly, exc,
            )
        except Exception:  # noqa: BLE001 — last-resort bookkeeping
            log.exception("Failed to record failure for job %s", job_id)
