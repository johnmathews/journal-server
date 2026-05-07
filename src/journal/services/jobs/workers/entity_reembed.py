"""Worker body: refresh a single entity's stored embedding."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from journal.services.jobs.errors import friendly_error

if TYPE_CHECKING:
    from journal.services.jobs.workers import WorkerContext

log = logging.getLogger(__name__)


def run_entity_reembed(
    ctx: WorkerContext, job_id: str, params: dict[str, Any],
) -> None:
    """Execute one entity-reembed job from start to terminal state.

    Same exception-safety guarantee as every other worker: a stuck-
    running row is strictly worse than a failed row, so every exit
    path lands in ``mark_succeeded`` or ``mark_failed``.
    """
    try:
        ctx.jobs.mark_running(job_id)
        ctx.jobs.update_progress(job_id, 0, 1)
        entity_id = int(params["entity_id"])
        job_user_id = int(params["user_id"])

        summary = ctx.reembedder.reembed_entity_for_description(
            entity_id, user_id=job_user_id,
        )
        ctx.jobs.update_progress(job_id, 1, 1)
        ctx.jobs.mark_succeeded(job_id, summary)
        ctx.notifier.notify_success(job_user_id, "entity_reembed", summary)
    except Exception as exc:  # noqa: BLE001 — terminal-state guard
        log.exception("Entity reembed job %s failed", job_id)
        try:
            friendly = friendly_error(exc)
            ctx.jobs.mark_failed(job_id, friendly)
            ctx.notifier.notify_failed(
                params.get("user_id"), "entity_reembed", friendly, exc,
            )
        except Exception:  # noqa: BLE001 — last-resort bookkeeping
            log.exception("Failed to record failure for job %s", job_id)
