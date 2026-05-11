"""Direct unit tests for the fitness_backfill_{strava,garmin} workers (W5).

Symmetric to ``test_worker_fitness_sync`` — minimal WorkerContext built
from fakes, the new ``backfill_*`` seam wired to canned outcomes. The
seam mirrors what ``mcp_server/bootstrap.py`` wires in production
(a closure over the per-source fetch service + repo + notifier, taking
only ``(user_id, start, end)``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from journal.db.factory import ConnectionFactory
from journal.db.jobs_repository import SQLiteJobRepository
from journal.db.migrations import run_migrations
from journal.services.fitness.backfill import BackfillBlocked, BackfillResult
from journal.services.jobs.notifier import JobNotifier
from journal.services.jobs.workers import WorkerContext
from journal.services.jobs.workers.fitness_backfill_garmin import (
    run_fitness_backfill_garmin,
)
from journal.services.jobs.workers.fitness_backfill_strava import (
    run_fitness_backfill_strava,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@pytest.fixture
def jobs_factory(tmp_path: Path) -> ConnectionFactory:
    db_path = tmp_path / "jobs.db"
    factory = ConnectionFactory(db_path)
    run_migrations(factory.get())
    return factory


@pytest.fixture
def jobs_repo(jobs_factory: ConnectionFactory) -> SQLiteJobRepository:
    return SQLiteJobRepository(jobs_factory)


def _make_ctx(
    *,
    jobs_repo: SQLiteJobRepository,
    backfill_strava: Callable[..., BackfillResult] | None = None,
    backfill_garmin: Callable[..., BackfillResult] | None = None,
    notifications: object | None = None,
) -> WorkerContext:
    notifier = JobNotifier(jobs=jobs_repo, notifications=notifications)
    return WorkerContext(
        jobs=jobs_repo,
        notifier=notifier,
        extraction=MagicMock(name="EntityExtractionService"),
        reembedder=MagicMock(name="EntityReembedder"),
        mood_backfill=MagicMock(name="mood_backfill_callable"),
        mood_scoring=MagicMock(name="MoodScoringService"),
        entries=MagicMock(name="EntryRepository"),
        ingestion=None,
        pop_pending_images=lambda _jid: [],
        pop_pending_audio=lambda _jid: [],
        queue_post_ingestion_jobs=lambda *_args: {},
        backfill_strava=backfill_strava,
        backfill_garmin=backfill_garmin,
    )


class _Recorder:
    def __init__(self, value: Any) -> None:
        self.value = value
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class TestRunFitnessBackfillStrava:
    def test_success_marks_succeeded_with_orchestrator_result(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        backfill = _Recorder(BackfillResult(
            source="strava", final_status="complete",
            windows_attempted=2, windows_succeeded=2,
            rows_fetched=5, rows_normalized=5,
        ))
        ctx = _make_ctx(jobs_repo=jobs_repo, backfill_strava=backfill)
        job = jobs_repo.create(
            "fitness_backfill_strava",
            {"user_id": 1, "start": "2026-01-01", "end": "2026-02-01"},
            user_id=1,
        )

        run_fitness_backfill_strava(
            ctx, job.id,
            {"user_id": 1, "start": "2026-01-01", "end": "2026-02-01"},
        )

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.result is not None
        assert final.result["source"] == "strava"
        assert final.result["final_status"] == "complete"
        assert final.result["windows_succeeded"] == 2
        # Orchestrator received the right window args.
        assert backfill.calls == [
            {"user_id": 1, "start": "2026-01-01", "end": "2026-02-01"},
        ]

    def test_end_optional_passes_none_through_to_orchestrator(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        """The orchestrator resolves ``end=None`` to today (UTC) — the
        worker must pass None through unchanged when params lack `end`."""
        backfill = _Recorder(BackfillResult(
            source="strava", final_status="complete",
            windows_attempted=1, windows_succeeded=1,
            rows_fetched=0, rows_normalized=0,
        ))
        ctx = _make_ctx(jobs_repo=jobs_repo, backfill_strava=backfill)
        job = jobs_repo.create(
            "fitness_backfill_strava",
            {"user_id": 1, "start": "2026-01-01"}, user_id=1,
        )

        run_fitness_backfill_strava(
            ctx, job.id, {"user_id": 1, "start": "2026-01-01"},
        )

        assert backfill.calls == [
            {"user_id": 1, "start": "2026-01-01", "end": None},
        ]
        assert jobs_repo.get(job.id).status == "succeeded"  # type: ignore[union-attr]

    def test_unconfigured_marks_failed_without_calling_orchestrator(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        ctx = _make_ctx(jobs_repo=jobs_repo, backfill_strava=None)
        job = jobs_repo.create(
            "fitness_backfill_strava",
            {"user_id": 1, "start": "2026-01-01"}, user_id=1,
        )

        run_fitness_backfill_strava(
            ctx, job.id, {"user_id": 1, "start": "2026-01-01"},
        )

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "failed"
        assert "not configured" in (final.error_message or "")

    def test_backfill_blocked_marks_failed_cleanly(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        """If the orchestrator's single-run guard trips mid-window (rare
        with the W5 spanning idempotency, but possible if a fetch
        service routine sync slipped in), we record the block as a
        clean job failure."""
        backfill = _Recorder(BackfillBlocked("routine sync in flight"))
        ctx = _make_ctx(jobs_repo=jobs_repo, backfill_strava=backfill)
        job = jobs_repo.create(
            "fitness_backfill_strava",
            {"user_id": 1, "start": "2026-01-01"}, user_id=1,
        )

        run_fitness_backfill_strava(
            ctx, job.id, {"user_id": 1, "start": "2026-01-01"},
        )

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "failed"
        assert "routine sync in flight" in (final.error_message or "")

    def test_unexpected_exception_marks_failed(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        backfill = _Recorder(RuntimeError("boom"))
        ctx = _make_ctx(jobs_repo=jobs_repo, backfill_strava=backfill)
        job = jobs_repo.create(
            "fitness_backfill_strava",
            {"user_id": 1, "start": "2026-01-01"}, user_id=1,
        )

        run_fitness_backfill_strava(
            ctx, job.id, {"user_id": 1, "start": "2026-01-01"},
        )

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "failed"


class TestRunFitnessBackfillGarmin:
    def test_success_marks_succeeded(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        backfill = _Recorder(BackfillResult(
            source="garmin", final_status="complete",
            windows_attempted=1, windows_succeeded=1,
            rows_fetched=6, rows_normalized=4,
        ))
        ctx = _make_ctx(jobs_repo=jobs_repo, backfill_garmin=backfill)
        job = jobs_repo.create(
            "fitness_backfill_garmin",
            {"user_id": 1, "start": "2026-01-01", "end": "2026-01-31"},
            user_id=1,
        )

        run_fitness_backfill_garmin(
            ctx, job.id,
            {"user_id": 1, "start": "2026-01-01", "end": "2026-01-31"},
        )

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.result["source"] == "garmin"  # type: ignore[index]
        assert final.result["rows_normalized"] == 4  # type: ignore[index]

    def test_unconfigured_marks_failed(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        ctx = _make_ctx(jobs_repo=jobs_repo, backfill_garmin=None)
        job = jobs_repo.create(
            "fitness_backfill_garmin",
            {"user_id": 1, "start": "2026-01-01"}, user_id=1,
        )

        run_fitness_backfill_garmin(
            ctx, job.id, {"user_id": 1, "start": "2026-01-01"},
        )

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "failed"
        assert "not configured" in (final.error_message or "")
