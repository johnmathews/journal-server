"""Source-specific activity-type → coarse ``activity_type`` mapping.

The coarse enum (``run`` / ``ride`` / ``swim`` / ``walk`` / ``hike`` /
``row`` / ``strength`` / ``other``) is what
``fitness_activities.activity_type`` stores and what the locked-in
correlation queries (`fitness-schema.md` §8 Q2) join on. The verbatim
source enum lives in ``fitness_activities.source_subtype`` so future
queries that need finer-grained filtering (e.g. trail-runs only,
on-water vs ergometer rowing) can do that without a schema change.

**``row`` was added 2026-06-04** alongside this comment (W5 of the
fitness multi-user final-mile plan) once rowing became a regular part
of the dataset. Before that, every Strava ``Rowing`` row collapsed to
``other``; the W5 migration backfills those rows into ``row``.

**Source of truth** for the Strava mapping is `fitness-schema.md` §3.
If this table and the schema doc diverge, the schema doc wins —
``test_strava_activity_type_mapping`` in
``tests/test_services/test_fitness/test_normalize.py`` parametrises
the §3 table and will fail on drift. Garmin's enum is less stable so
its mapping is maintained here in code; new typeKey strings
encountered in production fall back to ``other`` and surface as
`source_subtype` values that ops can review.
"""

from __future__ import annotations

from typing import Final, Literal

FitnessActivityType = Literal[
    "run", "ride", "swim", "walk", "hike", "row", "strength", "other",
]


_STRAVA: Final[dict[str, FitnessActivityType]] = {
    # run
    "Run": "run",
    "TrailRun": "run",
    "VirtualRun": "run",
    # ride
    "Ride": "ride",
    "GravelRide": "ride",
    "MountainBikeRide": "ride",
    "EBikeRide": "ride",
    "EMountainBikeRide": "ride",
    "VirtualRide": "ride",
    # swim
    "Swim": "swim",
    # walk
    "Walk": "walk",
    # hike
    "Hike": "hike",
    # row
    "Rowing": "row",
    # strength
    "WeightTraining": "strength",
    "Crossfit": "strength",
    "HighIntensityIntervalTraining": "strength",
}


_GARMIN: Final[dict[str, FitnessActivityType]] = {
    # run
    "running": "run",
    "treadmill_running": "run",
    "track_running": "run",
    "trail_running": "run",
    "indoor_running": "run",
    "virtual_run": "run",
    # ride
    "cycling": "ride",
    "indoor_cycling": "ride",
    "mountain_biking": "ride",
    "gravel_cycling": "ride",
    "road_biking": "ride",
    "virtual_ride": "ride",
    # swim
    "swimming": "swim",
    "lap_swimming": "swim",
    "open_water_swimming": "swim",
    # walk
    "walking": "walk",
    "casual_walking": "walk",
    "speed_walking": "walk",
    # hike
    "hiking": "hike",
    # row
    "rowing": "row",
    "indoor_rowing": "row",
    # strength
    "strength_training": "strength",
    "indoor_cardio": "strength",
    "hiit": "strength",
    "crossfit": "strength",
}


def coarse_strava(sport_type: str) -> FitnessActivityType:
    """Map a Strava ``sport_type`` to the coarse ``FitnessActivityType``.

    Anything not in the §3 table (Yoga, AlpineSki, RockClimbing,
    NordicSki, Pilates, Kayaking, StairStepper, etc.) falls through to
    ``"other"``. The verbatim ``sport_type`` is preserved as
    ``source_subtype`` for downstream queries.
    """
    return _STRAVA.get(sport_type, "other")


def coarse_garmin(activity_type_str: str) -> FitnessActivityType:
    """Map a Garmin ``typeKey`` to the coarse ``FitnessActivityType``.

    Unknown typeKey strings fall through to ``"other"``. Garmin's
    activity taxonomy is less stable than Strava's, so new strings
    encountered in production should be added here once observed.
    """
    return _GARMIN.get(activity_type_str, "other")
