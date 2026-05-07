"""Save-entry pipeline orchestration.

The PATCH-text path of `/api/entries/{id}` queues *three* background
jobs (reprocess_embeddings, entity_extraction, mood_score_entry) and
wants ONE consolidated Pushover covering them. This module owns the
shape of that fan-out: a synthetic parent job marked succeeded
immediately with a `follow_up_jobs` map, plus deferred dispatches so
all of the API-thread's SQLite writes finish before any worker
starts (see ``journal/260507-item-1-save-pipeline-race-fix.md`` for
the original race that motivated the deferred-dispatch shape).

Lives in its own module so ``runner.py`` can stay focused on the
submitter dispatch surface; this orchestrator is the only `submit_*`
that fans out into multiple children + a defensive notification
sweep.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from journal.services.jobs.validation import (
    ENTITY_EXTRACTION_KEYS,
    MOOD_SCORE_ENTRY_KEYS,
    REPROCESS_EMBEDDINGS_KEYS,
    SAVE_ENTRY_PIPELINE_KEYS,
    validate_params,
)
from journal.services.jobs.workers.entity_extraction import (
    run_entity_extraction,
)
from journal.services.jobs.workers.mood_score_entry import (
    run_mood_score_entry,
)
from journal.services.jobs.workers.reprocess_embeddings import (
    run_reprocess_embeddings,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import ThreadPoolExecutor

    from journal.db.jobs_repository import SQLiteJobRepository
    from journal.models import Job
    from journal.services.jobs.notifier import JobNotifier
    from journal.services.jobs.workers import WorkerContext


def submit_save_entry_pipeline(
    *,
    jobs: SQLiteJobRepository,
    executor: ThreadPoolExecutor,
    ctx: WorkerContext,
    notifier: JobNotifier,
    entry_id: int,
    user_id: int | None,
    enable_mood_scoring: bool,
) -> tuple[Job, dict[str, str]]:
    """Queue the three background jobs that run after an entry edit
    and orchestrate ONE consolidated Pushover for them.

    Creates a synthetic parent job of type ``save_entry_pipeline``
    with no actual worker — it is marked succeeded immediately with::

        result = {
            "entry_id": entry_id,
            "follow_up_jobs": {key -> child_job_id},
        }

    Each child (``reprocess_embeddings``, ``entity_extraction``,
    and optionally ``mood_score_entry``) is submitted with
    ``parent_job_id`` set. Children's workers detect the
    ``compressed_all`` strategy and skip BOTH per-child success and
    failure pushovers, deferring to
    ``JobNotifier.try_pipeline_notification``. The last child to
    reach a terminal state emits one consolidated Pushover.

    Returns ``(parent_job, follow_ups)``.
    """
    # ``notify_strategy`` lives in params (fixed at creation), not
    # result, so it is visible to children's strategy checks from the
    # moment the parent row exists — no need for a separate "mark
    # succeeded with strategy" pre-write that would double the SQLite
    # contention against the worker thread.
    params: dict[str, Any] = {
        "entry_id": entry_id,
        "notify_strategy": "compressed_all",
    }
    if user_id is not None:
        params["user_id"] = user_id
    validate_params(
        params, SAVE_ENTRY_PIPELINE_KEYS, job_type="save_entry_pipeline",
    )

    parent = jobs.create("save_entry_pipeline", params, user_id=user_id)
    follow_ups: dict[str, str] = {}

    # Defer every child's executor.submit until AFTER this thread is
    # done writing to SQLite. The connection is opened with
    # check_same_thread=False and shared with the worker thread; if a
    # child starts executing while this thread is still calling
    # _jobs.create / mark_succeeded, the two threads collide on the
    # connection and one commit fails with
    # ``sqlite3.OperationalError: not an error``. The fix is to do
    # all of this thread's writes first, then dispatch workers in a
    # batch — single-worker executor + serialised dispatch keeps the
    # worker thread idle until we are done.
    deferred: list[Callable[[], None]] = []

    def _stage_child(
        job_type: str,
        keys: frozenset[str],
        child_params: dict[str, Any],
        run_method: Callable[..., Any],
        follow_up_key: str,
    ) -> None:
        validate_params(child_params, keys, job_type=job_type)
        child = jobs.create(job_type, child_params, user_id=user_id)
        follow_ups[follow_up_key] = child.id
        deferred.append(
            lambda jid=child.id, p=child_params: executor.submit(
                run_method, ctx, jid, p,
            ),
        )

    reprocess_params: dict[str, Any] = {
        "entry_id": entry_id, "parent_job_id": parent.id,
    }
    if user_id is not None:
        reprocess_params["user_id"] = user_id
    _stage_child(
        "reprocess_embeddings", REPROCESS_EMBEDDINGS_KEYS,
        reprocess_params, run_reprocess_embeddings,
        "reprocess_embeddings",
    )

    extraction_params: dict[str, Any] = {
        "entry_id": entry_id, "parent_job_id": parent.id,
    }
    if user_id is not None:
        extraction_params["user_id"] = user_id
    _stage_child(
        "entity_extraction", ENTITY_EXTRACTION_KEYS,
        extraction_params, run_entity_extraction,
        "entity_extraction",
    )

    if enable_mood_scoring:
        mood_params: dict[str, Any] = {
            "entry_id": entry_id, "parent_job_id": parent.id,
        }
        if user_id is not None:
            mood_params["user_id"] = user_id
        _stage_child(
            "mood_score_entry", MOOD_SCORE_ENTRY_KEYS,
            mood_params, run_mood_score_entry,
            "mood_scoring",
        )

    # Until this UPDATE lands, ``try_pipeline_notification`` calls
    # from finishing children see ``parent.status != "succeeded"``
    # and return early — the defensive sweep below covers the rare
    # case where every child completed before this point.
    jobs.mark_succeeded(
        parent.id,
        {"entry_id": entry_id, "follow_up_jobs": follow_ups},
    )

    # All API-thread writes have committed; safe to release the
    # workers. The single-worker executor will drain them in FIFO
    # order without contending against this thread.
    for dispatch in deferred:
        dispatch()

    # Defensive sweep: if every child completed in the brief window
    # before mark_succeeded landed (their pipeline checks would have
    # returned early seeing parent still queued), trigger one more
    # check from this thread so the consolidated push still fires.
    # ``try_pipeline_notification`` uses
    # ``try_acquire_notification_lock`` to dedupe against any
    # concurrent worker call.
    notifier.try_pipeline_notification(parent.id, user_id)

    parent_final = jobs.get(parent.id)
    assert parent_final is not None  # noqa: S101 — just created above
    return parent_final, follow_ups
