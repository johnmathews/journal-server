"""Mood↔recovery divergence detector (fitness-schema.md §9).

The product question this answers: *"Am I tired because I run, or is it
mental?"* Per day it compares **self-reported tiredness** (the
``physical_fatigue`` / ``mental_fatigue`` mood facets) against
**baselined objective recovery signals** from ``fitness_daily`` and
sorts each day into a quadrant.

Everything here is per-person and baseline-relative. A raw HRV of 55 ms
means nothing without knowing *your* normal HRV; so every signal is
turned into a **rolling z-score** — the value on day *D* versus the mean
and standard deviation of that same signal over the trailing ``window``
calendar days *before* D. The z's are then **oriented so positive =
better recovered** (resting HR and the acute:chronic load ratio are
sign-flipped, since lower is better for both), averaged into a single
``recovery_z``, and compared against the subjective tiredness z.

No numpy/scipy on purpose (see ``correlation_stats.py`` for the same
rationale): personal-scale windows (≤~365 points) make a plain two-pass
Python computation more than fast enough.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from journal.models import DivergenceDay

if TYPE_CHECKING:
    import sqlite3

# A per-signal rolling baseline needs at least this many non-null points
# before its z-score is trusted. Below it, the signal is marked
# unavailable for the day rather than producing a noisy z off 2-3 points.
MIN_BASELINE_POINTS = 10


def _rolling_z(
    baseline: list[float], value: float | None, *,
    min_points: int = MIN_BASELINE_POINTS,
) -> float | None:
    """Population z-score of ``value`` against ``baseline``.

    Returns ``None`` when ``value`` is missing, the baseline has fewer
    than ``min_points`` points, or the baseline has zero variance (a
    constant series has no scale to normalise against).
    """
    if value is None:
        return None
    if len(baseline) < min_points:
        return None
    n = len(baseline)
    mean = sum(baseline) / n
    var = sum((x - mean) ** 2 for x in baseline) / n
    if var == 0.0:
        return None
    return (value - mean) / var**0.5


def _daterange(start: date, end: date) -> list[date]:
    """Inclusive list of calendar dates from ``start`` to ``end``."""
    out: list[date] = []
    d = start
    while d <= end:
        out.append(d)
        d += timedelta(days=1)
    return out


def _trailing(series: dict[str, float], d: date, window: int) -> list[float]:
    """Non-null values of ``series`` on the ``window`` days *before* ``d``."""
    out: list[float] = []
    for i in range(1, window + 1):
        key = (d - timedelta(days=i)).isoformat()
        v = series.get(key)
        if v is not None:
            out.append(v)
    return out


def _load_objective(
    conn: sqlite3.Connection, *, user_id: int, start: str, end: str,
) -> dict[str, dict[str, float]]:
    """Per-date objective signal values over ``[start, end]``.

    Collapses multiple sources per day with ``AVG`` (NULL-ignoring), the
    same posture Q3 uses. ``acwr`` is the acute:chronic training-load
    ratio; ``NULLIF`` guards the divide-by-zero on a zero chronic load.
    """
    rows = conn.execute(
        """
        SELECT
            local_date,
            AVG(hrv_overnight_ms)   AS hrv,
            AVG(resting_hr_bpm)     AS resting_hr,
            AVG(sleep_score)        AS sleep,
            AVG(training_readiness) AS readiness,
            AVG(training_load_acute) / NULLIF(AVG(training_load_chronic), 0)
                                    AS acwr
        FROM fitness_daily
        WHERE user_id = :uid AND local_date BETWEEN :start AND :end
        GROUP BY local_date
        """,
        {"uid": user_id, "start": start, "end": end},
    ).fetchall()
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        out[r["local_date"]] = {
            "hrv": r["hrv"],
            "resting_hr": r["resting_hr"],
            "sleep": r["sleep"],
            "readiness": r["readiness"],
            "acwr": r["acwr"],
        }
    return out


def _load_subjective(
    conn: sqlite3.Connection, *, user_id: int, start: str, end: str,
) -> dict[str, dict[str, float]]:
    """Per-date physical/mental fatigue mood scores over ``[start, end]``."""
    rows = conn.execute(
        """
        SELECT
            e.entry_date AS d,
            AVG(CASE WHEN ms.dimension = 'physical_fatigue' THEN ms.score END)
                AS physical_fatigue,
            AVG(CASE WHEN ms.dimension = 'mental_fatigue'   THEN ms.score END)
                AS mental_fatigue
        FROM entries e
        JOIN mood_scores ms ON ms.entry_id = e.id
        WHERE e.user_id = :uid AND e.entry_date BETWEEN :start AND :end
        GROUP BY e.entry_date
        """,
        {"uid": user_id, "start": start, "end": end},
    ).fetchall()
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        out[r["d"]] = {
            "physical_fatigue": r["physical_fatigue"],
            "mental_fatigue": r["mental_fatigue"],
        }
    return out


def _series(
    by_date: dict[str, dict[str, float]], key: str,
) -> dict[str, float]:
    """Flatten ``{date: {signal: value}}`` to ``{date: value}`` for one signal."""
    return {d: vals[key] for d, vals in by_date.items()}


def _classify(
    *, feels_tired: bool, recovery_z: float, z_threshold: float,
) -> str:
    """Quadrant label from the tired axis and the recovery axis.

    The two *divergence* quadrants demand a clear opposite signal: a
    tired day is only ``likely_mental_fatigue`` when objectively fully
    recovered (``recovery_z >= 0``); a fresh day is only
    ``hidden_physical_under_recovery`` when clearly under-recovered
    (``recovery_z <= -z_threshold``). The ambiguous middle collapses into
    the congruent buckets.
    """
    if feels_tired:
        if recovery_z >= 0.0:
            return "likely_mental_fatigue"
        return "congruent_fatigue"
    if recovery_z <= -z_threshold:
        return "hidden_physical_under_recovery"
    return "congruent_ok"


def compute_divergence(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    start: str,
    end: str,
    window: int = 28,
    z_threshold: float = 1.0,
) -> list[DivergenceDay]:
    """Classify each day in ``[start, end]`` into a mood↔recovery quadrant.

    ``window`` days of history *before* ``start`` are pulled internally so
    even the first day in the range has a rolling baseline. A day is
    emitted only when it carries at least one objective signal value or a
    fatigue mood score — days with no data at all are skipped (so an empty
    DB yields ``[]``).

    See the module docstring and :class:`DivergenceDay` for the z-score,
    orientation, and quadrant semantics.
    """
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    if end_d < start_d:
        return []
    history_start = (start_d - timedelta(days=window)).isoformat()

    objective = _load_objective(
        conn, user_id=user_id, start=history_start, end=end,
    )
    subjective = _load_subjective(
        conn, user_id=user_id, start=history_start, end=end,
    )
    hrv_s = _series(objective, "hrv")
    rhr_s = _series(objective, "resting_hr")
    sleep_s = _series(objective, "sleep")
    readiness_s = _series(objective, "readiness")
    acwr_s = _series(objective, "acwr")
    phys_s = _series(subjective, "physical_fatigue")
    ment_s = _series(subjective, "mental_fatigue")

    results: list[DivergenceDay] = []
    for d in _daterange(start_d, end_d):
        key = d.isoformat()
        obj = objective.get(key, {})
        subj = subjective.get(key, {})
        has_obj = any(obj.get(k) is not None for k in obj)
        phys_val = subj.get("physical_fatigue")
        ment_val = subj.get("mental_fatigue")
        if not has_obj and phys_val is None and ment_val is None:
            continue

        # Oriented per-signal z's: positive = better recovered.
        hrv_z = _rolling_z(_trailing(hrv_s, d, window), obj.get("hrv"))
        rhr_raw = _rolling_z(_trailing(rhr_s, d, window), obj.get("resting_hr"))
        resting_hr_z = None if rhr_raw is None else -rhr_raw  # lower = better
        sleep_z = _rolling_z(_trailing(sleep_s, d, window), obj.get("sleep"))
        readiness_z = _rolling_z(
            _trailing(readiness_s, d, window), obj.get("readiness"),
        )
        acwr_raw = _rolling_z(_trailing(acwr_s, d, window), obj.get("acwr"))
        acwr_z = None if acwr_raw is None else -acwr_raw  # higher ratio = worse

        signals = [
            z for z in (hrv_z, resting_hr_z, sleep_z, readiness_z, acwr_z)
            if z is not None
        ]
        n_signals = len(signals)
        recovery_z = sum(signals) / n_signals if n_signals >= 2 else None

        # Subjective side: positive = more tired than personal norm.
        phys_z = _rolling_z(_trailing(phys_s, d, window), phys_val)
        ment_z = _rolling_z(_trailing(ment_s, d, window), ment_val)
        subj_zs = [z for z in (phys_z, ment_z) if z is not None]
        subjective_tired_z = max(subj_zs) if subj_zs else None

        sufficient = recovery_z is not None and subjective_tired_z is not None
        quadrant = (
            _classify(
                feels_tired=subjective_tired_z >= z_threshold,
                recovery_z=recovery_z,
                z_threshold=z_threshold,
            )
            if sufficient
            else None
        )

        results.append(
            DivergenceDay(
                local_date=key,
                subjective_tired_z=subjective_tired_z,
                physical_fatigue=phys_val,
                mental_fatigue=ment_val,
                recovery_z=recovery_z,
                hrv_z=hrv_z,
                resting_hr_z=resting_hr_z,
                sleep_z=sleep_z,
                readiness_z=readiness_z,
                acwr=obj.get("acwr"),
                acwr_z=acwr_z,
                quadrant=quadrant,
                n_signals=n_signals,
                sufficient=sufficient,
            ),
        )
    return results


def mood_recovery_rows(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    """Date-aligned raw training-load / recovery / fatigue rows.

    Powers the webapp overlay: one row per calendar date in ``[start,
    end]`` that has either a ``fitness_daily`` row or a fatigue mood
    score, with the raw (non-z) values the overlay plots. Any field is
    nullable — a date can have fitness data but no journal entry, or vice
    versa. Sorted ascending by date.
    """
    # The overlay wants the raw acute load + readiness + HRV (not z's), so
    # this is a direct AVG-per-date read rather than the baselined pull.
    obj_raw = conn.execute(
        """
        SELECT
            local_date,
            AVG(training_load_acute) AS training_load_acute,
            AVG(training_readiness)  AS training_readiness,
            AVG(hrv_overnight_ms)    AS hrv_overnight_ms
        FROM fitness_daily
        WHERE user_id = :uid AND local_date BETWEEN :start AND :end
        GROUP BY local_date
        """,
        {"uid": user_id, "start": start, "end": end},
    ).fetchall()
    obj_by_date = {r["local_date"]: r for r in obj_raw}
    subjective = _load_subjective(conn, user_id=user_id, start=start, end=end)

    dates = sorted(set(obj_by_date) | set(subjective))
    rows: list[dict[str, Any]] = []
    for d in dates:
        o = obj_by_date.get(d)
        s = subjective.get(d, {})
        rows.append(
            {
                "local_date": d,
                "training_load_acute": o["training_load_acute"] if o else None,
                "training_readiness": o["training_readiness"] if o else None,
                "hrv_overnight_ms": o["hrv_overnight_ms"] if o else None,
                "physical_fatigue": s.get("physical_fatigue"),
                "mental_fatigue": s.get("mental_fatigue"),
            },
        )
    return rows
