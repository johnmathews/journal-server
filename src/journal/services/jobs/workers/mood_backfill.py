"""Worker body: backfill mood scores across many entries."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from journal.services.jobs.errors import friendly_error

if TYPE_CHECKING:
    from journal.services.jobs.workers import WorkerContext

log = logging.getLogger(__name__)


def run_mood_backfill(
    ctx: WorkerContext, job_id: str, params: dict[str, Any],
) -> None:
    """Execute one mood-backfill job from start to terminal state.

    Same guarantee as every other worker: always reaches a terminal
    state, never lets exceptions escape the executor.
    """
    try:
        ctx.jobs.mark_running(job_id)

        def progress_callback(current: int, total: int) -> None:
            ctx.jobs.update_progress(job_id, current, total)

        backfill_result = ctx.mood_backfill(
            repository=ctx.entries,
            mood_scoring=ctx.mood_scoring,
            mode=params["mode"],
            start_date=params.get("start_date"),
            end_date=params.get("end_date"),
            on_progress=progress_callback,
            user_id=params.get("user_id"),
        )

        summary: dict[str, Any] = {
            "scored": backfill_result.scored,
            "skipped": backfill_result.skipped,
            "errors": list(backfill_result.errors),
        }
        ctx.jobs.mark_succeeded(job_id, summary)
        ctx.notifier.notify_success(
            params.get("user_id"), "mood_backfill", summary,
        )
    except Exception as exc:  # noqa: BLE001 — terminal-state guard
        log.exception("Mood backfill job %s failed", job_id)
        try:
            friendly = friendly_error(exc)
            ctx.jobs.mark_failed(job_id, friendly)
            ctx.notifier.notify_failed(
                params.get("user_id"), "mood_backfill", friendly, exc,
            )
        except Exception:  # noqa: BLE001 — last-resort bookkeeping
            log.exception("Failed to record failure for job %s", job_id)
