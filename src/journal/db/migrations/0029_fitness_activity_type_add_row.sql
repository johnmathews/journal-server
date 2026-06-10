-- W5 of the fitness multi-user final-mile plan: extend the
-- ``fitness_activities.activity_type`` CHECK constraint with a new
-- ``'row'`` value, and backfill existing rows whose ``source_subtype``
-- identifies them as rowing.
--
-- Before this migration the canonical activity_type enum was seven
-- values (run / ride / swim / walk / hike / strength / other); rowing
-- collapsed to ``'other'`` per `_activity_type_map._STRAVA`'s default
-- fall-through. After this migration rowing is a first-class type,
-- powering the activity-type filter on the webapp's /fitness view and
-- (in time) the weekly-volume + correlation queries documented in
-- ``fitness-schema.md`` §8.
--
-- SQLite cannot relax a CHECK constraint in place, so the table is
-- rebuilt: create-new / copy-with-backfill / drop-old / rename. Every
-- non-CHECK column definition (NOT NULL, defaults, FKs, the other
-- CHECK constraints, the UNIQUE clause) is copied verbatim from
-- migration 0025; only the ``activity_type`` CHECK list grows.
--
-- Re-runnability. The whole rebuild is wrapped in BEGIN / COMMIT; the
-- migration runner's ``_executescript_idempotent`` rolls back any
-- dangling transaction on failure. Crashes anywhere inside the
-- migration leave the DB in the pre-migration state, so the next run
-- restarts cleanly. ``DROP TABLE IF EXISTS fitness_activities_new``
-- at the top is belt-and-braces for any odd state left by a partial
-- ad-hoc run.

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

DROP TABLE IF EXISTS fitness_activities_new;

CREATE TABLE fitness_activities_new (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id),
    source              TEXT    NOT NULL CHECK(source IN ('strava', 'garmin')),
    source_id           TEXT    NOT NULL,
    activity_type       TEXT    NOT NULL CHECK(activity_type IN (
                            'run', 'ride', 'swim', 'walk', 'hike',
                            'row', 'strength', 'other'
                        )),
    source_subtype      TEXT    NOT NULL,
    start_time          TEXT    NOT NULL,
    local_date          TEXT    NOT NULL,
    duration_s          INTEGER NOT NULL CHECK(duration_s >= 0),
    moving_time_s       INTEGER CHECK(moving_time_s IS NULL OR moving_time_s >= 0),
    distance_m          REAL    CHECK(distance_m IS NULL OR distance_m >= 0),
    elevation_gain_m    REAL    CHECK(elevation_gain_m IS NULL OR elevation_gain_m >= 0),
    avg_hr_bpm          INTEGER CHECK(avg_hr_bpm IS NULL OR avg_hr_bpm BETWEEN 20 AND 250),
    max_hr_bpm          INTEGER CHECK(max_hr_bpm IS NULL OR max_hr_bpm BETWEEN 20 AND 250),
    avg_pace_s_per_km   REAL    CHECK(avg_pace_s_per_km IS NULL OR avg_pace_s_per_km > 0),
    calories_kcal       INTEGER CHECK(calories_kcal IS NULL OR calories_kcal >= 0),
    perceived_exertion  INTEGER CHECK(perceived_exertion IS NULL OR perceived_exertion BETWEEN 1 AND 10),
    extras_json         TEXT    NOT NULL DEFAULT '{}',
    raw_ref_id          INTEGER NOT NULL,
    normalized_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(user_id, source, source_id)
);

-- Copy data; backfill rowing rows from 'other' → 'row'. The verbatim
-- match on source_subtype matches `_activity_type_map._STRAVA` and
-- `_GARMIN` exactly, so the post-migration state agrees with what the
-- normalize service would produce for a fresh fetch of the same raws.
INSERT INTO fitness_activities_new (
    id, user_id, source, source_id, activity_type, source_subtype,
    start_time, local_date, duration_s, moving_time_s, distance_m,
    elevation_gain_m, avg_hr_bpm, max_hr_bpm, avg_pace_s_per_km,
    calories_kcal, perceived_exertion, extras_json, raw_ref_id,
    normalized_at
)
SELECT
    id, user_id, source, source_id,
    CASE
        WHEN source = 'strava' AND source_subtype = 'Rowing' THEN 'row'
        WHEN source = 'garmin' AND source_subtype IN (
            'rowing', 'indoor_rowing'
        ) THEN 'row'
        ELSE activity_type
    END AS activity_type,
    source_subtype,
    start_time, local_date, duration_s, moving_time_s, distance_m,
    elevation_gain_m, avg_hr_bpm, max_hr_bpm, avg_pace_s_per_km,
    calories_kcal, perceived_exertion, extras_json, raw_ref_id,
    normalized_at
FROM fitness_activities;

DROP TABLE fitness_activities;
ALTER TABLE fitness_activities_new RENAME TO fitness_activities;

-- Recreate indexes verbatim from migration 0025.
CREATE INDEX IF NOT EXISTS idx_fit_act_user_date
    ON fitness_activities(user_id, local_date);
CREATE INDEX IF NOT EXISTS idx_fit_act_user_type_date
    ON fitness_activities(user_id, activity_type, local_date);
CREATE INDEX IF NOT EXISTS idx_fit_act_start
    ON fitness_activities(user_id, start_time);

COMMIT;

PRAGMA foreign_keys = ON;
