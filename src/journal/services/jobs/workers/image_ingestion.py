"""Worker body: ingest one or more images into a single entry."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from journal.services.jobs.errors import friendly_error, is_transient
from journal.services.jobs.retry import run_with_retry

if TYPE_CHECKING:
    from journal.services.jobs.workers import WorkerContext

log = logging.getLogger(__name__)


def run_image_ingestion(
    ctx: WorkerContext, job_id: str, params: dict[str, Any],
) -> None:
    """Execute one image-ingestion job from start to terminal state.

    Transient API errors (503 overload, 429 rate limit) trigger
    automatic retries with exponential backoff (3 min, 6 min, 12 min).
    The job stays in ``running`` state during waits and
    ``status_detail`` shows the next retry time so the webapp can
    display it.
    """
    job_user_id: int | None = None
    try:
        ctx.jobs.mark_running(job_id)
        images_with_names = ctx.pop_pending_images(job_id)
        if not images_with_names:
            ctx.jobs.mark_failed(job_id, "No image data found for job")
            return
        if ctx.ingestion is None:
            ctx.jobs.mark_failed(job_id, "Ingestion service not available")
            return

        # Strip filenames — IngestionService expects (bytes, media_type)
        images: list[tuple[bytes, str]] = [
            (data, media_type)
            for data, media_type, _filename in images_with_names
        ]
        entry_date = params["entry_date"]
        job_user_id = params.get("user_id")
        total = len(images)

        def progress_callback(current: int, _total_pages: int) -> None:
            ctx.jobs.update_progress(job_id, current, total)

        def operation():  # noqa: ANN202 — local helper
            assert ctx.ingestion is not None  # noqa: S101 — guarded above
            if len(images) == 1:
                ctx.jobs.update_progress(job_id, 0, total)
                entry = ctx.ingestion.ingest_image(
                    images[0][0], images[0][1], entry_date,
                    skip_mood=True, user_id=job_user_id or 1,
                )
                ctx.jobs.update_progress(job_id, 1, total)
            else:
                ctx.jobs.update_progress(job_id, 0, total)
                entry = ctx.ingestion.ingest_multi_page_entry(
                    images, entry_date, skip_mood=True,
                    on_progress=progress_callback,
                    user_id=job_user_id or 1,
                )
            return entry

        entry = run_with_retry(
            jobs=ctx.jobs,
            notifier=ctx.notifier,
            job_id=job_id,
            job_type="ingest_images",
            user_id=job_user_id,
            operation=operation,
            log_prefix="Image ingestion",
        )

        ctx.jobs.update_progress(job_id, total, total)

        # Queue follow-up jobs: mood scoring + entity extraction.
        follow_up_ids = ctx.queue_post_ingestion_jobs(
            job_id, "Image", entry.id, job_user_id,
        )

        result: dict[str, Any] = {
            "entry_id": entry.id,
            "entry_date": entry.entry_date,
            "source_type": entry.source_type,
            "word_count": entry.word_count,
            "chunk_count": entry.chunk_count,
            "page_count": total,
            "follow_up_jobs": follow_up_ids,
        }
        ctx.jobs.mark_succeeded(job_id, result)
        if not follow_up_ids:
            # No follow-ups were queued (e.g. executor shutting down) —
            # notify directly so the user learns the entry was created.
            # If follow-ups were queued the combined pipeline notification
            # fires when the last one completes.
            ctx.notifier.notify_success(job_user_id, "ingest_images", result)
    except Exception as exc:  # noqa: BLE001 — terminal-state guard
        log.exception("Image ingestion job %s failed", job_id)
        # Clean up any remaining image data
        ctx.pop_pending_images(job_id)
        try:
            friendly = friendly_error(exc)
            if is_transient(exc):
                friendly += " — please try again later"
            ctx.jobs.mark_failed(job_id, friendly)
            ctx.notifier.notify_failed(
                job_user_id, "ingest_images", friendly, exc,
            )
        except Exception:  # noqa: BLE001 — last-resort bookkeeping
            log.exception("Failed to record failure for job %s", job_id)
