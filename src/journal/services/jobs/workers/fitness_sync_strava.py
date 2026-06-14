"""Worker body: run one Strava fitness sync (fetch + normalize).

Branches on ``FitnessSyncResult.status`` because the W6 fetch service
swallows non-auth exceptions and surfaces them as
``status="transient_failure"`` (auth failures become
``status="auth_broken"``, with the Pushover already fired). The worker
itself never sees ``FitnessAuthError`` from a healthy ``run_sync`` —
it only re-raises through the terminal-state guard for genuinely
unexpected exceptions (a programming bug or an OOM).
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from journal.services.jobs.errors import friendly_error

if TYPE_CHECKING:
    from journal.services.jobs.workers import WorkerContext

log = logging.getLogger(__name__)


def run_fitness_sync_strava(
    ctx: WorkerContext, job_id: str, params: dict[str, Any],
) -> None:
    """Execute one Strava sync job from start to terminal state.

    Same terminal-state guarantee as every other worker: every exit
    path lands in ``mark_succeeded`` or ``mark_failed``. The auth /
    transient short-circuits delegate user-facing alerting to the
    fetch service (which already fired the right Pushover before
    returning) and only need to record a failed jobs row so the
    webapp UI reflects the outcome.
    """
    user_id = int(params["user_id"])
    try:
        ctx.jobs.mark_running(job_id)
        if ctx.fetch_strava is None or ctx.normalize_strava is None:
            ctx.jobs.mark_failed(
                job_id, "Strava sync is not configured on this server",
            )
            return

        fetch_result = ctx.fetch_strava(user_id=user_id)

        if fetch_result.status == "auth_broken":
            ctx.jobs.mark_failed(
                job_id,
                "Strava authorization is broken — please re-authorize",
            )
            return
        if fetch_result.status == "transient_failure":
            ctx.jobs.mark_failed(
                job_id,
                "Strava sync failed transiently — will retry on next run",
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

        normalize_result = ctx.normalize_strava(
            user_id=user_id, sync_run_id=fetch_result.run_id,
        )
        result: dict[str, Any] = {
            "fetch": asdict(fetch_result),
            "normalize": asdict(normalize_result),
        }
        ctx.jobs.mark_succeeded(job_id, result)
        # Scheduled (quiet_success) syncs stay silent on a no-op run — a
        # successful fetch that returned zero new rows. Auth/transient
        # failures notify above; manual syncs always notify here.
        quiet = bool(params.get("quiet_success")) and fetch_result.rows_fetched == 0
        if not quiet:
            ctx.notifier.notify_success(user_id, "fitness_sync_strava", result)
    except Exception as exc:  # noqa: BLE001 — terminal-state guard
        log.exception("Strava fitness sync job %s failed", job_id)
        try:
            friendly = friendly_error(exc)
            ctx.jobs.mark_failed(job_id, friendly)
            ctx.notifier.notify_failed(
                user_id, "fitness_sync_strava", friendly, exc,
            )
        except Exception:  # noqa: BLE001 — last-resort bookkeeping
            log.exception("Failed to record failure for job %s", job_id)
