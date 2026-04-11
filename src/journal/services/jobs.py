"""Async batch job runner.

The `JobRunner` service is the glue between "please run this batch"
requests arriving from the API/MCP layer and the long-running
entity-extraction / mood-backfill workloads living in the rest of
the services package.

It owns:

- Param-shape validation (reject unknown keys up front rather than
  letting the worker discover them mid-flight).
- Lifecycle bookkeeping against the `jobs` table: queued -> running
  -> (succeeded | failed), with per-step progress updates driven by
  the `on_progress` callback contract added in Work Unit 1.
- A single-worker `ThreadPoolExecutor` that serialises every
  submitted job. One worker gives us three things at once:
    1. Predictable LLM rate usage (no contention for tokens).
    2. A clean story for the shared SQLite connection opened with
       `check_same_thread=False` — only one thread writes at a time,
       so WAL + NORMAL synchronous stays safe.
    3. Simpler failure reasoning: jobs cannot be racing each other.

IMPORTANT: bumping `max_workers` above 1 is a serious change. The
SQLite connection opened by the server for jobs use is shared
across threads under the explicit assumption that this executor
serialises access to it (see `db/connection.py` docstring). If that
invariant is relaxed, writes from multiple worker threads on a
single connection with WAL + NORMAL synchronous is NOT safe and
the schema access pattern must be redesigned first.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from journal.db.jobs_repository import SQLiteJobRepository
    from journal.db.repository import EntryRepository
    from journal.models import Job
    from journal.services.backfill import MoodBackfillResult
    from journal.services.entity_extraction import EntityExtractionService
    from journal.services.mood_scoring import MoodScoringService

log = logging.getLogger(__name__)


# --------------------------------------------------------------------
# Param validation
# --------------------------------------------------------------------

_ENTITY_EXTRACTION_KEYS: dict[str, type | tuple[type, ...]] = {
    "entry_id": int,
    "start_date": str,
    "end_date": str,
    "stale_only": bool,
}

_MOOD_BACKFILL_KEYS: dict[str, type | tuple[type, ...]] = {
    "mode": str,
    "start_date": str,
    "end_date": str,
}

_MOOD_BACKFILL_MODES = frozenset({"stale-only", "force"})


def _validate_params(
    params: dict[str, Any],
    allowed: dict[str, type | tuple[type, ...]],
    *,
    job_type: str,
) -> None:
    """Reject params with unknown keys or wrong value types.

    Booleans are a subclass of int in Python, so `stale_only=True`
    would incorrectly satisfy `int` typing. We handle that by
    checking bool BEFORE the generic isinstance when int is allowed
    but bool is not, and vice versa.
    """
    unknown = set(params) - set(allowed)
    if unknown:
        raise ValueError(
            f"Unknown params for {job_type}: {sorted(unknown)}"
        )
    for key, value in params.items():
        expected = allowed[key]
        # Python quirk: bool is a subclass of int. Disallow the
        # cross-type acceptance that isinstance would otherwise
        # silently allow.
        if expected is int and isinstance(value, bool):
            raise ValueError(
                f"Param {key!r} for {job_type} must be int, "
                f"got bool ({value!r})"
            )
        if expected is bool and not isinstance(value, bool):
            raise ValueError(
                f"Param {key!r} for {job_type} must be bool, "
                f"got {type(value).__name__} ({value!r})"
            )
        if not isinstance(value, expected):  # type: ignore[arg-type]
            raise ValueError(
                f"Param {key!r} for {job_type} must be "
                f"{expected}, got {type(value).__name__} ({value!r})"
            )


# --------------------------------------------------------------------
# JobRunner
# --------------------------------------------------------------------


class JobRunner:
    """Run background batch jobs serialised on a single worker.

    Uses a single-worker `ThreadPoolExecutor` so jobs are serialised
    — this keeps LLM rate-limiting simple and guarantees the shared
    SQLite connection (opened with `check_same_thread=False`) only
    receives one writer at a time.

    IMPORTANT: if `max_workers` is ever bumped above 1, the SQLite
    threading assumption (see `db/connection.py` docstring) must be
    re-examined. Writes from multiple threads on a single
    connection with WAL + NORMAL synchronous is NOT safe.
    """

    def __init__(
        self,
        *,
        job_repository: SQLiteJobRepository,
        entity_extraction_service: EntityExtractionService,
        mood_backfill_callable: Callable[..., MoodBackfillResult],
        mood_scoring_service: MoodScoringService,
        entry_repository: EntryRepository,
    ) -> None:
        self._jobs = job_repository
        self._extraction = entity_extraction_service
        self._mood_backfill = mood_backfill_callable
        self._mood_scoring = mood_scoring_service
        self._entries = entry_repository
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="journal-jobs"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit_entity_extraction(self, params: dict[str, Any]) -> Job:
        """Queue an entity-extraction batch job.

        Accepts any subset of `entry_id`, `start_date`, `end_date`,
        `stale_only`. Unknown keys or wrong types raise
        `ValueError` before a row is inserted.
        """
        _validate_params(
            params, _ENTITY_EXTRACTION_KEYS, job_type="entity_extraction"
        )
        job = self._jobs.create("entity_extraction", params)
        self._executor.submit(self._run_entity_extraction, job.id, params)
        return job

    def submit_mood_backfill(self, params: dict[str, Any]) -> Job:
        """Queue a mood-backfill batch job.

        Requires `mode` to be present and set to either
        `"stale-only"` or `"force"`. Accepts optional `start_date`
        and `end_date`. Unknown keys or wrong types raise
        `ValueError` before a row is inserted.
        """
        _validate_params(
            params, _MOOD_BACKFILL_KEYS, job_type="mood_backfill"
        )
        if "mode" not in params:
            raise ValueError(
                "Param 'mode' is required for mood_backfill"
            )
        if params["mode"] not in _MOOD_BACKFILL_MODES:
            raise ValueError(
                f"Param 'mode' must be one of "
                f"{sorted(_MOOD_BACKFILL_MODES)}, got {params['mode']!r}"
            )
        job = self._jobs.create("mood_backfill", params)
        self._executor.submit(self._run_mood_backfill, job.id, params)
        return job

    def shutdown(self, wait: bool = False) -> None:
        """Stop the executor, cancelling queued-but-not-started tasks.

        Call once at server shutdown. Running tasks are allowed to
        finish their current iteration; queued tasks are cancelled.
        After `shutdown`, further `submit_*` calls raise
        `RuntimeError` from the underlying executor.
        """
        self._executor.shutdown(wait=wait, cancel_futures=True)

    # ------------------------------------------------------------------
    # Worker bodies
    # ------------------------------------------------------------------

    def _run_entity_extraction(
        self, job_id: str, params: dict[str, Any]
    ) -> None:
        """Execute one entity-extraction job from start to terminal state.

        Must never let an exception escape: a "stuck running" row
        is strictly worse than a "failed" row for the UI. Any
        exception raised by the underlying extraction is caught,
        logged, and recorded via `mark_failed`.
        """
        try:
            self._jobs.mark_running(job_id)

            def progress_callback(current: int, total: int) -> None:
                self._jobs.update_progress(job_id, current, total)

            entry_id = params.get("entry_id")
            if entry_id is not None:
                # Single-entry path: extract_from_entry has no
                # native on_progress hook, so bracket the call with
                # manual (0, 1) and (1, 1) updates.
                progress_callback(0, 1)
                result_obj = self._extraction.extract_from_entry(
                    int(entry_id)
                )
                results = [result_obj]
                progress_callback(1, 1)
            else:
                results = self._extraction.extract_batch(
                    start_date=params.get("start_date"),
                    end_date=params.get("end_date"),
                    stale_only=bool(params.get("stale_only", False)),
                    on_progress=progress_callback,
                )

            summary: dict[str, Any] = {
                "processed": len(results),
                "entities_created": sum(
                    r.entities_created for r in results
                ),
                "entities_matched": sum(
                    r.entities_matched for r in results
                ),
                "mentions_created": sum(
                    r.mentions_created for r in results
                ),
                "relationships_created": sum(
                    r.relationships_created for r in results
                ),
                "warnings": [w for r in results for w in r.warnings],
            }
            self._jobs.mark_succeeded(job_id, summary)
        except Exception as exc:  # noqa: BLE001 — terminal-state guard
            log.exception(
                "Entity extraction job %s failed", job_id
            )
            try:
                self._jobs.mark_failed(job_id, str(exc))
            except Exception:  # noqa: BLE001 — last-resort bookkeeping
                log.exception(
                    "Failed to record failure for job %s", job_id
                )

    def _run_mood_backfill(
        self, job_id: str, params: dict[str, Any]
    ) -> None:
        """Execute one mood-backfill job from start to terminal state.

        Same guarantee as `_run_entity_extraction`: always reaches a
        terminal state, never lets exceptions escape the executor.
        """
        try:
            self._jobs.mark_running(job_id)

            def progress_callback(current: int, total: int) -> None:
                self._jobs.update_progress(job_id, current, total)

            backfill_result = self._mood_backfill(
                repository=self._entries,
                mood_scoring=self._mood_scoring,
                mode=params["mode"],
                start_date=params.get("start_date"),
                end_date=params.get("end_date"),
                on_progress=progress_callback,
            )

            summary: dict[str, Any] = {
                "scored": backfill_result.scored,
                "skipped": backfill_result.skipped,
                "errors": list(backfill_result.errors),
            }
            self._jobs.mark_succeeded(job_id, summary)
        except Exception as exc:  # noqa: BLE001 — terminal-state guard
            log.exception("Mood backfill job %s failed", job_id)
            try:
                self._jobs.mark_failed(job_id, str(exc))
            except Exception:  # noqa: BLE001 — last-resort bookkeeping
                log.exception(
                    "Failed to record failure for job %s", job_id
                )
