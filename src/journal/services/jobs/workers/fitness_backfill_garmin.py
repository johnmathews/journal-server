"""Worker body: run one Garmin historical backfill (W5).

Symmetric to :mod:`fitness_backfill_strava` — see that module's
docstring for the design rationale (queue → orchestrator shim,
terminal-state guarantee, BackfillBlocked handling).
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from journal.services.fitness.backfill import BackfillBlocked
from journal.services.jobs.errors import friendly_error

if TYPE_CHECKING:
    from journal.services.jobs.workers import WorkerContext

log = logging.getLogger(__name__)


def run_fitness_backfill_garmin(
    ctx: WorkerContext, job_id: str, params: dict[str, Any],
) -> None:
    """Execute one Garmin backfill job from start to terminal state."""
    user_id = int(params["user_id"])
    start = str(params["start"])
    end_raw = params.get("end")
    end = str(end_raw) if end_raw is not None else None
    try:
        ctx.jobs.mark_running(job_id)
        if ctx.backfill_garmin is None:
            ctx.jobs.mark_failed(
                job_id, "Garmin backfill is not configured on this server",
            )
            return

        try:
            result = ctx.backfill_garmin(
                user_id=user_id, start=start, end=end,
            )
        except BackfillBlocked as exc:
            ctx.jobs.mark_failed(job_id, str(exc))
            return

        result_dict = asdict(result)
        ctx.jobs.mark_succeeded(job_id, result_dict)
        ctx.notifier.notify_success(
            user_id, "fitness_backfill_garmin", result_dict,
        )
    except Exception as exc:  # noqa: BLE001 — terminal-state guard
        log.exception("Garmin fitness backfill job %s failed", job_id)
        try:
            friendly = friendly_error(exc)
            ctx.jobs.mark_failed(job_id, friendly)
            ctx.notifier.notify_failed(
                user_id, "fitness_backfill_garmin", friendly, exc,
            )
        except Exception:  # noqa: BLE001 — last-resort bookkeeping
            log.exception("Failed to record failure for job %s", job_id)
