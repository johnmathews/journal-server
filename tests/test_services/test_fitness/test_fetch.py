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
from journal.services.fitness.errors import FitnessAuthError, MidRunAuthLost
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
def repo(factory, db_conn: sqlite3.Connection) -> FitnessRepository:
    _seed_user(db_conn)
    return FitnessRepository(factory)


def _seed_auth(repo: FitnessRepository, source: str) -> None:
    """Insert an OK auth-state row so the fetch service has a credential to use.

    The shape mirrors what each source's W11 re-auth flow actually
    persists: Strava gets the OAuth triple; Garmin gets the token blob
    in ``extra_state`` and leaves the OAuth columns ``None`` (matching
    ``cmd_fitness_reauth_garmin``). Tests that want to exercise the
    "no credentials" path should NOT call this helper.
    """
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
        return
    repo.upsert_auth_state(
        FitnessAuthState(
            user_id=1, source="garmin",
            access_token=None,
            refresh_token=None,
            token_expires_at=None,
            extra_state={"tokens_blob": "blob-from-W11-reauth"},
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
        # W6 unattended re-login seam (SupportsUnattendedRelogin surface).
        self.relogin_password = ""  # empty → can_relogin False (default)
        self.relogin_calls = 0
        self.raise_on_relogin: BaseException | None = None
        self.on_relogin_success: Any = None

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

    # W6 — SupportsUnattendedRelogin surface ---------------------------
    def can_relogin_with_password(self) -> bool:
        return bool(self.relogin_password)

    def relogin_with_password(self) -> None:
        self.relogin_calls += 1
        if self.raise_on_relogin is not None:
            raise self.raise_on_relogin
        if self.on_relogin_success is not None:
            self.on_relogin_success()


class _FakeNotifier:
    def __init__(self) -> None:
        self.auth_broken_calls: list[tuple[int, str]] = []
        self.auth_broken_recovery_flags: list[bool] = []
        self.sync_failure_calls: list[tuple[int, str, int]] = []

    def notify_fitness_auth_broken(
        self, user_id: int, source: str, *, recovery_attempted: bool = False,
    ) -> None:
        self.auth_broken_calls.append((user_id, source))
        self.auth_broken_recovery_flags.append(recovery_attempted)

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
    notifier: _FakeNotifier, upstream_cooldown: Any = None,
) -> GarminFetchService:
    return GarminFetchService(
        repo=repo, notifier=notifier, config=config,
        provider_factory=lambda _auth: fake,
        upstream_cooldown=upstream_cooldown,
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
        "SELECT status, rows_fetched, workouts_fetched, wellness_fetched"
        " FROM fitness_sync_runs WHERE user_id=1",
    ).fetchall()
    assert len(runs) == 1
    assert runs[0]["status"] == "success"
    assert runs[0]["rows_fetched"] == 3
    # T7: Strava is workouts-only.
    assert runs[0]["workouts_fetched"] == 3
    assert runs[0]["wellness_fetched"] == 0

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

    # T7: this fixture has zero activities, so the sync_runs row's split
    # counters report 6 wellness / 0 workouts.
    sync_runs = repo.list_recent_sync_runs(user_id=1, source="garmin")
    assert sync_runs[0].workouts_fetched == 0
    assert sync_runs[0].wellness_fetched == 6


# 2b. T7 — Garmin mixed (workouts + wellness) ------------------------


def test_garmin_mixed_workouts_and_wellness_recorded_separately(
    repo: FitnessRepository, config: Any,
) -> None:
    """T7: a Garmin sync that pulls both wellness rows AND activities
    records the per-bucket counts on the sync_runs row."""
    _seed_auth(repo, "garmin")
    fake = _FakeGarminProvider()
    fake.daily_by_date = {
        "2026-04-15": GarminDailyMetrics(
            local_date="2026-04-15",
            sleep_score=80, sleep_duration_s=27000, sleep_efficiency_pct=90.0,
            hrv_overnight_ms=50.0, resting_hr_bpm=55,
            body_battery_high=70, body_battery_low=20, stress_avg=30,
            training_load_acute=400.0, training_load_chronic=380.0,
            training_readiness=70,
            extras={},
            raw_payloads_per_endpoint={
                "sleep": {"score": 80},
                "hrv": {"avg": 50},
                "body_battery": [{"charged": 70}],
                "stress": {"avg": 30},
                "training_load": {"acute": 400},
                "training_readiness": [{"score": 70}],
            },
        ),
    }
    fake.activities = [
        GarminActivitySummary(
            source_id="999000001", activity_type_str="running",
            start_time="2026-04-15T08:00:00Z", local_date="2026-04-15",
            duration_s=2400, moving_time_s=2380,
            distance_m=6000.0, elevation_gain_m=20.0,
            avg_hr_bpm=150, max_hr_bpm=170, calories_kcal=420,
            extras={},
            raw_payload={"activityId": 999000001},
        ),
        GarminActivitySummary(
            source_id="999000002", activity_type_str="cycling",
            start_time="2026-04-15T18:00:00Z", local_date="2026-04-15",
            duration_s=3600, moving_time_s=3590,
            distance_m=20000.0, elevation_gain_m=120.0,
            avg_hr_bpm=130, max_hr_bpm=160, calories_kcal=600,
            extras={},
            raw_payload={"activityId": 999000002},
        ),
    ]
    notifier = _FakeNotifier()
    svc = _make_garmin(repo=repo, config=config, fake=fake, notifier=notifier)

    result = svc.run_sync(
        user_id=1,
        since=datetime(2026, 4, 15, tzinfo=UTC),
        until=datetime(2026, 4, 15, 23, 59, tzinfo=UTC),
    )

    assert result.status == "success"
    # Legacy total stays in sync: 6 wellness endpoints + 2 activities = 8.
    assert result.rows_fetched == 8
    sync_runs = repo.list_recent_sync_runs(user_id=1, source="garmin")
    assert sync_runs[0].workouts_fetched == 2
    assert sync_runs[0].wellness_fetched == 6


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


# 10b. Garmin blob-only auth row reaches the provider -----------------
#
# Regression for the bug surfaced by the W13 live smoke: W11's Garmin
# re-auth persists the credential as ``extra_state["tokens_blob"]`` and
# leaves ``access_token`` as None, but the W6 missing-credentials check
# only looked at ``access_token``. Result: every Garmin sync hit
# ``MissingAuthState`` regardless of whether the blob was populated.
# The fix decouples the credential check via ``_has_credentials``,
# overridden per-source so Garmin checks the blob and Strava continues
# to check the OAuth access token.


def test_garmin_run_sync_proceeds_when_only_tokens_blob_is_set(
    repo: FitnessRepository, config: Any,
) -> None:
    """W11 Garmin auth lives in extra_state.tokens_blob, not access_token.

    The fetch service must not short-circuit to MissingAuthState in that
    case. We seed an auth row in the W11 shape (no access_token, blob
    present) and assert run_sync goes through the provider path.
    """
    repo.upsert_auth_state(
        FitnessAuthState(
            user_id=1, source="garmin",
            access_token=None,
            refresh_token=None,
            token_expires_at=None,
            extra_state={"tokens_blob": "blob-from-W11-reauth"},
            last_successful_login_at=None,
            last_refresh_at=None,
            auth_status="ok",
            auth_broken_since=None,
        ),
    )
    fake = _FakeGarminProvider()
    notifier = _FakeNotifier()
    svc = _make_garmin(repo=repo, config=config, fake=fake, notifier=notifier)

    result = svc.run_sync(
        user_id=1,
        since=datetime(2026, 4, 15, tzinfo=UTC),
        until=datetime(2026, 4, 15, 23, 59, tzinfo=UTC),
    )

    assert result.status != "auth_broken", (
        "blob-only auth must not trigger MissingAuthState; got "
        f"{result.status} (likely the W6 access_token check still bites)"
    )
    assert fake.login_called, "provider.login must run when blob is present"


def test_strava_run_sync_still_requires_access_token(
    repo: FitnessRepository, config: Any,
) -> None:
    """Strava's OAuth pattern is unchanged — access_token is the credential.

    A row with no access_token (whatever extra_state contains) must
    still short-circuit MissingAuthState for Strava. Pinning this
    behaviour prevents the fix from over-rotating the check.
    """
    repo.upsert_auth_state(
        FitnessAuthState(
            user_id=1, source="strava",
            access_token=None,
            refresh_token=None,
            token_expires_at=None,
            extra_state={"tokens_blob": "ignored-for-strava"},
            last_successful_login_at=None,
            last_refresh_at=None,
            auth_status="ok",
            auth_broken_since=None,
        ),
    )
    fake = _FakeStravaProvider()
    notifier = _FakeNotifier()
    svc = _make_strava(repo=repo, config=config, fake=fake, notifier=notifier)

    result = svc.run_sync(user_id=1)

    assert result.status == "auth_broken"
    runs = repo.list_recent_sync_runs(user_id=1, source="strava", limit=1)
    assert runs[0].error_class == "MissingAuthState"


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


# 13. W5 — mid-run auth removal / broken handled cleanly --------------


def test_strava_midrun_disconnect_marks_run_auth_broken_no_recreate(
    repo: FitnessRepository, config: Any, db_conn: sqlite3.Connection,
) -> None:
    """User disconnects mid-run (deletes fitness_auth_state). The W5
    hardening must mark the run ``auth_broken`` with error_class
    ``MidRunAuthLost`` and NOT recreate the auth row (calling the
    normal transition_auth path would silently un-do the disconnect).
    """
    _seed_auth(repo, "strava")

    fake = _FakeStravaProvider()

    def _delete_during_refresh() -> None:
        repo.delete_auth_state(user_id=1, source="strava")

    # Hook the delete into the provider's refresh step so it fires
    # between the run_sync top-level auth read and the next
    # _verify_auth_live call inside _do_fetch_and_persist.
    original_refresh = fake.refresh_token_if_needed

    def _refresh_then_delete() -> None:
        original_refresh()
        _delete_during_refresh()

    fake.refresh_token_if_needed = _refresh_then_delete  # type: ignore[method-assign]

    notifier = _FakeNotifier()
    svc = _make_strava(repo=repo, config=config, fake=fake, notifier=notifier)

    result = svc.run_sync(user_id=1, since=datetime(2026, 4, 1, tzinfo=UTC))

    assert result.status == "auth_broken"
    # No `running`-stuck rows — the run finished in a terminal state.
    runs = db_conn.execute(
        "SELECT status, error_class, error_message, finished_at "
        "FROM fitness_sync_runs WHERE user_id=1",
    ).fetchall()
    assert len(runs) == 1
    assert runs[0]["status"] == "auth_broken"
    assert runs[0]["error_class"] == "MidRunAuthLost"
    assert "removed" in runs[0]["error_message"]
    assert runs[0]["finished_at"] is not None

    # The auth row stays DELETED — transition_auth must not have run.
    assert repo.get_auth_state(user_id=1, source="strava") is None
    # No notification fired — the user explicitly disconnected.
    assert notifier.auth_broken_calls == []


def test_strava_midrun_auth_status_flips_to_broken_clean_abort(
    repo: FitnessRepository, config: Any, db_conn: sqlite3.Connection,
) -> None:
    """Auth row stays in place but ``auth_status`` flips to ``broken``
    mid-run (another path detected the failure). The run aborts cleanly
    without recursively re-broken-transitioning the row or notifying."""
    _seed_auth(repo, "strava")

    fake = _FakeStravaProvider()
    original_refresh = fake.refresh_token_if_needed

    def _refresh_then_break() -> None:
        original_refresh()
        repo.transition_auth(
            user_id=1, source="strava", status="broken",
            at="2026-05-09T12:00:00Z",
        )

    fake.refresh_token_if_needed = _refresh_then_break  # type: ignore[method-assign]

    notifier = _FakeNotifier()
    svc = _make_strava(repo=repo, config=config, fake=fake, notifier=notifier)

    result = svc.run_sync(user_id=1, since=datetime(2026, 4, 1, tzinfo=UTC))

    assert result.status == "auth_broken"
    runs = db_conn.execute(
        "SELECT status, error_class, error_message FROM fitness_sync_runs "
        "WHERE user_id=1",
    ).fetchall()
    assert runs[0]["status"] == "auth_broken"
    assert runs[0]["error_class"] == "MidRunAuthLost"
    assert "broken" in runs[0]["error_message"]
    # Row remains broken, not recreated, not re-notified.
    auth = repo.get_auth_state(user_id=1, source="strava")
    assert auth is not None
    assert auth.auth_status == "broken"
    assert notifier.auth_broken_calls == []


def test_garmin_midrun_disconnect_during_day_loop_marks_auth_broken(
    repo: FitnessRepository, config: Any, db_conn: sqlite3.Connection,
) -> None:
    """User disconnects between the first and second day of a multi-day
    Garmin window. The per-day _verify_auth_live check catches it."""
    _seed_auth(repo, "garmin")

    fake = _FakeGarminProvider()
    fake.daily_by_date = {
        "2026-04-15": _empty_daily("2026-04-15"),
        "2026-04-16": _empty_daily("2026-04-16"),
    }

    days_seen: list[str] = []
    original_get_daily = fake.get_daily

    def _get_daily_then_disconnect(date: str) -> GarminDailyMetrics:
        days_seen.append(date)
        result = original_get_daily(date)
        # After the first day, the user disconnects.
        if len(days_seen) == 1:
            repo.delete_auth_state(user_id=1, source="garmin")
        return result

    fake.get_daily = _get_daily_then_disconnect  # type: ignore[method-assign]

    notifier = _FakeNotifier()
    svc = _make_garmin(repo=repo, config=config, fake=fake, notifier=notifier)

    result = svc.run_sync(
        user_id=1,
        since=datetime(2026, 4, 15, tzinfo=UTC),
        until=datetime(2026, 4, 16, 23, 59, tzinfo=UTC),
    )

    assert result.status == "auth_broken"
    # Day-1 was attempted; the day-2 loop iteration aborted before
    # provider.get_daily ran for the second date.
    assert days_seen == ["2026-04-15"]

    runs = db_conn.execute(
        "SELECT status, error_class, error_message FROM fitness_sync_runs "
        "WHERE user_id=1",
    ).fetchall()
    assert len(runs) == 1
    assert runs[0]["status"] == "auth_broken"
    assert runs[0]["error_class"] == "MidRunAuthLost"
    assert "removed" in runs[0]["error_message"]
    assert repo.get_auth_state(user_id=1, source="garmin") is None
    assert notifier.auth_broken_calls == []


def test_garmin_midrun_auth_status_flips_to_broken_clean_abort(
    repo: FitnessRepository, config: Any, db_conn: sqlite3.Connection,
) -> None:
    """Garmin equivalent of the auth_status='broken' mid-run test."""
    _seed_auth(repo, "garmin")

    fake = _FakeGarminProvider()
    fake.daily_by_date = {"2026-04-15": _empty_daily("2026-04-15")}
    original_login = fake.login

    def _login_then_break(*, mfa_callback: Any = None) -> None:
        original_login(mfa_callback=mfa_callback)
        repo.transition_auth(
            user_id=1, source="garmin", status="broken",
            at="2026-05-09T12:00:00Z",
        )

    fake.login = _login_then_break  # type: ignore[method-assign]

    notifier = _FakeNotifier()
    svc = _make_garmin(repo=repo, config=config, fake=fake, notifier=notifier)

    result = svc.run_sync(
        user_id=1,
        since=datetime(2026, 4, 15, tzinfo=UTC),
        until=datetime(2026, 4, 15, 23, 59, tzinfo=UTC),
    )

    assert result.status == "auth_broken"
    runs = db_conn.execute(
        "SELECT status, error_class, error_message FROM fitness_sync_runs "
        "WHERE user_id=1",
    ).fetchall()
    assert runs[0]["status"] == "auth_broken"
    assert runs[0]["error_class"] == "MidRunAuthLost"
    assert notifier.auth_broken_calls == []


# 14. W6 — unattended Garmin re-login on dead token blob --------------


def _relogin_capable_fake() -> _FakeGarminProvider:
    """A fake whose dead-blob failure mode is a login-time auth error and
    which carries a usable saved password for the unattended retry."""
    fake = _FakeGarminProvider()
    fake.relogin_password = "hunter2"
    fake.raise_on_login = GarminAuthError("401 from dead blob")
    return fake


def test_garmin_dead_blob_auto_relogin_completes_sync_and_persists_blob(
    repo: FitnessRepository, config: Any, db_conn: sqlite3.Connection,
) -> None:
    """Dead blob → one unattended re-login → the retried fetch succeeds
    without user action and the fresh blob reaches the auth row."""
    from journal.services.fitness.garmin_pending import GarminUpstreamCooldown

    _seed_auth(repo, "garmin")
    fake = _relogin_capable_fake()

    def _relogin_succeeds() -> None:
        # Fresh client works from here on; persist the new blob the way
        # the real provider's persist callback does.
        fake.raise_on_login = None
        auth = repo.get_auth_state(user_id=1, source="garmin")
        assert auth is not None
        extra = dict(auth.extra_state)
        extra["tokens_blob"] = "fresh-blob-after-relogin"
        repo.upsert_auth_state(
            FitnessAuthState(
                user_id=1, source="garmin",
                access_token=None, refresh_token=None, token_expires_at=None,
                extra_state=extra,
            ),
        )

    fake.on_relogin_success = _relogin_succeeds
    notifier = _FakeNotifier()
    gate = GarminUpstreamCooldown()
    svc = _make_garmin(
        repo=repo, config=config, fake=fake, notifier=notifier,
        upstream_cooldown=gate,
    )

    result = svc.run_sync(
        user_id=1,
        since=datetime(2026, 4, 15, tzinfo=UTC),
        until=datetime(2026, 4, 15, 23, 59, tzinfo=UTC),
    )

    assert result.status == "success"
    assert fake.relogin_calls == 1
    auth = repo.get_auth_state(user_id=1, source="garmin")
    assert auth is not None
    assert auth.auth_status == "ok"
    assert auth.extra_state["tokens_blob"] == "fresh-blob-after-relogin"
    # Recovery was silent — no auth-broken notification fired.
    assert notifier.auth_broken_calls == []
    # A successful upstream contact clears the shared gate.
    assert gate.check() is None


def test_garmin_relogin_skipped_when_upstream_cooldown_hot(
    repo: FitnessRepository, config: Any,
) -> None:
    """A hot shared cooldown (e.g. the connect UI just got blocked) must
    suppress the unattended attempt entirely — straight to auth_broken."""
    from journal.services.fitness.garmin_pending import GarminUpstreamCooldown

    _seed_auth(repo, "garmin")
    fake = _relogin_capable_fake()
    notifier = _FakeNotifier()
    gate = GarminUpstreamCooldown()
    gate.record_block()  # the UI (or a previous run) observed a block
    svc = _make_garmin(
        repo=repo, config=config, fake=fake, notifier=notifier,
        upstream_cooldown=gate,
    )

    result = svc.run_sync(
        user_id=1,
        since=datetime(2026, 4, 15, tzinfo=UTC),
        until=datetime(2026, 4, 15, 23, 59, tzinfo=UTC),
    )

    assert result.status == "auth_broken"
    assert fake.relogin_calls == 0
    # No attempt was made, so the notification is the plain variant.
    assert notifier.auth_broken_calls == [(1, "garmin")]
    assert notifier.auth_broken_recovery_flags == [False]


def test_garmin_relogin_rate_limited_records_block_on_shared_cooldown(
    repo: FitnessRepository, config: Any,
) -> None:
    from journal.providers.garmin import GarminRateLimitError
    from journal.services.fitness.garmin_pending import GarminUpstreamCooldown

    _seed_auth(repo, "garmin")
    fake = _relogin_capable_fake()
    fake.raise_on_relogin = GarminRateLimitError("429 from Cloudflare")
    notifier = _FakeNotifier()
    gate = GarminUpstreamCooldown()
    svc = _make_garmin(
        repo=repo, config=config, fake=fake, notifier=notifier,
        upstream_cooldown=gate,
    )

    result = svc.run_sync(
        user_id=1,
        since=datetime(2026, 4, 15, tzinfo=UTC),
        until=datetime(2026, 4, 15, 23, 59, tzinfo=UTC),
    )

    assert result.status == "auth_broken"
    assert fake.relogin_calls == 1
    # The block was recorded on the SHARED gate so the connect UI
    # refuses further logins while it is hot.
    assert gate.check() is not None
    # An attempt was made and failed — the notification says so.
    assert notifier.auth_broken_calls == [(1, "garmin")]
    assert notifier.auth_broken_recovery_flags == [True]


def test_garmin_relogin_mfa_challenge_degrades_to_auth_broken_one_attempt(
    repo: FitnessRepository, config: Any,
) -> None:
    _seed_auth(repo, "garmin")
    fake = _relogin_capable_fake()
    fake.raise_on_relogin = GarminAuthError(
        "Garmin requested MFA — unattended re-login cannot complete",
    )
    notifier = _FakeNotifier()
    svc = _make_garmin(repo=repo, config=config, fake=fake, notifier=notifier)

    result = svc.run_sync(
        user_id=1,
        since=datetime(2026, 4, 15, tzinfo=UTC),
        until=datetime(2026, 4, 15, 23, 59, tzinfo=UTC),
    )

    assert result.status == "auth_broken"
    assert fake.relogin_calls == 1  # exactly one attempt, no retry loop
    assert notifier.auth_broken_calls == [(1, "garmin")]
    assert notifier.auth_broken_recovery_flags == [True]


def test_garmin_no_saved_credentials_keeps_current_behavior(
    repo: FitnessRepository, config: Any,
) -> None:
    """No saved password → no attempt; the pre-W6 auth_broken flow."""
    _seed_auth(repo, "garmin")
    fake = _FakeGarminProvider()
    fake.raise_on_login = GarminAuthError("401 from dead blob")
    assert not fake.can_relogin_with_password()
    notifier = _FakeNotifier()
    svc = _make_garmin(repo=repo, config=config, fake=fake, notifier=notifier)

    result = svc.run_sync(
        user_id=1,
        since=datetime(2026, 4, 15, tzinfo=UTC),
        until=datetime(2026, 4, 15, 23, 59, tzinfo=UTC),
    )

    assert result.status == "auth_broken"
    assert fake.relogin_calls == 0
    assert notifier.auth_broken_calls == [(1, "garmin")]
    assert notifier.auth_broken_recovery_flags == [False]


def test_garmin_second_auth_failure_in_same_run_does_not_relogin_again(
    repo: FitnessRepository, config: Any,
) -> None:
    """Re-login 'succeeds' but the retried fetch still 401s (e.g. the
    account is genuinely locked) — the run must fail auth_broken after
    exactly one re-login attempt, never loop."""
    _seed_auth(repo, "garmin")
    fake = _relogin_capable_fake()
    # on_relogin_success left as None: raise_on_login stays armed, so the
    # retried fetch hits the auth error again.
    notifier = _FakeNotifier()
    svc = _make_garmin(repo=repo, config=config, fake=fake, notifier=notifier)

    result = svc.run_sync(
        user_id=1,
        since=datetime(2026, 4, 15, tzinfo=UTC),
        until=datetime(2026, 4, 15, 23, 59, tzinfo=UTC),
    )

    assert result.status == "auth_broken"
    assert fake.relogin_calls == 1
    assert notifier.auth_broken_calls == [(1, "garmin")]
    assert notifier.auth_broken_recovery_flags == [True]


def test_garmin_relogin_attempted_without_cooldown_instance(
    repo: FitnessRepository, config: Any,
) -> None:
    """CLI-style construction passes no shared cooldown — the attempt
    still runs (a one-shot process has no gate state to consult)."""
    _seed_auth(repo, "garmin")
    fake = _relogin_capable_fake()

    def _relogin_succeeds() -> None:
        fake.raise_on_login = None

    fake.on_relogin_success = _relogin_succeeds
    notifier = _FakeNotifier()
    svc = _make_garmin(repo=repo, config=config, fake=fake, notifier=notifier)

    result = svc.run_sync(
        user_id=1,
        since=datetime(2026, 4, 15, tzinfo=UTC),
        until=datetime(2026, 4, 15, 23, 59, tzinfo=UTC),
    )

    assert result.status == "success"
    assert fake.relogin_calls == 1


def test_midrun_auth_lost_error_class_carries_reason() -> None:
    """The exception's ``reason`` attribute distinguishes removed vs broken
    so future callers (e.g., a more granular UI message) can branch on it."""
    removed = MidRunAuthLost("auth removed during run", reason="removed")
    broken = MidRunAuthLost("auth broken during run", reason="broken")
    assert removed.reason == "removed"
    assert broken.reason == "broken"
    assert str(removed) == "auth removed during run"
    # Subclass of FitnessError so callers depending on the base type
    # still catch it.
    from journal.services.fitness.errors import FitnessError
    assert isinstance(removed, FitnessError)
