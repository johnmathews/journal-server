"""Transient-error retry helper shared by the image + audio workers.

Both workers need the same exponential-backoff loop with the same
``status_detail`` updates and the same "notify on first retry" rule.
Pulling it out keeps the worker bodies focused on their actual work
(prepping inputs, calling the ingestion service, writing the result)
instead of duplicating bookkeeping.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from journal.services.jobs.errors import (
    RETRY_DELAYS_SECONDS,
    friendly_error,
    is_transient,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from journal.db.jobs_repository import SQLiteJobRepository
    from journal.services.jobs.notifier import JobNotifier

log = logging.getLogger(__name__)


def run_with_retry[T](
    *,
    jobs: SQLiteJobRepository,
    notifier: JobNotifier,
    job_id: str,
    job_type: str,
    user_id: int | None,
    operation: Callable[[], T],
    log_prefix: str,
) -> T:
    """Run *operation* with exponential-backoff retries on transient errors.

    Mirrors the behaviour the image and audio workers used to inline:

    - On a transient error (``is_transient``), wait ``RETRY_DELAYS_SECONDS[attempt]``
      seconds then retry, up to ``len(RETRY_DELAYS_SECONDS)`` retries.
    - During each wait, the job's ``status_detail`` is set to a
      friendly "retrying in N minutes at HH:MM" string so the webapp
      can show progress; cleared on the next attempt.
    - The user is notified once on the first retry only — subsequent
      retries don't push, to avoid spamming.
    - Non-transient errors raise immediately.

    Returns the operation's return value on success. Raises the last
    exception on exhausted retries or on a non-transient error.
    """
    last_exc: Exception | None = None
    for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
        try:
            if attempt > 0:
                jobs.update_status_detail(job_id, None)
                log.info(
                    "%s job %s — retry attempt %d", log_prefix, job_id, attempt,
                )
            return operation()
        except Exception as exc:  # noqa: BLE001 — re-raised below
            last_exc = exc
            if not is_transient(exc) or attempt >= len(RETRY_DELAYS_SECONDS):
                break  # non-transient or out of retries
            delay = RETRY_DELAYS_SECONDS[attempt]
            delay_minutes = delay // 60
            retry_at_local = (
                datetime.now().astimezone() + timedelta(seconds=delay)
            )
            retry_time = retry_at_local.strftime("%H:%M")
            friendly = friendly_error(exc)
            detail = (
                f"{friendly}, retrying in {delay_minutes} minutes "
                f"at {retry_time}"
            )
            log.warning(
                "%s job %s — transient error, retrying in %ds "
                "(attempt %d): %s",
                log_prefix, job_id, delay, attempt + 1, exc,
            )
            jobs.update_status_detail(job_id, detail)
            # Notify on first retry only.
            if attempt == 0:
                notifier.notify_retrying(
                    user_id, job_type, attempt + 1, delay, friendly, exc,
                )
            time.sleep(delay)

    assert last_exc is not None  # noqa: S101 — loop ran at least once
    raise last_exc
