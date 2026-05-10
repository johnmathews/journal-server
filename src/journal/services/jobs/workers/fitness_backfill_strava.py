"""Worker body: run one Strava historical backfill (W5).

Wraps :func:`journal.services.fitness.backfill.backfill_strava` so the
existing CLI-only backfill orchestrator is reachable as a queued job.
The orchestrator already drives the W6 fetch service per 30-day
window — this worker is just the queue → orchestrator shim plus the
terminal-state bookkeeping every worker shares.

Terminal-state guarantee mirrors :mod:`fitness_sync_strava`: every exit
path lands in ``mark_succeeded`` or ``mark_failed``. The orchestrator's
``aborted_*`` final-status values map onto ``mark_succeeded`` (the job
itself ran to completion — the *backfill* was aborted, not the job)
with the abort reason surfaced in the result payload so the webapp can
show "10/30 windows completed, then auth broke" rather than a binary
failure.
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


def run_fitness_backfill_strava(
    ctx: WorkerContext, job_id: str, params: dict[str, Any],
) -> None:
    """Execute one Strava backfill job from start to terminal state."""
    user_id = int(params["user_id"])
    start = str(params["start"])
    end_raw = params.get("end")
    end = str(end_raw) if end_raw is not None else None
    try:
        ctx.jobs.mark_running(job_id)
        if ctx.backfill_strava is None:
            ctx.jobs.mark_failed(
                job_id, "Strava backfill is not configured on this server",
            )
            return

        try:
            result = ctx.backfill_strava(
                user_id=user_id, start=start, end=end,
            )
        except BackfillBlocked as exc:
            # The orchestrator's "another sync is in flight" guard.
            # With the W5 spanning idempotency check at submit time this
            # is now improbable (the colliding job would have been
            # detected before enqueue) — but the orchestrator and the
            # submit-time dedup are independent and a window-boundary
            # race could still surface it. Treat as a clean failure
            # with a message the UI can show.
            ctx.jobs.mark_failed(job_id, str(exc))
            return

        result_dict = asdict(result)
        ctx.jobs.mark_succeeded(job_id, result_dict)
        ctx.notifier.notify_success(
            user_id, "fitness_backfill_strava", result_dict,
        )
    except Exception as exc:  # noqa: BLE001 — terminal-state guard
        log.exception("Strava fitness backfill job %s failed", job_id)
        try:
            friendly = friendly_error(exc)
            ctx.jobs.mark_failed(job_id, friendly)
            ctx.notifier.notify_failed(
                user_id, "fitness_backfill_strava", friendly, exc,
            )
        except Exception:  # noqa: BLE001 — last-resort bookkeeping
            log.exception("Failed to record failure for job %s", job_id)
