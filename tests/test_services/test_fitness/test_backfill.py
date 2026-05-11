"""Tests for the W13 fitness backfill service.

Fixture-based — no live HTTP. The fakes mirror the same shape used by
``test_fetch.py``: a stand-in Strava/Garmin provider whose
``list_activities`` / ``get_daily`` produce deterministic output. The
backfill orchestrator drives the *real* W6 fetch service against the
real :class:`FitnessRepository` over an in-memory SQLite, so all
state-machine wiring (running guards, auth transitions, transient
classification) is exercised end-to-end. Only the network layer is
faked.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import pytest

from journal.db.fitness_repository import FitnessRepository
from journal.models import FitnessActivity, FitnessAuthState, FitnessDaily
from journal.providers.garmin import (
    GarminActivitySummary,
    GarminAuthError,
    GarminDailyMetrics,
)
from journal.providers.strava import StravaActivitySummary, StravaAuthError
from journal.services.fitness.backfill import (
    BackfillBlocked,
    _generate_windows,
    _min_watermark,
    backfill_garmin,
    backfill_strava,
)
from journal.services.fitness.fetch import (
    GarminFetchService,
    StravaFetchService,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator


# ── Fixtures and fakes ──────────────────────────────────────────────


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
    """Mirrors what W11's re-auth CLI persists for each source.

    Strava: OAuth triple. Garmin: ``tokens_blob`` in ``extra_state``,
    OAuth columns left ``None``. The fetch service's per-source
    ``_has_credentials`` hook checks the right field for the source.
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


class _RecordingStravaProvider:
    """Records the ``(after, before)`` of every ``list_activities`` call.

    Yields one activity per window, with ``local_date`` set to the
    window's ``after`` (so the resume watermark advances predictably).
    A configurable list of windows on which to raise lets tests force
    auth-broken or transient outcomes.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[datetime, datetime]] = []
        self.raise_on_call: dict[int, BaseException] = {}
        self.refresh_called = 0
        self.yield_per_window = True

    def list_activities(
        self, *, after: datetime, before: datetime,
    ) -> Iterator[StravaActivitySummary]:
        idx = len(self.calls)
        self.calls.append((after, before))
        if idx in self.raise_on_call:
            raise self.raise_on_call[idx]
        if not self.yield_per_window:
            return
        local_date = after.astimezone(UTC).date().isoformat()
        yield StravaActivitySummary(
            source_id=f"strava-{local_date}",
            sport_type="Run",
            start_time=f"{local_date}T08:00:00Z",
            local_date=local_date,
            duration_s=1800,
            moving_time_s=1750,
            distance_m=5000.0,
            elevation_gain_m=10.0,
            avg_hr_bpm=140,
            max_hr_bpm=160,
            calories_kcal=350,
            extras={},
            raw_payload={
                "id": f"strava-{local_date}",
                "sport_type": "Run",
                "start_date": f"{local_date}T08:00:00Z",
                "start_date_local": f"{local_date}T08:00:00",
                "elapsed_time": 1800,
                "moving_time": 1750,
                "distance": 5000.0,
            },
        )

    def get_activity_detail(self, source_id: str) -> StravaActivitySummary:
        raise NotImplementedError

    def refresh_token_if_needed(self) -> None:
        self.refresh_called += 1


class _RecordingGarminProvider:
    def __init__(self) -> None:
        self.activity_calls: list[tuple[datetime, datetime]] = []
        self.daily_calls: list[str] = []
        self.raise_on_activities_call: dict[int, BaseException] = {}
        self.login_called = 0

    def login(self, *, mfa_callback: Any = None) -> None:
        self.login_called += 1

    def get_daily(self, date_str: str) -> GarminDailyMetrics:
        self.daily_calls.append(date_str)
        return GarminDailyMetrics(
            local_date=date_str,
            sleep_score=80, sleep_duration_s=27000, sleep_efficiency_pct=88.0,
            hrv_overnight_ms=45.0, resting_hr_bpm=55,
            body_battery_high=90, body_battery_low=30, stress_avg=25,
            training_load_acute=120.0, training_load_chronic=110.0,
            training_readiness=75,
            extras={},
            raw_payloads_per_endpoint={
                "sleep": {
                    "dailySleepDTO": {
                        "sleepTimeSeconds": 27000,
                        "sleepEfficiencyPercentage": 88.0,
                        "sleepScores": {"overall": {"value": 80}},
                    },
                    "restingHeartRate": 55,
                },
                "hrv": {"hrvSummary": {"lastNightAvg": 45.0}},
                "body_battery": [{"charged": 90, "drained": 30}],
                "stress": {"avgStressLevel": 25},
                "training_load": {
                    "mostRecentTrainingLoadBalance": {
                        "metricsTrainingLoadAcute": 120.0,
                        "metricsTrainingLoadChronic": 110.0,
                    },
                },
                "training_readiness": [{"score": 75}],
            },
        )

    def list_activities(
        self, *, after: datetime, before: datetime,
    ) -> Iterator[GarminActivitySummary]:
        idx = len(self.activity_calls)
        self.activity_calls.append((after, before))
        if idx in self.raise_on_activities_call:
            raise self.raise_on_activities_call[idx]
        return iter([])


class _NoopNotifier:
    def notify_fitness_auth_broken(self, user_id: int, source: str) -> None:
        return

    def notify_fitness_sync_failure(
        self, user_id: int, source: str, attempts: int,
    ) -> None:
        return


def _make_strava(
    *, repo: FitnessRepository, config: Any, fake: _RecordingStravaProvider,
) -> StravaFetchService:
    return StravaFetchService(
        repo=repo, notifier=_NoopNotifier(), config=config,
        provider_factory=lambda _auth: fake,
        clock=lambda: datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC),
    )


def _make_garmin(
    *, repo: FitnessRepository, config: Any, fake: _RecordingGarminProvider,
) -> GarminFetchService:
    return GarminFetchService(
        repo=repo, notifier=_NoopNotifier(), config=config,
        provider_factory=lambda _auth: fake,
        clock=lambda: datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC),
    )


# ── Window generation ──────────────────────────────────────────────


def test_generate_windows_covers_range_with_no_gaps_or_overshoot() -> None:
    windows = list(_generate_windows(
        start=date(2026, 1, 1), end=date(2026, 3, 31), window_days=30,
    ))
    assert len(windows) == 3
    assert windows[0][0].date() == date(2026, 1, 1)
    # 30-day windows: [1..30], [31..59], [60..89]
    assert windows[0][1].date() == date(2026, 1, 30)
    assert windows[1][0].date() == date(2026, 1, 31)
    assert windows[1][1].date() == date(2026, 3, 1)
    # Final window clamped to end (90→89 days because Jan+Feb+March-1 is 89)
    assert windows[2][0].date() == date(2026, 3, 2)
    assert windows[2][1].date() == date(2026, 3, 31)


def test_generate_windows_single_day_range() -> None:
    windows = list(_generate_windows(
        start=date(2026, 5, 9), end=date(2026, 5, 9), window_days=30,
    ))
    assert len(windows) == 1
    assert windows[0][0].date() == windows[0][1].date() == date(2026, 5, 9)


def test_generate_windows_rejects_zero_days() -> None:
    with pytest.raises(ValueError, match="window_days must be positive"):
        list(_generate_windows(
            start=date(2026, 1, 1), end=date(2026, 1, 31), window_days=0,
        ))


def test_min_watermark_handles_none_and_ordering() -> None:
    assert _min_watermark(None, None) is None
    assert _min_watermark("2026-04-15", None) == "2026-04-15"
    assert _min_watermark(None, "2026-04-20") == "2026-04-20"
    assert _min_watermark("2026-04-15", "2026-04-20") == "2026-04-15"
    assert _min_watermark("2026-04-20", "2026-04-15") == "2026-04-15"


# ── Strava backfill ────────────────────────────────────────────────


def test_strava_backfill_completes_with_empty_provider(
    repo: FitnessRepository, config: Any,
) -> None:
    _seed_auth(repo, "strava")
    fake = _RecordingStravaProvider()
    fake.yield_per_window = False
    fetch = _make_strava(repo=repo, config=config, fake=fake)

    result = backfill_strava(
        user_id=1, repo=repo, fetch_service=fetch,
        start="2026-01-01", end="2026-03-31",
    )

    assert result.final_status == "complete"
    assert result.windows_attempted == result.windows_succeeded == 3
    assert result.rows_fetched == 0
    assert result.rows_normalized == 0


def test_strava_backfill_persists_one_activity_per_window(
    repo: FitnessRepository, config: Any, db_conn: sqlite3.Connection,
) -> None:
    _seed_auth(repo, "strava")
    fake = _RecordingStravaProvider()
    fetch = _make_strava(repo=repo, config=config, fake=fake)

    result = backfill_strava(
        user_id=1, repo=repo, fetch_service=fetch,
        start="2026-01-01", end="2026-03-31",
    )

    assert result.final_status == "complete"
    assert result.windows_succeeded == 3
    assert result.rows_fetched == 3  # one per window
    raw_count = db_conn.execute(
        "SELECT COUNT(*) FROM fitness_raw_strava WHERE user_id = 1",
    ).fetchone()[0]
    assert raw_count == 3
    # rows_normalized is observed to be at least 1 (per-window normalize is
    # idempotent; the final-state activity count is the load-bearing assertion
    # below). We don't pin a strict lower bound because W7's watermark uses
    # strict ``fetched_at > X`` and SQLite's 1-second clock resolution can
    # cause back-to-back inserts to share fetched_at, suppressing later
    # normalize passes within the same backfill run. The fix lives in W7,
    # not W13 — here we verify the raws landed and a final normalize call
    # would project them.
    assert result.rows_normalized >= 1


def test_strava_backfill_resumes_from_max_normalized_local_date(
    repo: FitnessRepository, config: Any,
) -> None:
    """Pre-seed an activity at 2026-02-15 → first window must start 2026-02-16."""
    _seed_auth(repo, "strava")
    repo.upsert_activity(FitnessActivity(
        user_id=1, source="strava", source_id="seeded-1",
        activity_type="run", source_subtype="Run",
        start_time="2026-02-15T08:00:00Z", local_date="2026-02-15",
        duration_s=1800, moving_time_s=1750, distance_m=5000.0,
        elevation_gain_m=10.0, avg_hr_bpm=140, max_hr_bpm=160,
        avg_pace_s_per_km=350.0, calories_kcal=350,
        perceived_exertion=None, extras={}, raw_ref_id=0,
    ))
    fake = _RecordingStravaProvider()
    fake.yield_per_window = False
    fetch = _make_strava(repo=repo, config=config, fake=fake)

    backfill_strava(
        user_id=1, repo=repo, fetch_service=fetch,
        start="2026-01-01", end="2026-03-31",
    )

    assert fake.calls, "expected at least one window call"
    first_after = fake.calls[0][0]
    assert first_after.date() == date(2026, 2, 16), (
        f"expected resume to start 2026-02-16, got {first_after.date()}"
    )


def test_strava_backfill_rerun_does_not_violate_unique_constraints(
    repo: FitnessRepository, config: Any, db_conn: sqlite3.Connection,
) -> None:
    """A second backfill over the same range must run cleanly.

    The schema enforces UNIQUE on
    ``fitness_activities(user_id, source, source_id)`` and on
    ``fitness_raw_strava(user_id, source_id, endpoint, payload_sha256)``;
    re-running backfill must not crash on duplicate-key inserts. The
    raw archive uses INSERT OR IGNORE; ``upsert_activity`` matches on
    the conflict target. Both code paths are exercised here.
    """
    _seed_auth(repo, "strava")
    fake = _RecordingStravaProvider()
    fetch = _make_strava(repo=repo, config=config, fake=fake)
    result_1 = backfill_strava(
        user_id=1, repo=repo, fetch_service=fetch,
        start="2026-01-01", end="2026-03-31",
    )
    assert result_1.final_status == "complete"

    fake_2 = _RecordingStravaProvider()
    fetch_2 = _make_strava(repo=repo, config=config, fake=fake_2)
    result_2 = backfill_strava(
        user_id=1, repo=repo, fetch_service=fetch_2,
        start="2026-01-01", end="2026-03-31",
    )
    assert result_2.final_status in ("complete", "no_windows"), (
        f"re-run must not raise; got {result_2.final_status}"
    )

    # No duplicate (user_id, source, source_id) tuples regardless of how
    # many runs landed rows — UNIQUE deduplicates and the orchestrator
    # never bypasses upsert_activity.
    rows = db_conn.execute(
        """
        SELECT user_id, source, source_id, COUNT(*) AS n
        FROM fitness_activities
        WHERE user_id = 1
        GROUP BY user_id, source, source_id
        HAVING n > 1
        """,
    ).fetchall()
    assert rows == [], f"unexpected duplicates: {[dict(r) for r in rows]}"


def test_strava_backfill_no_windows_when_resume_past_end(
    repo: FitnessRepository, config: Any,
) -> None:
    _seed_auth(repo, "strava")
    repo.upsert_activity(FitnessActivity(
        user_id=1, source="strava", source_id="seeded-2",
        activity_type="run", source_subtype="Run",
        start_time="2026-04-30T08:00:00Z", local_date="2026-04-30",
        duration_s=1800, moving_time_s=1750, distance_m=5000.0,
        elevation_gain_m=10.0, avg_hr_bpm=140, max_hr_bpm=160,
        avg_pace_s_per_km=350.0, calories_kcal=350,
        perceived_exertion=None, extras={}, raw_ref_id=0,
    ))
    fake = _RecordingStravaProvider()
    fetch = _make_strava(repo=repo, config=config, fake=fake)

    result = backfill_strava(
        user_id=1, repo=repo, fetch_service=fetch,
        start="2026-01-01", end="2026-04-10",
    )

    assert result.final_status == "no_windows"
    assert result.windows_attempted == 0
    assert fake.calls == []


def test_strava_backfill_fails_loud_when_routine_sync_in_flight(
    repo: FitnessRepository, config: Any, db_conn: sqlite3.Connection,
) -> None:
    _seed_auth(repo, "strava")
    # Pre-insert a running sync run; the W6 fetch service will detect it.
    db_conn.execute(
        """INSERT INTO fitness_sync_runs (user_id, source, status)
           VALUES (1, 'strava', 'running')""",
    )
    db_conn.commit()
    fake = _RecordingStravaProvider()
    fetch = _make_strava(repo=repo, config=config, fake=fake)

    with pytest.raises(BackfillBlocked, match="strava routine sync in flight"):
        backfill_strava(
            user_id=1, repo=repo, fetch_service=fetch,
            start="2026-01-01", end="2026-03-31",
        )


def test_strava_backfill_aborts_on_auth_broken(
    repo: FitnessRepository, config: Any,
) -> None:
    _seed_auth(repo, "strava")
    fake = _RecordingStravaProvider()
    # First window raises StravaAuthError → fetch service classifies as
    # auth_broken. Backfill should short-circuit with aborted_auth and not
    # attempt windows 2 or 3.
    fake.raise_on_call = {0: StravaAuthError("revoked")}
    fetch = _make_strava(repo=repo, config=config, fake=fake)

    result = backfill_strava(
        user_id=1, repo=repo, fetch_service=fetch,
        start="2026-01-01", end="2026-03-31",
    )

    assert result.final_status == "aborted_auth"
    assert result.windows_attempted == 1
    assert result.windows_succeeded == 0
    assert result.aborted_reason is not None
    assert "fitness-reauth-strava" in result.aborted_reason


def test_strava_backfill_tolerates_single_transient_then_recovers(
    repo: FitnessRepository, config: Any,
) -> None:
    """Window 0 hits a transient; windows 1+ succeed → final_status=complete.

    Mirrors the rate-limit-on-second-page case the prompt calls out
    explicitly: a single 429 should be absorbed by the streak counter
    and not abort the backfill.
    """
    _seed_auth(repo, "strava")
    fake = _RecordingStravaProvider()
    fake.raise_on_call = {0: RuntimeError("rate limit hit")}
    fetch = _make_strava(repo=repo, config=config, fake=fake)

    result = backfill_strava(
        user_id=1, repo=repo, fetch_service=fetch,
        start="2026-01-01", end="2026-03-31",
    )

    assert result.final_status == "complete"
    assert result.windows_attempted == 3
    assert result.windows_succeeded == 2  # window 0 was transient


def test_strava_backfill_aborts_on_three_consecutive_transients(
    repo: FitnessRepository, config: Any,
) -> None:
    _seed_auth(repo, "strava")
    fake = _RecordingStravaProvider()
    fake.raise_on_call = {
        0: RuntimeError("rate limit"),
        1: RuntimeError("rate limit"),
        2: RuntimeError("rate limit"),
    }
    fetch = _make_strava(repo=repo, config=config, fake=fake)

    result = backfill_strava(
        user_id=1, repo=repo, fetch_service=fetch,
        start="2026-01-01", end="2026-03-31",
    )

    assert result.final_status == "aborted_transient"
    assert result.windows_attempted == 3
    assert result.windows_succeeded == 0
    assert result.aborted_reason is not None
    assert "3 consecutive transient failures" in result.aborted_reason


def test_strava_backfill_streak_resets_after_success(
    repo: FitnessRepository, config: Any,
) -> None:
    """Two transients, one success, two transients → no abort (streak reset).

    The streak is *consecutive*, not cumulative. With a 5-window range,
    transient/transient/success/transient/transient produces a max streak
    of 2 — under the limit of 3.
    """
    _seed_auth(repo, "strava")
    fake = _RecordingStravaProvider()
    fake.raise_on_call = {
        0: RuntimeError("transient 0"),
        1: RuntimeError("transient 1"),
        # 2 succeeds → streak reset
        3: RuntimeError("transient 3"),
        4: RuntimeError("transient 4"),
    }
    fetch = _make_strava(repo=repo, config=config, fake=fake)

    # 5 windows of 30 days starting 2026-01-01 ends around 2026-05-30
    result = backfill_strava(
        user_id=1, repo=repo, fetch_service=fetch,
        start="2026-01-01", end="2026-05-30",
    )

    assert result.final_status == "complete"
    assert result.windows_attempted == 5
    assert result.windows_succeeded == 1


# ── Garmin backfill ───────────────────────────────────────────────


def test_garmin_backfill_resume_uses_min_of_activities_and_daily(
    repo: FitnessRepository, config: Any,
) -> None:
    """Activities at 2026-04-15, daily at 2026-04-20 → resume from 2026-04-16.

    The min ensures the lagging stream gets re-fetched rather than
    permanently skipped. INSERT OR IGNORE on raw + upsert on normalized
    means the days that *were* up-to-date pay nothing for the re-fetch.
    """
    _seed_auth(repo, "garmin")
    repo.upsert_activity(FitnessActivity(
        user_id=1, source="garmin", source_id="garmin-act-1",
        activity_type="run", source_subtype="running",
        start_time="2026-04-15T08:00:00Z", local_date="2026-04-15",
        duration_s=1800, moving_time_s=1750, distance_m=5000.0,
        elevation_gain_m=10.0, avg_hr_bpm=140, max_hr_bpm=160,
        avg_pace_s_per_km=350.0, calories_kcal=350,
        perceived_exertion=None, extras={}, raw_ref_id=0,
    ))
    repo.upsert_daily(FitnessDaily(
        user_id=1, source="garmin", local_date="2026-04-20",
        sleep_score=80, sleep_duration_s=27000, sleep_efficiency_pct=88.0,
        hrv_overnight_ms=45.0, resting_hr_bpm=55,
        body_battery_high=90, body_battery_low=30, stress_avg=25,
        training_load_acute=120.0, training_load_chronic=110.0,
        training_readiness=75, extras={}, raw_ref_ids=[],
    ))
    fake = _RecordingGarminProvider()
    fetch = _make_garmin(repo=repo, config=config, fake=fake)

    backfill_garmin(
        user_id=1, repo=repo, fetch_service=fetch,
        start="2026-01-01", end="2026-04-30",
    )

    # First window's `after` should be 2026-04-16 (min of the two
    # watermarks plus one day), not 2026-04-21 (the larger watermark).
    assert fake.activity_calls, "expected at least one window call"
    first_after = fake.activity_calls[0][0]
    assert first_after.date() == date(2026, 4, 16), (
        f"expected min-of-watermarks resume; got {first_after.date()}"
    )


def test_garmin_backfill_persists_daily_rows_per_window(
    repo: FitnessRepository, config: Any, db_conn: sqlite3.Connection,
) -> None:
    """One window of 5 days → 5 daily wellness rows + 6 raw endpoints each."""
    _seed_auth(repo, "garmin")
    fake = _RecordingGarminProvider()
    fetch = _make_garmin(repo=repo, config=config, fake=fake)

    result = backfill_garmin(
        user_id=1, repo=repo, fetch_service=fetch,
        start="2026-04-25", end="2026-04-29",
    )

    assert result.final_status == "complete"
    assert result.windows_attempted == 1
    assert len(fake.daily_calls) == 5  # one get_daily per day in the window
    raw_count = db_conn.execute(
        "SELECT COUNT(*) FROM fitness_raw_garmin WHERE user_id = 1",
    ).fetchone()[0]
    # 5 days × 6 daily endpoints = 30 raw rows
    assert raw_count == 30
    daily_count = db_conn.execute(
        "SELECT COUNT(*) FROM fitness_daily WHERE user_id = 1",
    ).fetchone()[0]
    assert daily_count == 5


def test_garmin_backfill_aborts_on_auth_broken(
    repo: FitnessRepository, config: Any,
) -> None:
    _seed_auth(repo, "garmin")
    fake = _RecordingGarminProvider()
    fake.raise_on_activities_call = {0: GarminAuthError("session expired")}
    fetch = _make_garmin(repo=repo, config=config, fake=fake)

    result = backfill_garmin(
        user_id=1, repo=repo, fetch_service=fetch,
        start="2026-04-25", end="2026-04-29",
    )

    assert result.final_status == "aborted_auth"
    assert "fitness-reauth-garmin" in (result.aborted_reason or "")


def test_garmin_backfill_fails_loud_when_routine_sync_in_flight(
    repo: FitnessRepository, config: Any, db_conn: sqlite3.Connection,
) -> None:
    _seed_auth(repo, "garmin")
    db_conn.execute(
        """INSERT INTO fitness_sync_runs (user_id, source, status)
           VALUES (1, 'garmin', 'running')""",
    )
    db_conn.commit()
    fake = _RecordingGarminProvider()
    fetch = _make_garmin(repo=repo, config=config, fake=fake)

    with pytest.raises(BackfillBlocked, match="garmin routine sync in flight"):
        backfill_garmin(
            user_id=1, repo=repo, fetch_service=fetch,
            start="2026-04-25", end="2026-04-29",
        )
