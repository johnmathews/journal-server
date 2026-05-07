"""Worker body: re-chunk + re-embed an entry's text."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from journal.services.jobs.errors import friendly_error

if TYPE_CHECKING:
    from journal.services.jobs.workers import WorkerContext

log = logging.getLogger(__name__)


def run_reprocess_embeddings(
    ctx: WorkerContext, job_id: str, params: dict[str, Any],
) -> None:
    """Re-chunk and re-embed an entry's text in the background."""
    parent_job_id = params.get("parent_job_id")
    try:
        ctx.jobs.mark_running(job_id)
        ctx.jobs.update_progress(job_id, 0, 1)

        entry_id = params["entry_id"]
        if ctx.ingestion is None:
            error_msg = "Ingestion service not available"
            ctx.jobs.mark_failed(job_id, error_msg)
            if ctx.notifier.get_notify_strategy(parent_job_id) != "compressed_all":
                ctx.notifier.notify_failed(
                    params.get("user_id"), "reprocess_embeddings", error_msg,
                )
            if parent_job_id:
                ctx.notifier.try_pipeline_notification(
                    parent_job_id, params.get("user_id"),
                )
            return

        chunk_count = ctx.ingestion.reprocess_embeddings(entry_id)
        ctx.jobs.update_progress(job_id, 1, 1)
        result = {"entry_id": entry_id, "chunk_count": chunk_count}
        ctx.jobs.mark_succeeded(job_id, result)
        if parent_job_id:
            # Part of a save-entry pipeline — defer to the
            # consolidated pipeline notification.
            ctx.notifier.try_pipeline_notification(
                parent_job_id, params.get("user_id"),
            )
        else:
            ctx.notifier.notify_success(
                params.get("user_id"), "reprocess_embeddings", result,
            )
    except Exception as exc:  # noqa: BLE001 — terminal-state guard
        log.exception("Reprocess embeddings job %s failed", job_id)
        try:
            friendly = friendly_error(exc)
            ctx.jobs.mark_failed(job_id, friendly)
            if ctx.notifier.get_notify_strategy(parent_job_id) != "compressed_all":
                ctx.notifier.notify_failed(
                    params.get("user_id"), "reprocess_embeddings",
                    friendly, exc,
                )
            if parent_job_id:
                ctx.notifier.try_pipeline_notification(
                    parent_job_id, params.get("user_id"),
                )
        except Exception:  # noqa: BLE001 — last-resort bookkeeping
            log.exception("Failed to record failure for job %s", job_id)
