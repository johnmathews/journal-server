"""Direct unit tests for the entity_reembed worker.

Demonstrates the WorkerContext seam: tests build a minimal context
from fakes/mocks and call ``run_entity_reembed`` directly without
constructing the full ``JobRunner``. This is what the original
refactor plan called the worker's "independently testable" property.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from journal.db.connection import get_connection
from journal.db.jobs_repository import SQLiteJobRepository
from journal.db.migrations import run_migrations
from journal.services.jobs.notifier import JobNotifier
from journal.services.jobs.workers import WorkerContext
from journal.services.jobs.workers.entity_reembed import run_entity_reembed

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Generator
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
    reembedder: object,
    notifications: object | None = None,
) -> WorkerContext:
    """Build a WorkerContext that exposes only the fields
    ``run_entity_reembed`` actually reads.
    """
    notifier = JobNotifier(jobs=jobs_repo, notifications=notifications)
    return WorkerContext(
        jobs=jobs_repo,
        notifier=notifier,
        extraction=MagicMock(name="EntityExtractionService"),
        reembedder=reembedder,  # type: ignore[arg-type]
        mood_backfill=MagicMock(name="mood_backfill_callable"),
        mood_scoring=MagicMock(name="MoodScoringService"),
        entries=MagicMock(name="EntryRepository"),
        ingestion=None,
        pop_pending_images=lambda _jid: [],
        pop_pending_audio=lambda _jid: [],
        queue_post_ingestion_jobs=lambda *_args: {},
    )


class _SuccessReembedder:
    """Records calls and returns the canned summary."""

    def __init__(self, summary: dict[str, object]) -> None:
        self._summary = summary
        self.calls: list[dict[str, object]] = []

    def reembed_entity_for_description(
        self, entity_id: int, *, user_id: int,
    ) -> dict[str, object]:
        self.calls.append({"entity_id": entity_id, "user_id": user_id})
        return self._summary


class _FailingReembedder:
    """Raises so the worker exercises its terminal-state guard."""

    def reembed_entity_for_description(
        self, entity_id: int, *, user_id: int,
    ) -> dict[str, object]:
        raise RuntimeError("reembed exploded")


class TestRunEntityReembed:
    """Worker is callable without a JobRunner — minimal-context tests."""

    def test_succeeds_and_records_summary(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        reembedder = _SuccessReembedder(
            {"entity_id": 7, "embedded": True, "dimensions": 1024},
        )
        ctx = _make_ctx(jobs_repo=jobs_repo, reembedder=reembedder)

        job = jobs_repo.create("entity_reembed", {"entity_id": 7, "user_id": 1})
        run_entity_reembed(ctx, job.id, {"entity_id": 7, "user_id": 1})

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.result == {"entity_id": 7, "embedded": True, "dimensions": 1024}
        assert final.progress_current == 1
        assert final.progress_total == 1
        assert reembedder.calls == [{"entity_id": 7, "user_id": 1}]

    def test_failure_marks_failed_and_does_not_raise(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        ctx = _make_ctx(jobs_repo=jobs_repo, reembedder=_FailingReembedder())

        job = jobs_repo.create("entity_reembed", {"entity_id": 9, "user_id": 1})
        # The worker is the executor's terminal-state guard — it must
        # never raise out, no matter what the reembedder does.
        run_entity_reembed(ctx, job.id, {"entity_id": 9, "user_id": 1})

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "failed"
        assert final.error_message  # populated with friendly_error()

    def test_notifies_on_success_when_notifications_configured(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        notifications = MagicMock()
        reembedder = _SuccessReembedder({"entity_id": 1, "embedded": True})
        ctx = _make_ctx(
            jobs_repo=jobs_repo,
            reembedder=reembedder,
            notifications=notifications,
        )

        job = jobs_repo.create("entity_reembed", {"entity_id": 1, "user_id": 42})
        run_entity_reembed(ctx, job.id, {"entity_id": 1, "user_id": 42})

        notifications.notify_job_success.assert_called_once_with(
            42, "entity_reembed", {"entity_id": 1, "embedded": True},
        )

    def test_no_notifications_service_means_silent_success(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        # When constructed with notifications=None the JobNotifier is a
        # no-op; the worker still completes normally.
        reembedder = _SuccessReembedder({"entity_id": 5})
        ctx = _make_ctx(jobs_repo=jobs_repo, reembedder=reembedder)

        job = jobs_repo.create("entity_reembed", {"entity_id": 5, "user_id": 1})
        run_entity_reembed(ctx, job.id, {"entity_id": 5, "user_id": 1})

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
