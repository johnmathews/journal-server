"""Tests for the W7 fitness normalize service.

Covers the seven scenarios from ``docs/fitness-tier-plan.md`` §W7:
Strava activity normalize across all coarse types, Garmin daily fan-in,
re-publish authoritativeness (newest fetched_at wins), idempotent
re-run, drift on missing required field, activity-type mapping edge
cases, and a dirty-state fixture exercising valid + drift +
duplicates together.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from journal.db.fitness_repository import FitnessRepository
from journal.services.fitness._activity_type_map import (
    coarse_garmin,
    coarse_strava,
)
from journal.services.fitness.normalize import (
    NormalizeResult,
    normalize_garmin,
    normalize_strava,
)

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


class _CapturingNotifier:
    def __init__(self) -> None:
        self.drift_calls: list[tuple[str, int]] = []

    def notify_fitness_normalize_drift(
        self, source: str, drift_count: int,
    ) -> None:
        self.drift_calls.append((source, drift_count))


def _strava_payload(
    activity_id: int,
    sport_type: str = "Run",
    *,
    omit: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Hand-shaped to match ``stravalib.SummaryActivity.model_dump``."""
    payload = {
        "id": activity_id,
        "sport_type": sport_type,
        "type": sport_type,
        "start_date": "2026-04-21T07:15:00Z",
        "start_date_local": "2026-04-21T09:15:00",
        "elapsed_time": 1830,
        "moving_time": 1790,
        "distance": 5612.4,
        "total_elevation_gain": 42.1,
        "average_heartrate": 148.6,
        "max_heartrate": 169,
        "calories": 412,
    }
    for key in omit:
        payload.pop(key, None)
    return payload


def _garmin_activity_payload(
    activity_id: int,
    type_key: str = "running",
) -> dict[str, Any]:
    return {
        "activityId": activity_id,
        "activityType": {"typeId": 1, "typeKey": type_key, "parentTypeId": 17},
        "startTimeGMT": "2026-04-21 07:15:00",
        "startTimeLocal": "2026-04-21 09:15:00",
        "duration": 1830.0,
        "movingDuration": 1790.0,
        "distance": 5612.4,
        "elevationGain": 42.1,
        "averageHR": 148.6,
        "maxHR": 169.0,
        "calories": 412.0,
    }


_GARMIN_DAILY_FIXTURE: dict[str, Any] = {
    "sleep": {
        "dailySleepDTO": {
            "sleepTimeSeconds": 27180,
            "sleepEfficiencyPercentage": 92.7,
            "sleepScores": {"overall": {"value": 84}},
        },
        "restingHeartRate": 51,
    },
    "hrv": {"hrvSummary": {"lastNightAvg": 47.5}},
    "body_battery": [{"date": "2026-04-15", "charged": 78, "drained": 41}],
    "stress": {"avgStressLevel": 31},
    "training_load": {
        "mostRecentTrainingStatus": {
            "latestTrainingStatusData": {
                "3300000001": {
                    "primaryTrainingDevice": True,
                    "acuteTrainingLoadDTO": {
                        "dailyTrainingLoadAcute": 412,
                        "dailyTrainingLoadChronic": 380,
                    },
                },
            },
        },
    },
    "training_readiness": [{"score": 78}],
}


def _insert_strava(
    repo: FitnessRepository, activity_id: int,
    sport_type: str = "Run", *,
    omit: tuple[str, ...] = (),
    sync_run_id: int | None = None,
) -> int | None:
    return repo.insert_raw(
        source="strava", user_id=1,
        endpoint="activities", source_id=str(activity_id),
        payload_json=json.dumps(
            _strava_payload(activity_id, sport_type, omit=omit),
            sort_keys=True,
        ),
        sync_run_id=sync_run_id,
    )


def _insert_garmin_daily(
    repo: FitnessRepository, local_date: str,
    *,
    payloads: dict[str, Any] | None = None,
    sync_run_id: int | None = None,
) -> dict[str, int | None]:
    """Insert one raw row per Garmin daily endpoint. Returns the row ids by endpoint."""
    payloads = payloads if payloads is not None else _GARMIN_DAILY_FIXTURE
    ids: dict[str, int | None] = {}
    for endpoint, payload in payloads.items():
        ids[endpoint] = repo.insert_raw(
            source="garmin", user_id=1,
            endpoint=endpoint, source_id=local_date,
            payload_json=json.dumps(payload, sort_keys=True),
            sync_run_id=sync_run_id,
        )
    return ids


# ── Tests ────────────────────────────────────────────────────────────


# 1. Strava activity normalize across all coarse types ----------------


@pytest.mark.parametrize(
    ("sport_type", "expected_coarse"),
    [
        ("Run", "run"),
        ("Ride", "ride"),
        ("Swim", "swim"),
        ("Walk", "walk"),
        ("Hike", "hike"),
        ("WeightTraining", "strength"),
        ("Yoga", "other"),
    ],
)
def test_strava_normalize_maps_each_coarse_type(
    repo: FitnessRepository, sport_type: str, expected_coarse: str,
    db_conn: sqlite3.Connection,
) -> None:
    notifier = _CapturingNotifier()
    _insert_strava(repo, 11000000001, sport_type)

    result = normalize_strava(repo, user_id=1, notifier=notifier)

    assert result == NormalizeResult(
        source="strava", rows_normalized=1, drift_count=0,
    )
    row = db_conn.execute(
        "SELECT activity_type, source_subtype FROM fitness_activities WHERE user_id=1",
    ).fetchone()
    assert row["activity_type"] == expected_coarse
    assert row["source_subtype"] == sport_type
    assert notifier.drift_calls == []


# 2. Garmin daily fan-in ---------------------------------------------


def test_normalize_strava_amends_sync_run_rows_normalized(
    repo: FitnessRepository,
) -> None:
    """F1 regression: when normalize is wired to a fetch's sync_run_id, the
    existing sync_runs row's rows_normalized is updated. Without this fix the
    UI's `Norm.` column was always 0 on success.

    Extended by T7: Strava is workouts-only so workouts_normalized = the
    full count and wellness_normalized = 0.
    """
    _insert_strava(repo, 11000000001, "Run")
    run_id = repo.start_sync_run(user_id=1, source="strava")
    repo.finish_sync_run(
        run_id, status="success", rows_fetched=1, rows_normalized=0,
    )

    result = normalize_strava(repo, user_id=1, sync_run_id=run_id)

    assert result.rows_normalized == 1
    runs = repo.list_recent_sync_runs(user_id=1, source="strava")
    assert len(runs) == 1
    assert runs[0].status == "success"
    assert runs[0].rows_fetched == 1
    assert runs[0].rows_normalized == 1
    # T7: every Strava normalized row is a workout.
    assert runs[0].workouts_normalized == 1
    assert runs[0].wellness_normalized == 0


def test_normalize_garmin_amends_sync_run_rows_normalized(
    repo: FitnessRepository,
) -> None:
    """F1 regression for Garmin. See Strava counterpart.

    Extended by T7: the Garmin daily fan-in is wellness; the activity
    loop is workouts. This fixture only inserts daily rows, so the
    expected split is wellness=1, workouts=0.
    """
    _insert_garmin_daily(repo, "2026-04-15")
    run_id = repo.start_sync_run(user_id=1, source="garmin")
    repo.finish_sync_run(
        run_id, status="success", rows_fetched=6, rows_normalized=0,
    )

    result = normalize_garmin(repo, user_id=1, sync_run_id=run_id)

    assert result.rows_normalized == 1
    runs = repo.list_recent_sync_runs(user_id=1, source="garmin")
    assert len(runs) == 1
    assert runs[0].rows_normalized == 1
    assert runs[0].rows_fetched == 6  # untouched by the amend
    # T7: this fixture is wellness-only.
    assert runs[0].workouts_normalized == 0
    assert runs[0].wellness_normalized == 1


def test_normalize_without_sync_run_id_leaves_runs_unchanged(
    repo: FitnessRepository,
) -> None:
    """When called outside a fetch context (CLI manual normalize, backfill),
    normalize must not invent a sync_run row to update."""
    _insert_strava(repo, 11000000001, "Run")
    run_id = repo.start_sync_run(user_id=1, source="strava")
    repo.finish_sync_run(
        run_id, status="success", rows_fetched=42, rows_normalized=99,
    )

    result = normalize_strava(repo, user_id=1)  # no sync_run_id

    assert result.rows_normalized == 1
    runs = repo.list_recent_sync_runs(user_id=1, source="strava")
    assert runs[0].rows_normalized == 99  # untouched


def test_garmin_daily_fan_in_six_endpoints_to_one_row(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    notifier = _CapturingNotifier()
    raw_ids = _insert_garmin_daily(repo, "2026-04-15")

    result = normalize_garmin(repo, user_id=1, notifier=notifier)

    assert result == NormalizeResult(
        source="garmin", rows_normalized=1, drift_count=0,
    )
    rows = db_conn.execute(
        "SELECT * FROM fitness_daily WHERE user_id=1",
    ).fetchall()
    assert len(rows) == 1
    daily = rows[0]
    assert daily["local_date"] == "2026-04-15"
    assert daily["sleep_score"] == 84
    assert daily["sleep_duration_s"] == 27180
    assert daily["sleep_efficiency_pct"] == pytest.approx(92.7)
    assert daily["hrv_overnight_ms"] == pytest.approx(47.5)
    assert daily["resting_hr_bpm"] == 51
    assert daily["body_battery_high"] == 78
    assert daily["body_battery_low"] == 41
    assert daily["stress_avg"] == 31
    assert daily["training_load_acute"] == pytest.approx(412.0)
    assert daily["training_load_chronic"] == pytest.approx(380.0)
    assert daily["training_readiness"] == 78

    raw_ref_ids = sorted(json.loads(daily["raw_ref_ids_json"]))
    expected_ids = sorted(i for i in raw_ids.values() if i is not None)
    assert raw_ref_ids == expected_ids
    assert len(raw_ref_ids) == 6


# 3. Garmin re-publish authoritativeness -----------------------------


def test_garmin_republish_uses_newest_fetched_at(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    """Two raw rows for (sleep, 2026-04-15) with different sha256 + fetched_at.
    Newest wins; older row stays in raw."""
    # First publish — old data
    old_payload = json.dumps(
        {"dailySleepDTO": {"sleepTimeSeconds": 20000,
                           "sleepScores": {"overall": {"value": 60}}},
         "restingHeartRate": 55},
        sort_keys=True,
    )
    old_id = repo.insert_raw(
        source="garmin", user_id=1,
        endpoint="sleep", source_id="2026-04-15",
        payload_json=old_payload, sync_run_id=None,
    )
    # Force a measurable fetched_at gap (the SQL CURRENT_TIMESTAMP is sub-second).
    time.sleep(1.1)
    # Second publish — corrected data
    new_payload = json.dumps(
        {"dailySleepDTO": {"sleepTimeSeconds": 27180,
                           "sleepScores": {"overall": {"value": 84}}},
         "restingHeartRate": 51},
        sort_keys=True,
    )
    new_id = repo.insert_raw(
        source="garmin", user_id=1,
        endpoint="sleep", source_id="2026-04-15",
        payload_json=new_payload, sync_run_id=None,
    )
    assert old_id != new_id  # both inserted (different sha256)

    notifier = _CapturingNotifier()
    normalize_garmin(repo, user_id=1, notifier=notifier)

    daily = db_conn.execute(
        "SELECT * FROM fitness_daily WHERE user_id=1 AND local_date='2026-04-15'",
    ).fetchone()
    assert daily["sleep_score"] == 84  # the new value
    assert daily["sleep_duration_s"] == 27180
    raw_ref_ids = json.loads(daily["raw_ref_ids_json"])
    assert raw_ref_ids == [new_id]
    # Older row remains in raw
    raw_count = db_conn.execute(
        "SELECT COUNT(*) AS n FROM fitness_raw_garmin "
        "WHERE user_id=1 AND endpoint='sleep' AND source_id='2026-04-15'",
    ).fetchone()
    assert raw_count["n"] == 2


# 4. Idempotent re-run -----------------------------------------------


def test_strava_idempotent_re_run_no_duplicates(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    _insert_strava(repo, 11000000001, "Run")

    r1 = normalize_strava(repo, user_id=1)
    time.sleep(1.1)

    r2 = normalize_strava(repo, user_id=1)
    rows = db_conn.execute(
        "SELECT id, normalized_at FROM fitness_activities WHERE user_id=1",
    ).fetchall()

    # First pass normalized 1 row; second pass sees nothing new (the
    # raw row's fetched_at is already <= the watermark) so 0 rows are
    # touched. Either way, only one normalized row exists.
    assert r1.rows_normalized == 1
    assert r2.rows_normalized in (0, 1)
    assert len(rows) == 1


def test_garmin_idempotent_re_run_no_duplicates(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    _insert_garmin_daily(repo, "2026-04-15")

    r1 = normalize_garmin(repo, user_id=1)
    r2 = normalize_garmin(repo, user_id=1)

    rows = db_conn.execute(
        "SELECT id FROM fitness_daily WHERE user_id=1",
    ).fetchall()
    assert r1.rows_normalized == 1
    assert r2.rows_normalized in (0, 1)
    assert len(rows) == 1


# 5. Drift on missing required field ---------------------------------


def test_strava_drift_skips_row_records_sync_run_fires_once(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    """A row missing start_date_local is skipped; the rest of the batch
    succeeds; one normalize_drift sync_run row is recorded; the
    Pushover topic fires once for the batch (not per row)."""
    notifier = _CapturingNotifier()
    _insert_strava(repo, 11000000001, "Run")  # valid
    _insert_strava(repo, 11000000002, "Ride", omit=("start_date_local",))  # drift
    _insert_strava(repo, 11000000003, "Walk", omit=("start_date_local",))  # drift

    result = normalize_strava(repo, user_id=1, notifier=notifier)

    assert result.rows_normalized == 1
    assert result.drift_count == 2
    assert notifier.drift_calls == [("strava", 2)]
    # Only the valid row landed in fitness_activities
    rows = db_conn.execute(
        "SELECT source_id FROM fitness_activities WHERE user_id=1",
    ).fetchall()
    assert [r["source_id"] for r in rows] == ["11000000001"]
    # Exactly one normalize_drift sync_run row, with drift_count in notes
    drift_runs = db_conn.execute(
        "SELECT status, error_class, notes_json FROM fitness_sync_runs "
        "WHERE user_id=1 AND status='normalize_drift'",
    ).fetchall()
    assert len(drift_runs) == 1
    assert drift_runs[0]["error_class"] == "NormalizeDrift"
    assert json.loads(drift_runs[0]["notes_json"])["drift_count"] == 2


def test_drift_without_notifier_still_records_sync_run(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    """notifier=None → no Pushover, but the sync_run row is still recorded."""
    _insert_strava(repo, 1, "Run", omit=("start_date_local",))

    result = normalize_strava(repo, user_id=1, notifier=None)

    assert result.drift_count == 1
    drift_runs = db_conn.execute(
        "SELECT id FROM fitness_sync_runs "
        "WHERE user_id=1 AND status='normalize_drift'",
    ).fetchall()
    assert len(drift_runs) == 1


# 6. Activity-type mapping edge cases --------------------------------


@pytest.mark.parametrize(
    ("sport_type", "expected"),
    [
        # W5: `Rowing` now collapses to the canonical `row` type, not
        # `other`. The verbatim sport_type is still preserved in
        # source_subtype for downstream filtering.
        ("Rowing", "row"),
        ("WeightTraining", "strength"),
        ("MountainBikeRide", "ride"),
        ("VirtualRun", "run"),
        ("EBikeRide", "ride"),
        ("Crossfit", "strength"),
        ("AlpineSki", "other"),
    ],
)
def test_strava_activity_type_mapping(sport_type: str, expected: str) -> None:
    """One-to-one mapping table from fitness-schema.md §3."""
    assert coarse_strava(sport_type) == expected


@pytest.mark.parametrize(
    ("type_key", "expected"),
    [
        ("running", "run"),
        ("treadmill_running", "run"),
        ("cycling", "ride"),
        ("mountain_biking", "ride"),
        ("lap_swimming", "swim"),
        ("walking", "walk"),
        ("hiking", "hike"),
        ("strength_training", "strength"),
        # W5: both Garmin rowing typeKeys map to the new canonical `row`.
        ("rowing", "row"),
        ("indoor_rowing", "row"),
        ("kayaking", "other"),
        ("unknown_future_thing", "other"),
    ],
)
def test_garmin_activity_type_mapping(type_key: str, expected: str) -> None:
    assert coarse_garmin(type_key) == expected


# 7. Dirty-state prod-shaped fixture ---------------------------------


def test_dirty_state_batch_normalizes_valid_skips_drift_dedupes_republish(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    """One pass over a mix of (a) valid Strava activities, (b) Strava
    drift, (c) duplicate-payload Strava (idempotent insert returns
    None), and (d) Garmin daily with a re-publish.

    Asserts: every valid row lands, drifts skip with one drift sync-run,
    republish keeps the newer row, batch is fully recoverable.
    """
    notifier = _CapturingNotifier()

    # (a) Valid Strava activities
    _insert_strava(repo, 1, "Run")
    _insert_strava(repo, 2, "Ride")
    # (b) Strava drift — missing start_date_local
    _insert_strava(repo, 3, "Run", omit=("start_date_local",))
    # (c) Duplicate insert — same activity, same payload sha256.
    # insert_raw returns None for the duplicate; nothing changes downstream.
    dup_id = _insert_strava(repo, 1, "Run")
    assert dup_id is None

    # (d) Garmin daily with re-publish on the sleep endpoint
    _insert_garmin_daily(repo, "2026-04-15")
    time.sleep(1.1)
    repo.insert_raw(
        source="garmin", user_id=1,
        endpoint="sleep", source_id="2026-04-15",
        payload_json=json.dumps({
            "dailySleepDTO": {"sleepTimeSeconds": 28000,
                              "sleepScores": {"overall": {"value": 90}}},
            "restingHeartRate": 50,
        }, sort_keys=True),
        sync_run_id=None,
    )

    strava_result = normalize_strava(repo, user_id=1, notifier=notifier)
    garmin_result = normalize_garmin(repo, user_id=1, notifier=notifier)

    # Strava: 2 valid rows landed, 1 drift skipped
    assert strava_result.rows_normalized == 2
    assert strava_result.drift_count == 1
    activity_ids = sorted(
        r["source_id"] for r in db_conn.execute(
            "SELECT source_id FROM fitness_activities "
            "WHERE user_id=1 AND source='strava'",
        ).fetchall()
    )
    assert activity_ids == ["1", "2"]

    # Garmin: 1 daily row, with the re-published sleep value
    assert garmin_result.rows_normalized == 1
    assert garmin_result.drift_count == 0
    daily = db_conn.execute(
        "SELECT sleep_score, sleep_duration_s "
        "FROM fitness_daily WHERE user_id=1 AND local_date='2026-04-15'",
    ).fetchone()
    assert daily["sleep_score"] == 90  # newer
    assert daily["sleep_duration_s"] == 28000

    # One drift notification for Strava, none for Garmin
    assert notifier.drift_calls == [("strava", 1)]


# 8. Strava avg_pace derived for foot activities, None elsewhere -----


def test_strava_avg_pace_set_for_run_none_for_ride(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    _insert_strava(repo, 1, "Run")
    _insert_strava(repo, 2, "Ride")
    normalize_strava(repo, user_id=1)

    rows = {
        r["activity_type"]: r["avg_pace_s_per_km"]
        for r in db_conn.execute(
            "SELECT activity_type, avg_pace_s_per_km "
            "FROM fitness_activities WHERE user_id=1",
        ).fetchall()
    }
    assert rows["run"] is not None
    assert rows["run"] == pytest.approx(1790 / 5.6124)
    assert rows["ride"] is None


# 9. Garmin activity normalize ---------------------------------------


def test_garmin_activity_normalize_writes_fitness_activity(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    repo.insert_raw(
        source="garmin", user_id=1,
        endpoint="activities", source_id="22000000001",
        payload_json=json.dumps(_garmin_activity_payload(22000000001)),
        sync_run_id=None,
    )

    result = normalize_garmin(repo, user_id=1)

    # rows_normalized counts both the (zero) daily rows and the activity
    assert result.rows_normalized == 1
    row = db_conn.execute(
        "SELECT activity_type, source_subtype, distance_m, duration_s, "
        "avg_pace_s_per_km FROM fitness_activities WHERE user_id=1",
    ).fetchone()
    assert row["activity_type"] == "run"
    assert row["source_subtype"] == "running"
    assert row["distance_m"] == pytest.approx(5612.4)
    assert row["duration_s"] == 1830
    assert row["avg_pace_s_per_km"] == pytest.approx(1790 / 5.6124)


# 10. Watermark advances incrementally -------------------------------


def test_watermark_skips_already_normalized_rows_on_resume(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    """First pass normalizes one row. Insert another raw row, run again.
    Second pass picks up only the new row."""
    _insert_strava(repo, 1, "Run")
    r1 = normalize_strava(repo, user_id=1)
    assert r1.rows_normalized == 1

    time.sleep(1.1)
    _insert_strava(repo, 2, "Ride")
    r2 = normalize_strava(repo, user_id=1)
    assert r2.rows_normalized == 1

    # Both end up in normalized table
    rows = db_conn.execute(
        "SELECT source_id FROM fitness_activities WHERE user_id=1 "
        "ORDER BY source_id",
    ).fetchall()
    assert [r["source_id"] for r in rows] == ["1", "2"]


# 11. NormalizeResult is dataclass-serialisable ----------------------


def test_normalize_result_dataclass_serialises_to_json() -> None:
    """Workers (W8) serialise via dataclasses.asdict."""
    import dataclasses
    result = NormalizeResult(
        source="strava", rows_normalized=12, drift_count=0,
    )
    d = dataclasses.asdict(result)
    assert d == {
        "source": "strava", "rows_normalized": 12, "drift_count": 0,
    }
    json.dumps(d)


# 12. Empty database — no-op success ---------------------------------


def test_normalize_empty_no_op(repo: FitnessRepository) -> None:
    notifier = _CapturingNotifier()
    r = normalize_strava(repo, user_id=1, notifier=notifier)
    assert r == NormalizeResult(
        source="strava", rows_normalized=0, drift_count=0,
    )
    assert notifier.drift_calls == []


# 13. Strava raw_ref_id points at the raw row ------------------------


def test_strava_raw_ref_id_points_at_raw_row(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    raw_id = _insert_strava(repo, 1, "Run")
    normalize_strava(repo, user_id=1)
    activity = db_conn.execute(
        "SELECT raw_ref_id FROM fitness_activities WHERE user_id=1",
    ).fetchone()
    assert activity["raw_ref_id"] == raw_id


# 14. Datetime parsing — Strava ISO with 'Z' suffix ------------------


def test_strava_start_time_normalized_to_z_suffix(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    """Strava sometimes serialises with '+00:00', sometimes 'Z'.
    Normalize emits canonical Z-suffix."""
    _insert_strava(repo, 1, "Run")
    normalize_strava(repo, user_id=1)
    row = db_conn.execute(
        "SELECT start_time FROM fitness_activities WHERE user_id=1",
    ).fetchone()
    # Parseable as UTC datetime
    dt = datetime.fromisoformat(row["start_time"].replace("Z", "+00:00"))
    assert dt.tzinfo == UTC
    assert row["start_time"].endswith("Z")


# 15. Garmin insufficient-wear sentinels are sanitized, not fatal -----
#
# Garmin publishes sentinel values (-1 / -2) for stress, sleep score,
# body battery, etc. on days with insufficient wear. Those violate the
# ``fitness_daily`` CHECK constraints (migration 0025). Before the W1
# fix, ``upsert_daily`` sat OUTSIDE the per-date drift guard, so the
# resulting sqlite3.IntegrityError aborted the whole daily pass (and the
# activity pass after it). The fix bounds-coerces sentinels to NULL and
# adds a defensive IntegrityError guard around the upsert.


def _sentinel_daily_fixture(**overrides: Any) -> dict[str, Any]:
    """Deep-copy ``_GARMIN_DAILY_FIXTURE`` and apply sentinel overrides.

    Recognized override keys map onto the nested Garmin payload shape:
    ``stress``, ``sleep_score``, ``resting_hr``, ``body_battery_charged``,
    ``training_readiness``.
    """
    fixture = copy.deepcopy(_GARMIN_DAILY_FIXTURE)
    if "stress" in overrides:
        fixture["stress"]["avgStressLevel"] = overrides["stress"]
    if "sleep_score" in overrides:
        fixture["sleep"]["dailySleepDTO"]["sleepScores"]["overall"]["value"] = (
            overrides["sleep_score"]
        )
    if "resting_hr" in overrides:
        fixture["sleep"]["restingHeartRate"] = overrides["resting_hr"]
    if "body_battery_charged" in overrides:
        fixture["body_battery"][0]["charged"] = overrides["body_battery_charged"]
    if "training_readiness" in overrides:
        fixture["training_readiness"][0]["score"] = overrides["training_readiness"]
    return fixture


def test_garmin_sentinel_batch_normalizes_all_days_not_fatal(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    """A batch of [clean, stress=-1 sentinel, clean] fully normalizes.

    All three daily rows land; the sentinel day gets ``stress_avg IS
    NULL`` (sanitized) while its other metrics survive; the clean days
    are intact; and ``drift_count == 0`` because a sanitized sentinel is
    not drift.
    """
    notifier = _CapturingNotifier()
    _insert_garmin_daily(repo, "2026-04-14")  # clean
    _insert_garmin_daily(
        repo, "2026-04-15", payloads=_sentinel_daily_fixture(stress=-1),
    )
    _insert_garmin_daily(repo, "2026-04-16")  # clean

    result = normalize_garmin(repo, user_id=1, notifier=notifier)

    assert result.rows_normalized == 3
    assert result.drift_count == 0
    assert notifier.drift_calls == []

    rows = {
        r["local_date"]: r
        for r in db_conn.execute(
            "SELECT * FROM fitness_daily WHERE user_id=1 ORDER BY local_date",
        ).fetchall()
    }
    assert set(rows) == {"2026-04-14", "2026-04-15", "2026-04-16"}
    # Sentinel day: stress sanitized to NULL, other metrics intact.
    assert rows["2026-04-15"]["stress_avg"] is None
    assert rows["2026-04-15"]["sleep_score"] == 84
    assert rows["2026-04-15"]["resting_hr_bpm"] == 51
    # Clean days unaffected.
    assert rows["2026-04-14"]["stress_avg"] == 31
    assert rows["2026-04-16"]["stress_avg"] == 31


@pytest.mark.parametrize(
    ("override", "column"),
    [
        ({"sleep_score": -1}, "sleep_score"),
        ({"body_battery_charged": -1}, "body_battery_high"),
        ({"resting_hr": -1}, "resting_hr_bpm"),
        ({"training_readiness": -1}, "training_readiness"),
    ],
)
def test_garmin_sentinel_field_becomes_null_row_persists(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
    override: dict[str, int], column: str,
) -> None:
    """Each out-of-range sentinel field is coerced to NULL and the row
    still persists (no IntegrityError, no drift)."""
    _insert_garmin_daily(
        repo, "2026-04-15", payloads=_sentinel_daily_fixture(**override),
    )

    result = normalize_garmin(repo, user_id=1)

    assert result.rows_normalized == 1
    assert result.drift_count == 0
    row = db_conn.execute(
        "SELECT * FROM fitness_daily WHERE user_id=1 AND local_date='2026-04-15'",
    ).fetchone()
    assert row is not None
    assert row[column] is None


def test_garmin_upsert_integrity_error_does_not_abort_activity_pass(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense in depth: even if ``upsert_daily`` raises an
    IntegrityError (e.g. a constraint we didn't bounds-check), the daily
    loop records it as drift and the subsequent activity pass still runs."""
    _insert_garmin_daily(repo, "2026-04-15")
    repo.insert_raw(
        source="garmin", user_id=1,
        endpoint="activities", source_id="22000000001",
        payload_json=json.dumps(_garmin_activity_payload(22000000001)),
        sync_run_id=None,
    )

    def _boom(_daily: Any) -> None:
        raise sqlite3.IntegrityError("CHECK constraint failed")

    monkeypatch.setattr(repo, "upsert_daily", _boom)

    result = normalize_garmin(repo, user_id=1)

    # The daily upsert failed → counted as drift, but the activity pass
    # still ran and persisted the activity.
    assert result.drift_count == 1
    activities = db_conn.execute(
        "SELECT source_id FROM fitness_activities WHERE user_id=1",
    ).fetchall()
    assert [r["source_id"] for r in activities] == ["22000000001"]


# ── W10: perceived_exertion + training-effect extras ──────────────────


def test_strava_perceived_exertion_maps_to_column(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    """Strava's manual RPE is already 1–10 → straight into the column."""
    payload = _strava_payload(31000000001)
    payload["perceived_exertion"] = 6
    repo.insert_raw(
        source="strava", user_id=1,
        endpoint="activities", source_id="31000000001",
        payload_json=json.dumps(payload, sort_keys=True),
        sync_run_id=None,
    )

    normalize_strava(repo, user_id=1)

    row = db_conn.execute(
        "SELECT perceived_exertion, extras_json FROM fitness_activities"
        " WHERE user_id=1",
    ).fetchone()
    assert row["perceived_exertion"] == 6
    assert json.loads(row["extras_json"]) == {}


def test_strava_suffer_score_goes_to_extras_not_column(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    """suffer_score is a different scale — it must NOT land in the 1–10
    column; it is captured in extras_json and perceived_exertion stays NULL."""
    payload = _strava_payload(31000000002)
    payload["suffer_score"] = 210  # well outside 1–10
    repo.insert_raw(
        source="strava", user_id=1,
        endpoint="activities", source_id="31000000002",
        payload_json=json.dumps(payload, sort_keys=True),
        sync_run_id=None,
    )

    normalize_strava(repo, user_id=1)

    row = db_conn.execute(
        "SELECT perceived_exertion, extras_json FROM fitness_activities"
        " WHERE user_id=1",
    ).fetchone()
    assert row["perceived_exertion"] is None
    assert json.loads(row["extras_json"]) == {"suffer_score": 210}


def test_strava_out_of_range_perceived_exertion_collapses_to_null(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    """A garbage RPE outside 1–10 is coerced to NULL, never a CHECK failure."""
    payload = _strava_payload(31000000003)
    payload["perceived_exertion"] = 42
    repo.insert_raw(
        source="strava", user_id=1,
        endpoint="activities", source_id="31000000003",
        payload_json=json.dumps(payload, sort_keys=True),
        sync_run_id=None,
    )

    result = normalize_strava(repo, user_id=1)

    assert result.drift_count == 0
    row = db_conn.execute(
        "SELECT perceived_exertion FROM fitness_activities WHERE user_id=1",
    ).fetchone()
    assert row["perceived_exertion"] is None


def test_garmin_training_effect_goes_to_extras_perceived_exertion_null(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    """Garmin has no manual RPE — we never invent one. Training-effect signals
    are captured verbatim in extras_json; perceived_exertion stays NULL."""
    payload = _garmin_activity_payload(32000000001)
    payload["aerobicTrainingEffect"] = 3.4
    payload["anaerobicTrainingEffect"] = 1.1
    payload["activityTrainingLoad"] = 156.0
    repo.insert_raw(
        source="garmin", user_id=1,
        endpoint="activities", source_id="32000000001",
        payload_json=json.dumps(payload),
        sync_run_id=None,
    )

    normalize_garmin(repo, user_id=1)

    row = db_conn.execute(
        "SELECT perceived_exertion, extras_json FROM fitness_activities"
        " WHERE user_id=1",
    ).fetchone()
    assert row["perceived_exertion"] is None
    assert json.loads(row["extras_json"]) == {
        "aerobicTrainingEffect": 3.4,
        "anaerobicTrainingEffect": 1.1,
        "activityTrainingLoad": 156.0,
    }


def test_garmin_activity_without_training_effect_has_empty_extras(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    """Absent training-effect keys → extras stays empty (capture-when-available)."""
    repo.insert_raw(
        source="garmin", user_id=1,
        endpoint="activities", source_id="32000000002",
        payload_json=json.dumps(_garmin_activity_payload(32000000002)),
        sync_run_id=None,
    )

    normalize_garmin(repo, user_id=1)

    row = db_conn.execute(
        "SELECT perceived_exertion, extras_json FROM fitness_activities"
        " WHERE user_id=1",
    ).fetchone()
    assert row["perceived_exertion"] is None
    assert json.loads(row["extras_json"]) == {}
