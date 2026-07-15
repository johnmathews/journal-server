"""Tests for the mood↔recovery divergence detector (fitness-schema.md §9).

All data is fully synthetic: the dev DB has 0 fitness rows, so every test
seeds ``fitness_daily`` (via ``FitnessRepository``) + ``entries`` +
``mood_scores`` (via raw SQL) directly.

Baselines are seeded as 10 days alternating ``base ± delta`` (5 low, 5
high), which gives an exact per-person mean ``base`` and population std
``delta``. A test day set to ``base + k*delta`` therefore has a clean raw
z of ``k`` — before orientation flips (resting HR, ACWR).
"""

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from journal.db.factory import ConnectionFactory
from journal.db.fitness_repository import FitnessRepository
from journal.db.migrations import run_migrations
from journal.models import FitnessDaily
from journal.services.fitness.divergence import (
    _rolling_z,
    compute_divergence,
    mood_recovery_rows,
)

_TEST_USER_ID = 1


# --------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------


@pytest.fixture
def factory(tmp_path: Path) -> ConnectionFactory:
    f = ConnectionFactory(tmp_path / "divergence.db")
    run_migrations(f.get())
    return f


@pytest.fixture
def db(factory: ConnectionFactory) -> sqlite3.Connection:
    return factory.get()


@pytest.fixture
def repo(factory: ConnectionFactory) -> FitnessRepository:
    return FitnessRepository(factory)


# --------------------------------------------------------------------
# Seeding helpers
# --------------------------------------------------------------------


def _seed_day(
    repo: FitnessRepository,
    local_date: str,
    *,
    hrv: float | None = None,
    resting_hr: int | None = None,
    sleep: int | None = None,
    readiness: int | None = None,
    acute: float | None = None,
    chronic: float | None = None,
) -> None:
    repo.upsert_daily(
        FitnessDaily(
            user_id=_TEST_USER_ID, source="garmin", local_date=local_date,
            hrv_overnight_ms=hrv, resting_hr_bpm=resting_hr, sleep_score=sleep,
            training_readiness=readiness, training_load_acute=acute,
            training_load_chronic=chronic, raw_ref_ids=[],
        ),
    )


def _seed_mood(
    db: sqlite3.Connection,
    entry_date: str,
    *,
    physical_fatigue: float | None = None,
    mental_fatigue: float | None = None,
) -> None:
    cur = db.execute(
        """
        INSERT INTO entries (user_id, entry_date, source_type, raw_text,
            final_text, word_count)
        VALUES (?, ?, 'voice', 't', 't', 1)
        """,
        (_TEST_USER_ID, entry_date),
    )
    entry_id = cur.lastrowid
    for dim, val in (
        ("physical_fatigue", physical_fatigue),
        ("mental_fatigue", mental_fatigue),
    ):
        if val is not None:
            db.execute(
                "INSERT INTO mood_scores (entry_id, dimension, score)"
                " VALUES (?, ?, ?)",
                (entry_id, dim, val),
            )
    db.commit()


def _days_before(day: str, n: int) -> list[str]:
    """``n`` calendar dates ending the day before ``day`` (ascending)."""
    d0 = date.fromisoformat(day)
    return [(d0 - timedelta(days=i)).isoformat() for i in range(n, 0, -1)]


def _alt(base: float, delta: float, i: int) -> float:
    """Alternate ``base - delta`` / ``base + delta`` by parity of ``i``."""
    return base - delta if i % 2 == 0 else base + delta


def _seed_recovery_baseline(
    repo: FitnessRepository,
    db: sqlite3.Connection,
    dates: list[str],
    *,
    hrv: tuple[float, float] | None = (55.0, 5.0),
    sleep: tuple[float, float] | None = (80.0, 5.0),
    phys: tuple[float, float] | None = (0.4, 0.1),
    ment: tuple[float, float] | None = (0.4, 0.1),
) -> None:
    """Seed hrv + sleep objective baseline and a fatigue-mood baseline."""
    for i, d in enumerate(dates):
        _seed_day(
            repo, d,
            hrv=_alt(*hrv, i) if hrv else None,
            sleep=int(_alt(*sleep, i)) if sleep else None,
        )
        _seed_mood(
            db, d,
            physical_fatigue=_alt(*phys, i) if phys else None,
            mental_fatigue=_alt(*ment, i) if ment else None,
        )


# --------------------------------------------------------------------
# Pure z-score helper
# --------------------------------------------------------------------


def test_rolling_z_exact_value() -> None:
    # base 55, std 5 (5×50 + 5×60). value 60 → z = +1.0; 65 → +2.0.
    baseline = [50.0] * 5 + [60.0] * 5
    assert _rolling_z(baseline, 60.0) == pytest.approx(1.0)
    assert _rolling_z(baseline, 65.0) == pytest.approx(2.0)
    assert _rolling_z(baseline, 45.0) == pytest.approx(-2.0)


def test_rolling_z_zero_variance_is_none() -> None:
    assert _rolling_z([55.0] * 10, 60.0) is None


def test_rolling_z_too_few_points_is_none() -> None:
    assert _rolling_z([1.0, 2.0, 3.0], 5.0) is None


def test_rolling_z_missing_value_is_none() -> None:
    assert _rolling_z([50.0] * 5 + [60.0] * 5, None) is None


# --------------------------------------------------------------------
# Baseline + outlier → correct z
# --------------------------------------------------------------------


def test_flat_baseline_plus_outlier_gives_correct_z(
    repo: FitnessRepository, db: sqlite3.Connection,
) -> None:
    testday = "2026-06-15"
    baseline = _days_before(testday, 10)
    # hrv baseline mean 55, std 5; test day 60 → z = +1.0 (no flip).
    for i, d in enumerate(baseline):
        _seed_day(repo, d, hrv=_alt(55.0, 5.0, i))
    _seed_day(repo, testday, hrv=60.0)

    days = compute_divergence(
        db, user_id=_TEST_USER_ID, start=testday, end=testday, window=28,
    )
    assert len(days) == 1
    assert days[0].hrv_z == pytest.approx(1.0)


def test_resting_hr_and_acwr_are_sign_flipped(
    repo: FitnessRepository, db: sqlite3.Connection,
) -> None:
    """A *lower* resting HR and a *lower* ACWR are better recovery, so
    their oriented z's flip sign relative to the raw value z."""
    testday = "2026-06-15"
    baseline = _days_before(testday, 10)
    for i, d in enumerate(baseline):
        # resting_hr mean 50 std 4; acwr mean 1.0 std 0.1 (chronic const 100).
        _seed_day(
            repo, d,
            resting_hr=int(_alt(50.0, 4.0, i)),
            acute=_alt(1.0, 0.1, i) * 100.0,
            chronic=100.0,
        )
    # Test day: resting HR two std BELOW norm (better) → +2 oriented.
    #           ACWR two std ABOVE norm (worse) → -2 oriented.
    _seed_day(repo, testday, resting_hr=42, acute=120.0, chronic=100.0)

    days = compute_divergence(
        db, user_id=_TEST_USER_ID, start=testday, end=testday, window=28,
    )
    row = days[0]
    assert row.resting_hr_z == pytest.approx(2.0)
    assert row.acwr == pytest.approx(1.2)
    assert row.acwr_z == pytest.approx(-2.0)


# --------------------------------------------------------------------
# Quadrant classification (one fixture per quadrant)
# --------------------------------------------------------------------


def _quadrant_day(
    repo: FitnessRepository,
    db: sqlite3.Connection,
    *,
    hrv: float,
    sleep: int,
    phys: float,
    ment: float,
) -> object:
    testday = "2026-06-15"
    baseline = _days_before(testday, 10)
    _seed_recovery_baseline(repo, db, baseline)
    _seed_day(repo, testday, hrv=hrv, sleep=sleep)
    _seed_mood(db, testday, physical_fatigue=phys, mental_fatigue=ment)
    days = compute_divergence(
        db, user_id=_TEST_USER_ID, start=testday, end=testday, window=28,
    )
    assert len(days) == 1
    return days[0]


def test_quadrant_likely_mental_fatigue(
    repo: FitnessRepository, db: sqlite3.Connection,
) -> None:
    # Tired (phys z=+2) but objectively recovered (hrv/sleep z=+1).
    row = _quadrant_day(repo, db, hrv=60.0, sleep=85, phys=0.6, ment=0.4)
    assert row.sufficient is True
    assert row.recovery_z == pytest.approx(1.0)
    assert row.subjective_tired_z == pytest.approx(2.0)
    assert row.quadrant == "likely_mental_fatigue"


def test_quadrant_hidden_physical_under_recovery(
    repo: FitnessRepository, db: sqlite3.Connection,
) -> None:
    # Fresh (fatigue z=-1) but objectively under-recovered (hrv/sleep z=-2).
    row = _quadrant_day(repo, db, hrv=45.0, sleep=70, phys=0.3, ment=0.3)
    assert row.sufficient is True
    assert row.recovery_z == pytest.approx(-2.0)
    assert row.subjective_tired_z == pytest.approx(-1.0)
    assert row.quadrant == "hidden_physical_under_recovery"


def test_quadrant_congruent_fatigue(
    repo: FitnessRepository, db: sqlite3.Connection,
) -> None:
    # Tired AND under-recovered → congruent.
    row = _quadrant_day(repo, db, hrv=45.0, sleep=70, phys=0.6, ment=0.4)
    assert row.quadrant == "congruent_fatigue"


def test_quadrant_congruent_ok(
    repo: FitnessRepository, db: sqlite3.Connection,
) -> None:
    # Fresh AND recovered → congruent.
    row = _quadrant_day(repo, db, hrv=60.0, sleep=85, phys=0.3, ment=0.3)
    assert row.quadrant == "congruent_ok"


# --------------------------------------------------------------------
# Sufficiency / availability edge cases
# --------------------------------------------------------------------


def test_fewer_than_two_signals_is_insufficient_no_quadrant(
    repo: FitnessRepository, db: sqlite3.Connection,
) -> None:
    testday = "2026-06-15"
    baseline = _days_before(testday, 10)
    # Only ONE objective signal (hrv) has a baseline + a value.
    for i, d in enumerate(baseline):
        _seed_day(repo, d, hrv=_alt(55.0, 5.0, i))
        _seed_mood(db, d, physical_fatigue=_alt(0.4, 0.1, i))
    _seed_day(repo, testday, hrv=60.0)
    _seed_mood(db, testday, physical_fatigue=0.6)

    days = compute_divergence(
        db, user_id=_TEST_USER_ID, start=testday, end=testday, window=28,
    )
    row = days[0]
    assert row.n_signals == 1
    assert row.recovery_z is None
    assert row.sufficient is False
    assert row.quadrant is None


def test_acwr_spike_drags_recovery_negative(
    repo: FitnessRepository, db: sqlite3.Connection,
) -> None:
    testday = "2026-06-15"
    baseline = _days_before(testday, 10)
    for i, d in enumerate(baseline):
        _seed_day(
            repo, d,
            hrv=_alt(55.0, 5.0, i),
            acute=_alt(1.0, 0.1, i) * 100.0,
            chronic=100.0,
        )
        _seed_mood(db, d, physical_fatigue=_alt(0.4, 0.1, i))
    # hrv +1 (recovered) but an acute:chronic spike (1.3, raw z=+3 → -3).
    _seed_day(repo, testday, hrv=60.0, acute=130.0, chronic=100.0)
    _seed_mood(db, testday, physical_fatigue=0.4)

    days = compute_divergence(
        db, user_id=_TEST_USER_ID, start=testday, end=testday, window=28,
    )
    row = days[0]
    assert row.hrv_z == pytest.approx(1.0)
    assert row.acwr_z == pytest.approx(-3.0)
    # mean(+1, -3) = -1.0 → the spike pulls the composite negative.
    assert row.recovery_z == pytest.approx(-1.0)


def test_fewer_than_ten_baseline_points_flags_signal_unavailable(
    repo: FitnessRepository, db: sqlite3.Connection,
) -> None:
    testday = "2026-06-15"
    baseline = _days_before(testday, 9)  # one short of the 10-point floor
    for i, d in enumerate(baseline):
        _seed_day(repo, d, hrv=_alt(55.0, 5.0, i))
    _seed_day(repo, testday, hrv=60.0)

    days = compute_divergence(
        db, user_id=_TEST_USER_ID, start=testday, end=testday, window=28,
    )
    row = days[0]
    assert row.hrv_z is None
    assert row.n_signals == 0
    assert row.sufficient is False


def test_empty_db_returns_empty_list(db: sqlite3.Connection) -> None:
    days = compute_divergence(
        db, user_id=_TEST_USER_ID, start="2026-06-01", end="2026-06-30",
        window=28,
    )
    assert days == []


def test_reversed_range_returns_empty_list(db: sqlite3.Connection) -> None:
    days = compute_divergence(
        db, user_id=_TEST_USER_ID, start="2026-06-30", end="2026-06-01",
    )
    assert days == []


# --------------------------------------------------------------------
# mood_recovery_rows (overlay endpoint helper)
# --------------------------------------------------------------------


def test_mood_recovery_rows_date_aligned_nullable(
    repo: FitnessRepository, db: sqlite3.Connection,
) -> None:
    # One day with fitness only, one with mood only, one with both.
    _seed_day(repo, "2026-06-01", hrv=55.0, acute=100.0, readiness=70)
    _seed_mood(db, "2026-06-02", physical_fatigue=0.5, mental_fatigue=0.3)
    _seed_day(repo, "2026-06-03", hrv=60.0, acute=120.0, readiness=65)
    _seed_mood(db, "2026-06-03", physical_fatigue=0.7, mental_fatigue=0.6)

    rows = mood_recovery_rows(
        db, user_id=_TEST_USER_ID, start="2026-06-01", end="2026-06-30",
    )
    by_date = {r["local_date"]: r for r in rows}
    assert set(by_date) == {"2026-06-01", "2026-06-02", "2026-06-03"}
    assert by_date["2026-06-01"]["hrv_overnight_ms"] == pytest.approx(55.0)
    assert by_date["2026-06-01"]["physical_fatigue"] is None
    assert by_date["2026-06-02"]["training_load_acute"] is None
    assert by_date["2026-06-02"]["physical_fatigue"] == pytest.approx(0.5)
    assert by_date["2026-06-03"]["training_readiness"] == pytest.approx(65.0)
    assert by_date["2026-06-03"]["mental_fatigue"] == pytest.approx(0.6)
