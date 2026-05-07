"""Notification dispatch + pipeline-level summary helpers.

Wraps the four ``notify_*`` paths the workers call, plus the
``compressed_*`` pipeline-strategy bookkeeping. Lives separately from
``JobRunner`` so worker functions can take a ``JobNotifier`` parameter
and be tested without standing up the executor.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from journal.db.jobs_repository import SQLiteJobRepository
    from journal.services.notifications import PushoverNotificationService

log = logging.getLogger(__name__)


class JobNotifier:
    """Per-worker notification helper. One instance per ``JobRunner``.

    Construction takes only the bits of state notifications actually
    need: the jobs repository (for pipeline parent lookups + the
    ``try_acquire_notification_lock`` claim) and an optional
    ``PushoverNotificationService``. The single ``_notifications`` is
    optional so the runner can run in test scenarios where push is
    unavailable.
    """

    def __init__(
        self,
        *,
        jobs: SQLiteJobRepository,
        notifications: PushoverNotificationService | None,
    ) -> None:
        self._jobs = jobs
        self._notifications = notifications

    # ── per-job notifications ────────────────────────────────────────

    def notify_success(
        self, user_id: int | None, job_type: str, result: dict[str, Any],
    ) -> None:
        if self._notifications is not None and user_id is not None:
            try:
                self._notifications.notify_job_success(user_id, job_type, result)
            except Exception:  # noqa: BLE001
                log.warning("Notification send failed (success)", exc_info=True)

    def notify_failed(
        self,
        user_id: int | None,
        job_type: str,
        error_msg: str,
        exc: Exception | None = None,
    ) -> None:
        if self._notifications is not None and user_id is not None:
            try:
                self._notifications.notify_job_failed(
                    user_id, job_type, error_msg, exc,
                )
                self._notifications.notify_admin_job_failed(
                    user_id, job_type, error_msg, exc,
                )
            except Exception:  # noqa: BLE001
                log.warning("Notification send failed (failure)", exc_info=True)

    def notify_retrying(
        self,
        user_id: int | None,
        job_type: str,
        attempt: int,
        delay: int,
        error_msg: str,
        exc: Exception | None = None,
    ) -> None:
        if self._notifications is not None and user_id is not None:
            try:
                self._notifications.notify_job_retrying(
                    user_id, job_type, attempt, delay, error_msg, exc,
                )
            except Exception:  # noqa: BLE001
                log.warning("Notification send failed (retry)", exc_info=True)

    # ── pipeline-strategy + summary ─────────────────────────────────

    def get_notify_strategy(self, parent_job_id: str | None) -> str:
        """Return the notification strategy for a pipeline parent.

        The strategy is stored in ``parent.params`` (fixed at parent
        creation), not ``parent.result`` (only available after
        ``mark_succeeded``). Reading from params makes the strategy
        visible to fast-failing children before the parent has been
        marked succeeded, eliminating a double-write that would
        otherwise contend with the worker thread on the shared SQLite
        connection.

        Values:
          ``"none"`` — caller has no parent; treat as standalone.
          ``"compressed_success_only"`` (default for legacy parents
            that do not set a strategy) — failures fire ``notify_failed``
            immediately, success fires once via pipeline summary.
          ``"compressed_all"`` — failures AND successes are deferred
            to the pipeline summary; one push covers everything.
        """
        if not parent_job_id:
            return "none"
        parent = self._jobs.get(parent_job_id)
        if parent is None:
            return "compressed_success_only"
        return parent.params.get(
            "notify_strategy", "compressed_success_only",
        )

    def try_pipeline_notification(
        self, parent_job_id: str, user_id: int | None,
    ) -> None:
        """Send one combined notification when all pipeline jobs finish.

        Called by follow-up job runners on completion; the method
        checks whether all sibling follow-ups have also reached a
        terminal state. Only the last one to finish actually sends
        the notification — earlier callers return early because a
        sibling is still running.

        Behavior depends on the parent's ``notify_strategy``:

        - ``compressed_success_only`` (legacy ingestion pipelines):
          fires ``notify_success`` summarising what worked. Failed
          children already fired their own ``notify_failed``.
        - ``compressed_all`` (save-entry edit pipeline): if any child
          failed, fires ``notify_pipeline_failed`` with a per-stage
          breakdown. Otherwise fires ``notify_success`` like the
          legacy path.

        Concurrent callers (worker thread + the API thread's defensive
        sweep in ``submit_save_entry_pipeline``) are deduped via
        ``try_acquire_notification_lock`` — only the first caller
        actually sends the push.
        """
        from journal.services.notifications import (
            build_pipeline_failure_body,
        )

        parent = self._jobs.get(parent_job_id)
        if parent is None or parent.status != "succeeded":
            return

        parent_result: dict[str, Any] = parent.result or {}
        follow_up_ids: dict[str, str] = parent_result.get(
            "follow_up_jobs", {},
        )
        if not follow_up_ids:
            return

        # Gather per-child terminal state. Bail if any follow-up
        # hasn't reached a terminal state yet.
        follow_up_results: dict[str, dict[str, Any]] = {}
        follow_up_failures: dict[str, str] = {}
        for key, fj_id in follow_up_ids.items():
            fj = self._jobs.get(fj_id)
            if fj is None or fj.status not in ("succeeded", "failed"):
                return  # still running — wait for next completion
            if fj.status == "succeeded":
                follow_up_results[key] = fj.result or {}
            else:
                follow_up_failures[key] = (
                    fj.error_message or "unknown error"
                )

        combined: dict[str, Any] = dict(parent_result)
        for key, fj_result in follow_up_results.items():
            combined[f"{key}_result"] = fj_result

        # Atomically claim the right to fire the notification — guards
        # against double-firing when the API thread's defensive sweep
        # races with the last worker's call.
        if not self._jobs.try_acquire_notification_lock(parent_job_id):
            return

        strategy = parent.params.get(
            "notify_strategy", "compressed_success_only",
        )

        if strategy == "compressed_all" and follow_up_failures:
            # Per-stage breakdown: list successes and failures with the
            # captured error_message.
            if self._notifications is not None and user_id is not None:
                try:
                    body = build_pipeline_failure_body(
                        parent.type, combined, follow_up_failures,
                    )
                    self._notifications.notify_pipeline_failed(
                        user_id, parent.type, body,
                    )
                except Exception:  # noqa: BLE001
                    log.warning(
                        "Pipeline failure notification failed",
                        exc_info=True,
                    )
            return

        # All children succeeded (or strategy is the legacy success-only
        # mode) — fire the success summary using the parent's job type
        # so the message-builder dispatches correctly.
        self.notify_success(user_id, parent.type, combined)
