"""Worker body: score one entry's mood dimensions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from journal.services.jobs.errors import friendly_error

if TYPE_CHECKING:
    from journal.services.jobs.workers import WorkerContext

log = logging.getLogger(__name__)


def run_mood_score_entry(
    ctx: WorkerContext, job_id: str, params: dict[str, Any],
) -> None:
    """Score a single entry's mood dimensions."""
    try:
        ctx.jobs.mark_running(job_id)
        ctx.jobs.update_progress(job_id, 0, 1)

        entry_id = params["entry_id"]
        parent_job_id = params.get("parent_job_id")
        entry = ctx.entries.get_entry(entry_id)
        if entry is None:
            error_msg = f"Entry {entry_id} not found"
            ctx.jobs.mark_failed(job_id, error_msg)
            if ctx.notifier.get_notify_strategy(parent_job_id) != "compressed_all":
                ctx.notifier.notify_failed(
                    params.get("user_id"), "mood_score_entry", error_msg,
                )
            if parent_job_id:
                ctx.notifier.try_pipeline_notification(
                    parent_job_id, params.get("user_id"),
                )
            return

        text = entry.final_text or entry.raw_text
        if not text or not text.strip():
            error_msg = f"Entry {entry_id} has no text"
            ctx.jobs.mark_failed(job_id, error_msg)
            if ctx.notifier.get_notify_strategy(parent_job_id) != "compressed_all":
                ctx.notifier.notify_failed(
                    params.get("user_id"), "mood_score_entry", error_msg,
                )
            if parent_job_id:
                ctx.notifier.try_pipeline_notification(
                    parent_job_id, params.get("user_id"),
                )
            return

        count = ctx.mood_scoring.score_entry(entry_id, text)
        ctx.jobs.update_progress(job_id, 1, 1)
        result = {"entry_id": entry_id, "scores_written": count}
        ctx.jobs.mark_succeeded(job_id, result)
        if parent_job_id:
            ctx.notifier.try_pipeline_notification(
                parent_job_id, params.get("user_id"),
            )
        else:
            ctx.notifier.notify_success(
                params.get("user_id"), "mood_score_entry", result,
            )
    except Exception as exc:  # noqa: BLE001 — terminal-state guard
        log.exception("Mood score entry job %s failed", job_id)
        try:
            friendly = friendly_error(exc)
            ctx.jobs.mark_failed(job_id, friendly)
            parent_job_id = params.get("parent_job_id")
            if ctx.notifier.get_notify_strategy(parent_job_id) != "compressed_all":
                ctx.notifier.notify_failed(
                    params.get("user_id"), "mood_score_entry",
                    friendly, exc,
                )
            if parent_job_id:
                ctx.notifier.try_pipeline_notification(
                    parent_job_id, params.get("user_id"),
                )
        except Exception:  # noqa: BLE001 — last-resort bookkeeping
            log.exception("Failed to record failure for job %s", job_id)
