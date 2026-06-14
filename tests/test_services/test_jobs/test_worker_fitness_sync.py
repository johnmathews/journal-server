"""Direct unit tests for the fitness_sync_{strava,garmin} workers.

Builds a minimal WorkerContext from fakes and calls each worker
function directly. The fetch + normalize callables on the context
are the seam: production wires them to ``StravaFetchService.run_sync``
/ ``GarminFetchService.run_sync`` and the free ``normalize_*``
functions; tests inject canned outcomes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from journal.db.factory import ConnectionFactory
from journal.db.fitness_repository import FitnessRepository
from journal.db.jobs_repository import SQLiteJobRepository
from journal.db.migrations import run_migrations
from journal.models import FitnessAuthState
from journal.providers.garmin import GarminAuthError
from journal.providers.strava import StravaAuthError
from journal.services.fitness.fetch import (
    FitnessSyncResult,
    GarminFetchService,
    StravaFetchService,
)
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
    from collections.abc import Callable, Iterator
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
        # F1: normalize must receive the fetch's run_id so it can amend
        # rows_normalized on the same sync_runs row.
        assert norm.calls == [{"user_id": 7, "sync_run_id": 1}]

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

    def test_quiet_success_suppresses_notify_when_no_new_rows(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        notifications = MagicMock()
        fetch = _Recorder(FitnessSyncResult(
            status="success", run_id=1, rows_fetched=0, rows_normalized=0,
        ))
        norm = _Recorder(NormalizeResult(
            source="strava", rows_normalized=0, drift_count=0,
        ))
        ctx = _make_ctx(
            jobs_repo=jobs_repo, fetch_strava=fetch, normalize_strava=norm,
            notifications=notifications,
        )

        job = jobs_repo.create("fitness_sync_strava", {"user_id": 1, "quiet_success": True})
        run_fitness_sync_strava(ctx, job.id, {"user_id": 1, "quiet_success": True})

        assert notifications.notify_job_success.call_count == 0
        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"

    def test_quiet_success_still_notifies_when_new_rows(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        notifications = MagicMock()
        fetch = _Recorder(FitnessSyncResult(
            status="success", run_id=1, rows_fetched=3, rows_normalized=0,
        ))
        norm = _Recorder(NormalizeResult(
            source="strava", rows_normalized=3, drift_count=0,
        ))
        ctx = _make_ctx(
            jobs_repo=jobs_repo, fetch_strava=fetch, normalize_strava=norm,
            notifications=notifications,
        )

        job = jobs_repo.create("fitness_sync_strava", {"user_id": 1, "quiet_success": True})
        run_fitness_sync_strava(ctx, job.id, {"user_id": 1, "quiet_success": True})

        assert notifications.notify_job_success.call_count == 1

    def test_manual_success_always_notifies_even_with_no_new_rows(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        notifications = MagicMock()
        fetch = _Recorder(FitnessSyncResult(
            status="success", run_id=1, rows_fetched=0, rows_normalized=0,
        ))
        norm = _Recorder(NormalizeResult(
            source="strava", rows_normalized=0, drift_count=0,
        ))
        ctx = _make_ctx(
            jobs_repo=jobs_repo, fetch_strava=fetch, normalize_strava=norm,
            notifications=notifications,
        )

        job = jobs_repo.create("fitness_sync_strava", {"user_id": 1})
        run_fitness_sync_strava(ctx, job.id, {"user_id": 1})

        assert notifications.notify_job_success.call_count == 1


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
        # F1: normalize must receive the fetch's run_id so it can amend
        # rows_normalized on the same sync_runs row.
        assert norm.calls == [{"user_id": 9, "sync_run_id": 1}]

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

    def test_quiet_success_suppresses_notify_when_no_new_rows(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        notifications = MagicMock()
        fetch = _Recorder(FitnessSyncResult(
            status="success", run_id=1, rows_fetched=0, rows_normalized=0,
        ))
        norm = _Recorder(NormalizeResult(
            source="garmin", rows_normalized=0, drift_count=0,
        ))
        ctx = _make_ctx(
            jobs_repo=jobs_repo, fetch_garmin=fetch, normalize_garmin=norm,
            notifications=notifications,
        )

        job = jobs_repo.create("fitness_sync_garmin", {"user_id": 1, "quiet_success": True})
        run_fitness_sync_garmin(ctx, job.id, {"user_id": 1, "quiet_success": True})

        assert notifications.notify_job_success.call_count == 0
        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"

    def test_quiet_success_still_notifies_when_new_rows(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        notifications = MagicMock()
        fetch = _Recorder(FitnessSyncResult(
            status="success", run_id=1, rows_fetched=3, rows_normalized=0,
        ))
        norm = _Recorder(NormalizeResult(
            source="garmin", rows_normalized=3, drift_count=0,
        ))
        ctx = _make_ctx(
            jobs_repo=jobs_repo, fetch_garmin=fetch, normalize_garmin=norm,
            notifications=notifications,
        )

        job = jobs_repo.create("fitness_sync_garmin", {"user_id": 1, "quiet_success": True})
        run_fitness_sync_garmin(ctx, job.id, {"user_id": 1, "quiet_success": True})

        assert notifications.notify_job_success.call_count == 1

    def test_manual_success_always_notifies_even_with_no_new_rows(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        notifications = MagicMock()
        fetch = _Recorder(FitnessSyncResult(
            status="success", run_id=1, rows_fetched=0, rows_normalized=0,
        ))
        norm = _Recorder(NormalizeResult(
            source="garmin", rows_normalized=0, drift_count=0,
        ))
        ctx = _make_ctx(
            jobs_repo=jobs_repo, fetch_garmin=fetch, normalize_garmin=norm,
            notifications=notifications,
        )

        job = jobs_repo.create("fitness_sync_garmin", {"user_id": 1})
        run_fitness_sync_garmin(ctx, job.id, {"user_id": 1})

        assert notifications.notify_job_success.call_count == 1


# ── W11: auth_status='broken' is written on a provider 401 ──────────
#
# The W11 banner copy (FitnessAuthBanner → Reconnect) is only useful if
# sync workers actually flip ``fitness_auth_state.auth_status`` to
# ``'broken'`` on 401s. The flip lives inside FitnessFetchService and is
# already covered at the fetch-service level (test_fetch.py § "Auth-
# broken fire-once"). This test guards the *worker-fetch wiring*: if a
# future refactor stops the worker from calling the fetch service, or
# the fetch service stops calling ``repo.transition_auth(status='broken')``,
# the banner silently stays green. We run the worker with the real
# fetch service backed by a fake provider that raises 401, then assert
# the persisted row.


class _FakeStravaProviderRaisingAuthError:
    """Minimal Strava provider that raises StravaAuthError on list_activities.

    Mirrors the surface used by StravaFetchService.run_sync. Distinct
    from the fakes in test_fetch.py so this test stays self-contained.
    """

    def __init__(self) -> None:
        self.refresh_called = False

    def refresh_token_if_needed(self) -> None:
        self.refresh_called = True

    def list_activities(
        self, *, after: datetime, before: datetime,
    ) -> Iterator[Any]:
        raise StravaAuthError("401 Unauthorized")
        yield  # pragma: no cover  (unreachable; pacifies generator typing)

    def get_activity_detail(self, source_id: str) -> Any:
        raise NotImplementedError


class _FakeGarminProviderRaisingAuthError:
    """Minimal Garmin provider that raises GarminAuthError on login.

    Garmin's auth check fires in ``login()`` (the SDK call that
    validates tokens against Garmin's user-profile endpoint), so the
    failure surfaces before any daily-metric fetch is attempted.
    """

    def login(self, *, mfa_callback: Any = None) -> None:
        raise GarminAuthError("401 Unauthorized")

    def get_daily(self, date: str) -> Any:
        raise NotImplementedError

    def list_activities(
        self, *, after: datetime, before: datetime,
    ) -> Iterator[Any]:
        raise NotImplementedError
        yield  # pragma: no cover


class _RecordingNotifier:
    """Captures FitnessNotifier callbacks so we can assert fire-once
    semantics without depending on the real notifications service."""

    def __init__(self) -> None:
        self.auth_broken_calls: list[tuple[int, str]] = []
        self.sync_failure_calls: list[tuple[int, str, int]] = []

    def notify_fitness_auth_broken(self, user_id: int, source: str) -> None:
        self.auth_broken_calls.append((user_id, source))

    def notify_fitness_sync_failure(
        self, user_id: int, source: str, attempts: int,
    ) -> None:
        self.sync_failure_calls.append((user_id, source, attempts))


def _seed_user_and_auth(
    factory: ConnectionFactory, *, source: str,
) -> FitnessRepository:
    """Seed a user + an ``auth_status='ok'`` row for the given source.

    Returns the FitnessRepository so callers can verify the post-run
    state. Strava persists the OAuth triple; Garmin persists the token
    blob via ``extra_state``.
    """
    conn = factory.get()
    conn.execute(
        """
        INSERT OR IGNORE INTO users (id, email, password_hash, display_name,
                                     email_verified, is_admin)
        VALUES (1, 'test@example.com', 'x', 'test', 1, 1)
        """,
    )
    repo = FitnessRepository(factory)
    if source == "strava":
        repo.upsert_auth_state(
            FitnessAuthState(
                user_id=1, source="strava",
                access_token="atok", refresh_token="rtok",
                token_expires_at="2030-01-01T00:00:00Z",
                extra_state={},
                last_successful_login_at=None,
                last_refresh_at=None,
                auth_status="ok",
                auth_broken_since=None,
            ),
        )
    else:
        repo.upsert_auth_state(
            FitnessAuthState(
                user_id=1, source="garmin",
                access_token=None, refresh_token=None,
                token_expires_at=None,
                extra_state={"tokens_blob": "blob"},
                last_successful_login_at=None,
                last_refresh_at=None,
                auth_status="ok",
                auth_broken_since=None,
            ),
        )
    return repo


class TestWorkerFlipsAuthStatusOn401:
    """End-to-end across worker → fetch service → repo: a provider 401
    must end up persisted as ``auth_status='broken'`` and surface as
    job ``status='failed'``."""

    def test_strava_worker_flips_auth_status_to_broken_on_provider_401(
        self,
        jobs_repo: SQLiteJobRepository,
        factory: ConnectionFactory,
        config: Any,
    ) -> None:
        repo = _seed_user_and_auth(factory, source="strava")
        fake_provider = _FakeStravaProviderRaisingAuthError()
        notifier = _RecordingNotifier()
        svc = StravaFetchService(
            repo=repo, notifier=notifier, config=config,
            provider_factory=lambda _auth: fake_provider,
            clock=lambda: datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC),
        )
        ctx = _make_ctx(jobs_repo=jobs_repo, fetch_strava=svc.run_sync)

        job = jobs_repo.create("fitness_sync_strava", {"user_id": 1})
        run_fitness_sync_strava(ctx, job.id, {"user_id": 1})

        # 1. The row is broken — the banner will now light up.
        auth = repo.get_auth_state(user_id=1, source="strava")
        assert auth is not None
        assert auth.auth_status == "broken"
        assert auth.auth_broken_since is not None

        # 2. Notification fired exactly once (fire-once contract from
        #    fetch.py:181 — repo.transition_auth returns True only on
        #    actual state change).
        assert notifier.auth_broken_calls == [(1, "strava")]

        # 3. The worker reports the failure via the jobs row.
        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "failed"
        assert "authorization is broken" in final.error_message.lower()

    def test_garmin_worker_flips_auth_status_to_broken_on_provider_401(
        self,
        jobs_repo: SQLiteJobRepository,
        factory: ConnectionFactory,
        config: Any,
    ) -> None:
        repo = _seed_user_and_auth(factory, source="garmin")
        fake_provider = _FakeGarminProviderRaisingAuthError()
        notifier = _RecordingNotifier()
        svc = GarminFetchService(
            repo=repo, notifier=notifier, config=config,
            provider_factory=lambda _auth: fake_provider,
            clock=lambda: datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC),
        )
        ctx = _make_ctx(jobs_repo=jobs_repo, fetch_garmin=svc.run_sync)

        job = jobs_repo.create("fitness_sync_garmin", {"user_id": 1})
        run_fitness_sync_garmin(ctx, job.id, {"user_id": 1})

        auth = repo.get_auth_state(user_id=1, source="garmin")
        assert auth is not None
        assert auth.auth_status == "broken"
        assert auth.auth_broken_since is not None
        assert notifier.auth_broken_calls == [(1, "garmin")]

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "failed"
        assert "authorization is broken" in final.error_message.lower()
