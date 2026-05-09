"""Direct unit tests for the fitness_sync_{strava,garmin} workers.

Builds a minimal WorkerContext from fakes and calls each worker
function directly. The fetch + normalize callables on the context
are the seam: production wires them to ``StravaFetchService.run_sync``
/ ``GarminFetchService.run_sync`` and the free ``normalize_*``
functions; tests inject canned outcomes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from journal.db.connection import get_connection
from journal.db.jobs_repository import SQLiteJobRepository
from journal.db.migrations import run_migrations
from journal.services.fitness.fetch import FitnessSyncResult
from journal.services.fitness.normalize import NormalizeResult
from journal.services.jobs.notifier import JobNotifier
from journal.services.jobs.workers import WorkerContext
from journal.services.jobs.workers.fitness_sync_garmin import (
    run_fitness_sync_garmin,
)
from journal.services.jobs.workers.fitness_sync_strava import (
    run_fitness_sync_strava,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Generator
    from pathlib import Path


@pytest.fixture
def jobs_conn(tmp_path: Path) -> Generator[sqlite3.Connection]:
    db_path = tmp_path / "jobs.db"
    conn = get_connection(db_path, check_same_thread=False)
    run_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def jobs_repo(jobs_conn: sqlite3.Connection) -> SQLiteJobRepository:
    return SQLiteJobRepository(jobs_conn)


def _make_ctx(
    *,
    jobs_repo: SQLiteJobRepository,
    fetch_strava: Callable[..., FitnessSyncResult] | None = None,
    fetch_garmin: Callable[..., FitnessSyncResult] | None = None,
    normalize_strava: Callable[..., NormalizeResult] | None = None,
    normalize_garmin: Callable[..., NormalizeResult] | None = None,
    notifications: object | None = None,
) -> WorkerContext:
    """Build a WorkerContext exposing only what the fitness workers read."""
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
        fetch_strava=fetch_strava
        or (lambda **_kwargs: _missing("fetch_strava")),
        fetch_garmin=fetch_garmin
        or (lambda **_kwargs: _missing("fetch_garmin")),
        normalize_strava=normalize_strava
        or (lambda **_kwargs: _missing("normalize_strava")),
        normalize_garmin=normalize_garmin
        or (lambda **_kwargs: _missing("normalize_garmin")),
    )


def _missing(name: str) -> Any:
    raise AssertionError(f"{name} should not have been called in this test")


class _Recorder:
    """Records calls and returns the canned value."""

    def __init__(self, value: Any) -> None:
        self.value = value
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


# ── Strava ──────────────────────────────────────────────────────────


class TestRunFitnessSyncStrava:
    """Worker is callable without a JobRunner — minimal-context tests."""

    def test_success_runs_fetch_then_normalize_and_marks_succeeded(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        fetch = _Recorder(FitnessSyncResult(
            status="success", run_id=1, rows_fetched=3, rows_normalized=0,
        ))
        norm = _Recorder(NormalizeResult(
            source="strava", rows_normalized=3, drift_count=0,
        ))
        ctx = _make_ctx(
            jobs_repo=jobs_repo, fetch_strava=fetch, normalize_strava=norm,
        )

        job = jobs_repo.create("fitness_sync_strava", {"user_id": 7})
        run_fitness_sync_strava(ctx, job.id, {"user_id": 7})

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.result["fetch"]["rows_fetched"] == 3
        assert final.result["normalize"]["rows_normalized"] == 3
        assert final.result["normalize"]["drift_count"] == 0
        assert fetch.calls == [{"user_id": 7}]
        assert norm.calls == [{"user_id": 7}]

    def test_auth_broken_short_circuits_normalize(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        fetch = _Recorder(FitnessSyncResult(
            status="auth_broken", run_id=2, rows_fetched=0, rows_normalized=0,
        ))
        norm = _Recorder(None)  # asserts not called
        ctx = _make_ctx(
            jobs_repo=jobs_repo, fetch_strava=fetch, normalize_strava=norm,
        )

        job = jobs_repo.create("fitness_sync_strava", {"user_id": 1})
        run_fitness_sync_strava(ctx, job.id, {"user_id": 1})

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "failed"
        assert "auth" in final.error_message.lower()
        assert norm.calls == []  # normalize must not run

    def test_transient_failure_short_circuits_normalize(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        fetch = _Recorder(FitnessSyncResult(
            status="transient_failure", run_id=3,
            rows_fetched=0, rows_normalized=0,
        ))
        norm = _Recorder(None)
        ctx = _make_ctx(
            jobs_repo=jobs_repo, fetch_strava=fetch, normalize_strava=norm,
        )

        job = jobs_repo.create("fitness_sync_strava", {"user_id": 1})
        run_fitness_sync_strava(ctx, job.id, {"user_id": 1})

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "failed"
        assert "transient" in final.error_message.lower()
        assert norm.calls == []

    def test_already_running_marks_succeeded_with_skip_flag(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        fetch = _Recorder(FitnessSyncResult(
            status="running", run_id=42, rows_fetched=0, rows_normalized=0,
        ))
        norm = _Recorder(None)
        ctx = _make_ctx(
            jobs_repo=jobs_repo, fetch_strava=fetch, normalize_strava=norm,
        )

        job = jobs_repo.create("fitness_sync_strava", {"user_id": 1})
        run_fitness_sync_strava(ctx, job.id, {"user_id": 1})

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.result["skipped"] is True
        assert final.result["reason"] == "already_running"
        assert norm.calls == []

    def test_drift_count_recorded_in_result(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        fetch = _Recorder(FitnessSyncResult(
            status="success", run_id=5, rows_fetched=4, rows_normalized=0,
        ))
        norm = _Recorder(NormalizeResult(
            source="strava", rows_normalized=2, drift_count=2,
        ))
        ctx = _make_ctx(
            jobs_repo=jobs_repo, fetch_strava=fetch, normalize_strava=norm,
        )

        job = jobs_repo.create("fitness_sync_strava", {"user_id": 1})
        run_fitness_sync_strava(ctx, job.id, {"user_id": 1})

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.result["normalize"]["drift_count"] == 2

    def test_terminal_state_guard_swallows_unexpected_exception(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        fetch = _Recorder(RuntimeError("kaboom"))
        ctx = _make_ctx(jobs_repo=jobs_repo, fetch_strava=fetch)

        job = jobs_repo.create("fitness_sync_strava", {"user_id": 1})
        # Worker is the executor's terminal-state guard — must never
        # raise out, no matter what fetch does.
        run_fitness_sync_strava(ctx, job.id, {"user_id": 1})

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "failed"
        assert final.error_message  # populated by friendly_error()

    def test_success_notifies_with_strava_job_type(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        notifications = MagicMock()
        fetch = _Recorder(FitnessSyncResult(
            status="success", run_id=1, rows_fetched=2, rows_normalized=0,
        ))
        norm = _Recorder(NormalizeResult(
            source="strava", rows_normalized=2, drift_count=0,
        ))
        ctx = _make_ctx(
            jobs_repo=jobs_repo, fetch_strava=fetch, normalize_strava=norm,
            notifications=notifications,
        )

        job = jobs_repo.create("fitness_sync_strava", {"user_id": 11})
        run_fitness_sync_strava(ctx, job.id, {"user_id": 11})

        # The job_type drives _SUCCESS_TOPIC_MAP routing — assert the
        # worker passed the right type so the Pushover topic is gated
        # by the user's notif_fitness_sync_success preference.
        assert notifications.notify_job_success.call_count == 1
        call = notifications.notify_job_success.call_args
        assert call.args[0] == 11
        assert call.args[1] == "fitness_sync_strava"


# ── Garmin ──────────────────────────────────────────────────────────


class TestRunFitnessSyncGarmin:
    """Symmetric coverage for the Garmin worker."""

    def test_success_runs_fetch_then_normalize_and_marks_succeeded(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        fetch = _Recorder(FitnessSyncResult(
            status="success", run_id=1, rows_fetched=6, rows_normalized=0,
        ))
        norm = _Recorder(NormalizeResult(
            source="garmin", rows_normalized=4, drift_count=0,
        ))
        ctx = _make_ctx(
            jobs_repo=jobs_repo, fetch_garmin=fetch, normalize_garmin=norm,
        )

        job = jobs_repo.create("fitness_sync_garmin", {"user_id": 9})
        run_fitness_sync_garmin(ctx, job.id, {"user_id": 9})

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.result["fetch"]["rows_fetched"] == 6
        assert final.result["normalize"]["rows_normalized"] == 4
        assert fetch.calls == [{"user_id": 9}]
        assert norm.calls == [{"user_id": 9}]

    def test_auth_broken_short_circuits_normalize(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        fetch = _Recorder(FitnessSyncResult(
            status="auth_broken", run_id=2, rows_fetched=0, rows_normalized=0,
        ))
        norm = _Recorder(None)
        ctx = _make_ctx(
            jobs_repo=jobs_repo, fetch_garmin=fetch, normalize_garmin=norm,
        )

        job = jobs_repo.create("fitness_sync_garmin", {"user_id": 1})
        run_fitness_sync_garmin(ctx, job.id, {"user_id": 1})

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "failed"
        assert "auth" in final.error_message.lower()
        assert norm.calls == []

    def test_transient_failure_short_circuits_normalize(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        fetch = _Recorder(FitnessSyncResult(
            status="transient_failure", run_id=3,
            rows_fetched=0, rows_normalized=0,
        ))
        norm = _Recorder(None)
        ctx = _make_ctx(
            jobs_repo=jobs_repo, fetch_garmin=fetch, normalize_garmin=norm,
        )

        job = jobs_repo.create("fitness_sync_garmin", {"user_id": 1})
        run_fitness_sync_garmin(ctx, job.id, {"user_id": 1})

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "failed"
        assert "transient" in final.error_message.lower()
        assert norm.calls == []

    def test_already_running_marks_succeeded_with_skip_flag(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        fetch = _Recorder(FitnessSyncResult(
            status="running", run_id=42, rows_fetched=0, rows_normalized=0,
        ))
        norm = _Recorder(None)
        ctx = _make_ctx(
            jobs_repo=jobs_repo, fetch_garmin=fetch, normalize_garmin=norm,
        )

        job = jobs_repo.create("fitness_sync_garmin", {"user_id": 1})
        run_fitness_sync_garmin(ctx, job.id, {"user_id": 1})

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.result["skipped"] is True
        assert norm.calls == []

    def test_drift_count_recorded_in_result(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        fetch = _Recorder(FitnessSyncResult(
            status="success", run_id=5, rows_fetched=12, rows_normalized=0,
        ))
        norm = _Recorder(NormalizeResult(
            source="garmin", rows_normalized=10, drift_count=2,
        ))
        ctx = _make_ctx(
            jobs_repo=jobs_repo, fetch_garmin=fetch, normalize_garmin=norm,
        )

        job = jobs_repo.create("fitness_sync_garmin", {"user_id": 1})
        run_fitness_sync_garmin(ctx, job.id, {"user_id": 1})

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.result["normalize"]["drift_count"] == 2

    def test_terminal_state_guard_swallows_unexpected_exception(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        fetch = _Recorder(RuntimeError("kaboom"))
        ctx = _make_ctx(jobs_repo=jobs_repo, fetch_garmin=fetch)

        job = jobs_repo.create("fitness_sync_garmin", {"user_id": 1})
        run_fitness_sync_garmin(ctx, job.id, {"user_id": 1})

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "failed"
        assert final.error_message

    def test_success_notifies_with_garmin_job_type(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        notifications = MagicMock()
        fetch = _Recorder(FitnessSyncResult(
            status="success", run_id=1, rows_fetched=2, rows_normalized=0,
        ))
        norm = _Recorder(NormalizeResult(
            source="garmin", rows_normalized=2, drift_count=0,
        ))
        ctx = _make_ctx(
            jobs_repo=jobs_repo, fetch_garmin=fetch, normalize_garmin=norm,
            notifications=notifications,
        )

        job = jobs_repo.create("fitness_sync_garmin", {"user_id": 22})
        run_fitness_sync_garmin(ctx, job.id, {"user_id": 22})

        assert notifications.notify_job_success.call_count == 1
        call = notifications.notify_job_success.call_args
        assert call.args[0] == 22
        assert call.args[1] == "fitness_sync_garmin"
