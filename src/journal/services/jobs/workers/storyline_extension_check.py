"""Worker body: classify whether a new entry extends each active storyline.

Fires after ingestion (queued from ``_queue_post_ingestion_jobs``).
For each ``yes`` decision, queues a follow-up
``storyline_generation`` job via the runner's
``submit_storyline_generation``. ``maybe`` decisions are recorded
on the job result for future surfacing; ``no`` decisions are simply
counted.

The classifier handles its own per-storyline timestamp update
(``record_extension_check``), so this worker is a thin orchestrator
on top of the classifier.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from journal.services.jobs.errors import friendly_error

if TYPE_CHECKING:
    from collections.abc import Callable

    from journal.services.jobs.workers import WorkerContext

log = logging.getLogger(__name__)


def run_storyline_extension_check(
    ctx: WorkerContext,
    job_id: str,
    params: dict[str, Any],
    submit_regenerate: Callable[..., Any],
) -> None:
    """Classify ``entry_id`` against each active storyline, queue
    regenerations for positive matches.

    ``submit_regenerate`` is the runner's
    ``submit_storyline_generation`` bound method, passed in so the
    worker stays free of any runner-side reach-in. ``user_id`` is
    required in ``params``; ``parent_job_id`` is honoured if set.
    """
    user_id = params["user_id"]
    parent_job_id = params.get("parent_job_id")
    entry_id = params["entry_id"]

    try:
        ctx.jobs.mark_running(job_id)
        ctx.jobs.update_progress(job_id, 0, 1)

        if ctx.storyline_extension_classifier is None:
            error_msg = (
                "StorylineExtensionClassifier not configured; "
                "cannot run extension check."
            )
            ctx.jobs.mark_failed(job_id, error_msg)
            if ctx.notifier.get_notify_strategy(parent_job_id) != "compressed_all":
                ctx.notifier.notify_failed(
                    user_id, "storyline_extension_check", error_msg,
                )
            if parent_job_id:
                ctx.notifier.try_pipeline_notification(parent_job_id, user_id)
            return

        results = ctx.storyline_extension_classifier.classify_for_entry(
            entry_id=entry_id, user_id=user_id,
        )

        regenerated_storyline_ids: list[int] = []
        for r in results:
            if r.decision == "yes":
                try:
                    submit_regenerate(
                        r.storyline_id,
                        user_id=user_id,
                        # Ingest path opts into auto-split: an over-budget
                        # open chapter is re-segmented automatically (W5).
                        auto_split=True,
                    )
                    regenerated_storyline_ids.append(r.storyline_id)
                except Exception:  # noqa: BLE001 — log + continue
                    log.exception(
                        "Failed to queue regeneration for storyline %d (entry %d)",
                        r.storyline_id, entry_id,
                    )

        ctx.jobs.update_progress(job_id, 1, 1)
        summary: dict[str, Any] = {
            "entry_id": entry_id,
            "classifications": [
                {
                    "storyline_id": r.storyline_id,
                    "decision": r.decision,
                    "stage": r.stage,
                    "reasoning": r.reasoning,
                }
                for r in results
            ],
            "regenerations_queued": regenerated_storyline_ids,
        }
        ctx.jobs.mark_succeeded(job_id, summary)

        if parent_job_id:
            ctx.notifier.try_pipeline_notification(parent_job_id, user_id)
        # No success notification by default — this fires on every
        # ingestion and would be noisy. Failures still notify.
    except Exception as exc:  # noqa: BLE001 — terminal-state guard
        log.exception("Storyline extension check job %s failed", job_id)
        try:
            friendly = friendly_error(exc)
            ctx.jobs.mark_failed(job_id, friendly)
            if ctx.notifier.get_notify_strategy(parent_job_id) != "compressed_all":
                ctx.notifier.notify_failed(
                    user_id, "storyline_extension_check", friendly, exc,
                )
            if parent_job_id:
                ctx.notifier.try_pipeline_notification(parent_job_id, user_id)
        except Exception:  # noqa: BLE001
            log.exception("Failed to record failure for job %s", job_id)
