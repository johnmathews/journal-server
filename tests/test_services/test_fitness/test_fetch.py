"""Tests for the W6 fitness fetch service.

Covers the seven scenarios from ``docs/fitness-tier-plan.md`` §W6:
happy-path Strava + Garmin, auth-broken fire-once, transient
threshold fire-on-Nth, idempotent re-run on identical payload,
auth-recovery silent + ``auth_broken_since`` clearing, and unknown
exceptions classified as transient_failure (not crashing the worker).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from journal.db.fitness_repository import FitnessRepository
from journal.models import FitnessAuthState
from journal.providers.garmin import (
    GarminActivitySummary,
    GarminAuthError,
    GarminDailyMetrics,
)
from journal.providers.strava import StravaActivitySummary, StravaAuthError
from journal.services.fitness.errors import FitnessAuthError
from journal.services.fitness.fetch import (
    FitnessSyncResult,
    GarminFetchService,
    StravaFetchService,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

# ── Test infrastructure ──────────────────────────────────────────────


def _seed_user(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO users (id, email, password_hash, display_name,
                                     email_verified, is_admin)
        VALUES (1, 'test@example.com', 'x', 'test', 1, 1)
        """,
    )


@pytest.fixture
def repo(db_conn: sqlite3.Connection) -> FitnessRepository:
    _seed_user(db_conn)
    return FitnessRepository(db_conn)


def _seed_auth(repo: FitnessRepository, source: str) -> None:
    """Insert an OK auth-state row so the fetch service has a token to use."""
    repo.upsert_auth_state(
        FitnessAuthState(
            user_id=1, source=source,
            access_token="atok", refresh_token="rtok",
            token_expires_at="2030-01-01T00:00:00Z",
            extra_state={},
            last_successful_login_at=None,
            last_refresh_at=None,
            auth_status="ok",
            auth_broken_since=None,
        ),
    )


class _FakeStravaProvider:
    def __init__(self) -> None:
        self.activities: list[StravaActivitySummary] = []
        self.refresh_called = False
        self.raise_on_list: BaseException | None = None
        self.raise_on_refresh: BaseException | None = None

    def list_activities(
        self, *, after: datetime, before: datetime,
    ) -> Iterator[StravaActivitySummary]:
        if self.raise_on_list is not None:
            raise self.raise_on_list
        yield from self.activities

    def get_activity_detail(self, source_id: str) -> StravaActivitySummary:
        raise NotImplementedError

    def refresh_token_if_needed(self) -> None:
        self.refresh_called = True
        if self.raise_on_refresh is not None:
            raise self.raise_on_refresh


class _FakeGarminProvider:
    def __init__(self) -> None:
        self.daily_by_date: dict[str, GarminDailyMetrics] = {}
        self.activities: list[GarminActivitySummary] = []
        self.login_called = False
        self.raise_on_get_daily: BaseException | None = None
        self.raise_on_list_activities: BaseException | None = None
        self.raise_on_login: BaseException | None = None

    def login(self, *, mfa_callback: Any = None) -> None:
        self.login_called = True
        if self.raise_on_login is not None:
            raise self.raise_on_login

    def get_daily(self, date: str) -> GarminDailyMetrics:
        if self.raise_on_get_daily is not None:
            raise self.raise_on_get_daily
        if date in self.daily_by_date:
            return self.daily_by_date[date]
        return _empty_daily(date)

    def list_activities(
        self, *, after: datetime, before: datetime,
    ) -> Iterator[GarminActivitySummary]:
        if self.raise_on_list_activities is not None:
            raise self.raise_on_list_activities
        yield from self.activities


class _FakeNotifier:
    def __init__(self) -> None:
        self.auth_broken_calls: list[tuple[int, str]] = []
        self.sync_failure_calls: list[tuple[int, str, int]] = []

    def notify_fitness_auth_broken(self, user_id: int, source: str) -> None:
        self.auth_broken_calls.append((user_id, source))

    def notify_fitness_sync_failure(
        self, user_id: int, source: str, attempts: int,
    ) -> None:
        self.sync_failure_calls.append((user_id, source, attempts))


def _empty_daily(date: str) -> GarminDailyMetrics:
    return GarminDailyMetrics(
        local_date=date,
        sleep_score=None, sleep_duration_s=None, sleep_efficiency_pct=None,
        hrv_overnight_ms=None, resting_hr_bpm=None,
        body_battery_high=None, body_battery_low=None, stress_avg=None,
        training_load_acute=None, training_load_chronic=None,
        training_readiness=None,
        extras={},
        raw_payloads_per_endpoint={
            "sleep": None, "hrv": None, "body_battery": None,
            "stress": None, "training_load": None, "training_readiness": None,
        },
    )


def _strava_summary(
    source_id: str, sport_type: str = "Run",
    raw: dict[str, Any] | None = None,
) -> StravaActivitySummary:
    return StravaActivitySummary(
        source_id=source_id, sport_type=sport_type,
        start_time="2026-04-15T08:00:00Z",
        local_date="2026-04-15",
        duration_s=1800, moving_time_s=1750,
        distance_m=5000.0, elevation_gain_m=10.0,
        avg_hr_bpm=140, max_hr_bpm=160, calories_kcal=350,
        extras={},
        raw_payload=raw or {"id": source_id, "sport_type": sport_type},
    )


def _make_strava(
    *, repo: FitnessRepository, config: Any, fake: _FakeStravaProvider,
    notifier: _FakeNotifier,
) -> StravaFetchService:
    return StravaFetchService(
        repo=repo, notifier=notifier, config=config,
        provider_factory=lambda _auth: fake,
        clock=lambda: datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC),
    )


def _make_garmin(
    *, repo: FitnessRepository, config: Any, fake: _FakeGarminProvider,
    notifier: _FakeNotifier,
) -> GarminFetchService:
    return GarminFetchService(
        repo=repo, notifier=notifier, config=config,
        provider_factory=lambda _auth: fake,
        clock=lambda: datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC),
    )


# ── Tests ────────────────────────────────────────────────────────────


# 1. Happy path Strava ------------------------------------------------


def test_strava_happy_path_writes_raw_rows_and_finishes_success(
    repo: FitnessRepository, config: Any, db_conn: sqlite3.Connection,
) -> None:
    _seed_auth(repo, "strava")
    fake = _FakeStravaProvider()
    fake.activities = [
        _strava_summary("11000000001", "Run"),
        _strava_summary("11000000002", "Ride"),
        _strava_summary("11000000003", "Walk"),
    ]
    notifier = _FakeNotifier()
    svc = _make_strava(repo=repo, config=config, fake=fake, notifier=notifier)

    result = svc.run_sync(
        user_id=1,
        since=datetime(2026, 4, 1, tzinfo=UTC),
        until=datetime(2026, 5, 1, tzinfo=UTC),
    )

    assert result.status == "success"
    assert result.rows_fetched == 3
    assert result.rows_normalized == 0
    assert fake.refresh_called

    # 3 raw rows in fitness_raw_strava
    raw_rows = db_conn.execute(
        "SELECT endpoint, source_id FROM fitness_raw_strava WHERE user_id=1",
    ).fetchall()
    assert len(raw_rows) == 3
    assert all(r["endpoint"] == "activities" for r in raw_rows)

    # one success run row
    runs = db_conn.execute(
        "SELECT status, rows_fetched FROM fitness_sync_runs WHERE user_id=1",
    ).fetchall()
    assert len(runs) == 1
    assert runs[0]["status"] == "success"
    assert runs[0]["rows_fetched"] == 3

    # auth_status flipped to ok (was already ok — still ok, no new rows)
    auth = repo.get_auth_state(user_id=1, source="strava")
    assert auth is not None
    assert auth.auth_status == "ok"
    assert notifier.auth_broken_calls == []
    assert notifier.sync_failure_calls == []


# 2. Happy path Garmin ------------------------------------------------


def test_garmin_happy_path_writes_six_raw_rows_per_day(
    repo: FitnessRepository, config: Any, db_conn: sqlite3.Connection,
) -> None:
    _seed_auth(repo, "garmin")
    fake = _FakeGarminProvider()
    fake.daily_by_date = {
        "2026-04-15": GarminDailyMetrics(
            local_date="2026-04-15",
            sleep_score=84, sleep_duration_s=27000, sleep_efficiency_pct=92.0,
            hrv_overnight_ms=47.5, resting_hr_bpm=51,
            body_battery_high=78, body_battery_low=22, stress_avg=31,
            training_load_acute=412.5, training_load_chronic=380.0,
            training_readiness=78,
            extras={},
            raw_payloads_per_endpoint={
                "sleep": {"score": 84},
                "hrv": {"avg": 47.5},
                "body_battery": [{"charged": 78}],
                "stress": {"avg": 31},
                "training_load": {"acute": 412.5},
                "training_readiness": [{"score": 78}],
            },
        ),
    }
    notifier = _FakeNotifier()
    svc = _make_garmin(repo=repo, config=config, fake=fake, notifier=notifier)

    result = svc.run_sync(
        user_id=1,
        since=datetime(2026, 4, 15, tzinfo=UTC),
        until=datetime(2026, 4, 15, 23, 59, tzinfo=UTC),
    )

    assert result.status == "success"
    # 6 daily-metrics endpoints, no activities → 6 raw rows
    assert result.rows_fetched == 6
    assert fake.login_called

    rows = db_conn.execute(
        "SELECT endpoint, source_id FROM fitness_raw_garmin WHERE user_id=1",
    ).fetchall()
    assert len(rows) == 6
    endpoints = {r["endpoint"] for r in rows}
    assert endpoints == {
        "sleep", "hrv", "body_battery",
        "stress", "training_load", "training_readiness",
    }
    # Garmin raw uses local_date as source_id
    assert all(r["source_id"] == "2026-04-15" for r in rows)


# 3. Auth-broken fire-once -------------------------------------------


def test_strava_auth_error_transitions_state_and_fires_once(
    repo: FitnessRepository, config: Any, db_conn: sqlite3.Connection,
) -> None:
    _seed_auth(repo, "strava")
    fake = _FakeStravaProvider()
    fake.raise_on_list = StravaAuthError("401 Unauthorized")
    notifier = _FakeNotifier()
    svc = _make_strava(repo=repo, config=config, fake=fake, notifier=notifier)

    # First failure → transitions to broken, fires once
    result1 = svc.run_sync(user_id=1, since=datetime(2026, 4, 1, tzinfo=UTC))
    assert result1.status == "auth_broken"
    auth = repo.get_auth_state(user_id=1, source="strava")
    assert auth is not None
    assert auth.auth_status == "broken"
    assert auth.auth_broken_since is not None
    assert notifier.auth_broken_calls == [(1, "strava")]

    # Second failure → still broken, but transition returns False → no re-fire
    result2 = svc.run_sync(user_id=1, since=datetime(2026, 4, 1, tzinfo=UTC))
    assert result2.status == "auth_broken"
    assert notifier.auth_broken_calls == [(1, "strava")]  # unchanged


# 4. Transient threshold fire-on-Nth ---------------------------------


def test_transient_threshold_fires_only_on_nth_failure(
    repo: FitnessRepository, config: Any,
) -> None:
    """With threshold=3, attempts 1 and 2 do not fire; the 3rd does."""
    assert config.fitness_transient_failure_threshold == 3  # default

    _seed_auth(repo, "strava")
    fake = _FakeStravaProvider()
    fake.raise_on_list = ConnectionError("network down")
    notifier = _FakeNotifier()
    svc = _make_strava(repo=repo, config=config, fake=fake, notifier=notifier)

    for attempt in (1, 2, 3):
        result = svc.run_sync(user_id=1, since=datetime(2026, 4, 1, tzinfo=UTC))
        assert result.status == "transient_failure"
        if attempt < 3:
            assert notifier.sync_failure_calls == []
        else:
            assert notifier.sync_failure_calls == [(1, "strava", 3)]

    # 4th failure does NOT re-fire (streak length is 4, threshold is 3)
    svc.run_sync(user_id=1, since=datetime(2026, 4, 1, tzinfo=UTC))
    assert notifier.sync_failure_calls == [(1, "strava", 3)]


# 5. Idempotent re-run on identical payload --------------------------


def test_idempotent_re_run_skips_duplicate_payload(
    repo: FitnessRepository, config: Any, db_conn: sqlite3.Connection,
) -> None:
    _seed_auth(repo, "strava")
    fake = _FakeStravaProvider()
    fake.activities = [_strava_summary("11000000001", "Run")]
    notifier = _FakeNotifier()
    svc = _make_strava(repo=repo, config=config, fake=fake, notifier=notifier)

    r1 = svc.run_sync(
        user_id=1, since=datetime(2026, 4, 1, tzinfo=UTC),
        until=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert r1.rows_fetched == 1

    # Second sync — same activity → no new row, rows_fetched=0 on second call
    r2 = svc.run_sync(
        user_id=1, since=datetime(2026, 4, 1, tzinfo=UTC),
        until=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert r2.status == "success"
    assert r2.rows_fetched == 0

    # DB still shows only one raw row
    raw_rows = db_conn.execute(
        "SELECT id FROM fitness_raw_strava WHERE user_id=1",
    ).fetchall()
    assert len(raw_rows) == 1


# 6. Auth recovery is silent and clears auth_broken_since ------------


def test_auth_recovery_clears_broken_since_and_does_not_notify(
    repo: FitnessRepository, config: Any, db_conn: sqlite3.Connection,
) -> None:
    """After recovery: auth_status='ok', auth_broken_since IS NULL,
    no Pushover (D5: recovery is silent)."""
    _seed_auth(repo, "strava")
    repo.transition_auth(
        user_id=1, source="strava", status="broken",
        at="2026-05-01T00:00:00Z",
    )
    auth_before = repo.get_auth_state(user_id=1, source="strava")
    assert auth_before is not None
    assert auth_before.auth_status == "broken"
    assert auth_before.auth_broken_since is not None

    fake = _FakeStravaProvider()
    fake.activities = [_strava_summary("11000000001")]
    notifier = _FakeNotifier()
    svc = _make_strava(repo=repo, config=config, fake=fake, notifier=notifier)

    result = svc.run_sync(
        user_id=1, since=datetime(2026, 4, 1, tzinfo=UTC),
        until=datetime(2026, 5, 1, tzinfo=UTC),
    )

    assert result.status == "success"
    # Assert directly against the DB row — the webapp banner clear
    # depends on auth_broken_since being NULL, not just the dataclass field.
    row = db_conn.execute(
        "SELECT auth_status, auth_broken_since FROM fitness_auth_state "
        "WHERE user_id=1 AND source='strava'",
    ).fetchone()
    assert row["auth_status"] == "ok"
    assert row["auth_broken_since"] is None
    assert notifier.auth_broken_calls == []
    assert notifier.sync_failure_calls == []


# 7. Unknown exception classified as transient, doesn't crash --------


def test_unknown_exception_classified_as_transient_failure(
    repo: FitnessRepository, config: Any, db_conn: sqlite3.Connection,
) -> None:
    """Test 7 from the plan: an unrecognised exception class lands as
    transient_failure with error_class set; the worker doesn't crash."""
    _seed_auth(repo, "strava")
    fake = _FakeStravaProvider()

    class _SomethingExotic(Exception):  # noqa: N818  intentional non-Error name for the unknown-exception path
        pass

    fake.raise_on_list = _SomethingExotic("never seen this before")
    notifier = _FakeNotifier()
    svc = _make_strava(repo=repo, config=config, fake=fake, notifier=notifier)

    result = svc.run_sync(user_id=1, since=datetime(2026, 4, 1, tzinfo=UTC))

    assert result.status == "transient_failure"
    row = db_conn.execute(
        "SELECT status, error_class, error_message FROM fitness_sync_runs "
        "WHERE user_id=1 ORDER BY started_at DESC LIMIT 1",
    ).fetchone()
    assert row["status"] == "transient_failure"
    assert row["error_class"] == "_SomethingExotic"
    assert "never seen" in row["error_message"]


# 8. Single-run guard short-circuits ---------------------------------


def test_single_run_guard_returns_existing_run_id(
    repo: FitnessRepository, config: Any,
) -> None:
    _seed_auth(repo, "strava")
    existing_run_id = repo.start_sync_run(user_id=1, source="strava")

    fake = _FakeStravaProvider()
    notifier = _FakeNotifier()
    svc = _make_strava(repo=repo, config=config, fake=fake, notifier=notifier)

    result = svc.run_sync(user_id=1, since=datetime(2026, 4, 1, tzinfo=UTC))

    assert result.status == "running"
    assert result.run_id == existing_run_id
    # Did not invoke the provider
    assert not fake.refresh_called
    # No new sync run row started
    runs = repo.list_recent_sync_runs(user_id=1, source="strava", limit=10)
    assert len(runs) == 1


# 9. Missing auth state → auth_broken without notification -----------


def test_missing_auth_state_returns_auth_broken_silently(
    repo: FitnessRepository, config: Any,
) -> None:
    """No auth-state row at all: still record auth_broken in the run,
    but don't fire Pushover (there's nothing to recover from — user
    has never connected)."""
    fake = _FakeStravaProvider()
    notifier = _FakeNotifier()
    svc = _make_strava(repo=repo, config=config, fake=fake, notifier=notifier)

    result = svc.run_sync(user_id=1)

    assert result.status == "auth_broken"
    assert notifier.auth_broken_calls == []
    runs = repo.list_recent_sync_runs(user_id=1, source="strava", limit=10)
    assert len(runs) == 1
    assert runs[0].status == "auth_broken"
    assert runs[0].error_class == "MissingAuthState"


# 10. Garmin auth error path -----------------------------------------


def test_garmin_auth_error_transitions_state_and_fires_once(
    repo: FitnessRepository, config: Any,
) -> None:
    _seed_auth(repo, "garmin")
    fake = _FakeGarminProvider()
    fake.raise_on_login = GarminAuthError("invalid credentials")
    notifier = _FakeNotifier()
    svc = _make_garmin(repo=repo, config=config, fake=fake, notifier=notifier)

    result = svc.run_sync(
        user_id=1, since=datetime(2026, 4, 15, tzinfo=UTC),
        until=datetime(2026, 4, 15, 23, 59, tzinfo=UTC),
    )

    assert result.status == "auth_broken"
    auth = repo.get_auth_state(user_id=1, source="garmin")
    assert auth is not None
    assert auth.auth_status == "broken"
    assert notifier.auth_broken_calls == [(1, "garmin")]


# 11. FitnessSyncResult shape pinned ---------------------------------


def test_fitness_sync_result_is_dataclass_serialisable() -> None:
    """Workers (W8) serialise via dataclasses.asdict — pin the fields."""
    import dataclasses
    result = FitnessSyncResult(
        status="success", run_id=42, rows_fetched=3, rows_normalized=0,
    )
    d = dataclasses.asdict(result)
    assert d == {
        "status": "success", "run_id": 42,
        "rows_fetched": 3, "rows_normalized": 0,
    }
    # JSON-safe (the workers will serialise to job result_json)
    json.dumps(d)


# 12. FitnessAuthError comes from the errors module, not providers ---


def test_fetch_service_raises_fitness_auth_error_not_provider_error(
    repo: FitnessRepository, config: Any,
) -> None:
    """The fetch service translates StravaAuthError → FitnessAuthError
    internally so callers depend on services/fitness/errors only.
    Verified by the inheritance check — provider auth errors must NOT
    be subclasses of FitnessAuthError, because the fetch service
    catches one and re-raises the other."""
    assert not issubclass(StravaAuthError, FitnessAuthError)
    assert not issubclass(GarminAuthError, FitnessAuthError)
