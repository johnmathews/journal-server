"""Worker body: extract entities + relationships from one or many entries."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from journal.services.jobs.errors import friendly_error

if TYPE_CHECKING:
    from journal.services.jobs.workers import WorkerContext

log = logging.getLogger(__name__)


def run_entity_extraction(
    ctx: WorkerContext, job_id: str, params: dict[str, Any],
) -> None:
    """Execute one entity-extraction job from start to terminal state.

    Must never let an exception escape: a "stuck running" row is
    strictly worse than a "failed" row for the UI. Any exception
    raised by the underlying extraction is caught, logged, and
    recorded via ``mark_failed``.
    """
    try:
        ctx.jobs.mark_running(job_id)

        def progress_callback(current: int, total: int) -> None:
            ctx.jobs.update_progress(job_id, current, total)

        entry_id = params.get("entry_id")
        job_user_id = params.get("user_id")
        if entry_id is not None:
            # Single-entry path: extract_from_entry has no native
            # on_progress hook, so bracket the call with manual
            # (0, 1) and (1, 1) updates.
            progress_callback(0, 1)
            result_obj = ctx.extraction.extract_from_entry(int(entry_id))
            results = [result_obj]
            progress_callback(1, 1)
        else:
            results = ctx.extraction.extract_batch(
                start_date=params.get("start_date"),
                end_date=params.get("end_date"),
                stale_only=bool(params.get("stale_only", False)),
                on_progress=progress_callback,
                user_id=job_user_id,
            )

        summary: dict[str, Any] = {
            "entries_processed": len(results),
            "entities_created": sum(r.entities_created for r in results),
            "entities_matched": sum(r.entities_matched for r in results),
            "entities_deleted": sum(r.entities_deleted for r in results),
            "mentions_created": sum(r.mentions_created for r in results),
            "relationships_created": sum(
                r.relationships_created for r in results
            ),
            "warnings": [w for r in results for w in r.warnings],
        }
        ctx.jobs.mark_succeeded(job_id, summary)

        # Single-entry extraction corresponds to one newly-ingested (or
        # edited) entry: now that its entity mentions are committed, kick
        # off the storyline extension check. Doing it here — rather than
        # as a concurrent sibling of this job — guarantees the classifier's
        # entity-overlap signal sees the mentions we just wrote. Batch
        # extraction (no entry_id) deliberately skips this to avoid fanning
        # out one check per entry. The bound runner callable no-ops when
        # storylines aren't wired and logs (never silently drops) when the
        # user is unknown.
        if entry_id is not None and ctx.queue_storyline_extension_check is not None:
            ctx.queue_storyline_extension_check(int(entry_id), job_user_id)

        parent_job_id = params.get("parent_job_id")
        if parent_job_id:
            ctx.notifier.try_pipeline_notification(parent_job_id, job_user_id)
        else:
            ctx.notifier.notify_success(
                job_user_id, "entity_extraction", summary,
            )
    except Exception as exc:  # noqa: BLE001 — terminal-state guard
        log.exception("Entity extraction job %s failed", job_id)
        try:
            friendly = friendly_error(exc)
            ctx.jobs.mark_failed(job_id, friendly)
            parent_job_id = params.get("parent_job_id")
            # Compressed-all pipelines (edit save) defer the failure
            # push to the pipeline-level summary so the user gets one
            # consolidated message instead of one per stage.
            if ctx.notifier.get_notify_strategy(parent_job_id) != "compressed_all":
                ctx.notifier.notify_failed(
                    params.get("user_id"), "entity_extraction",
                    friendly, exc,
                )
            if parent_job_id:
                ctx.notifier.try_pipeline_notification(
                    parent_job_id, params.get("user_id"),
                )
        except Exception:  # noqa: BLE001 — last-resort bookkeeping
            log.exception("Failed to record failure for job %s", job_id)
