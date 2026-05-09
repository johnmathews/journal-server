-- Fitness pipeline — normalized layer (activities + daily rollups).
--
-- Third of three fitness migrations. Single fitness_activities table
-- covers both Strava and Garmin (S1 in fitness-schema.md §1) — the
-- integrate layer wants "all runs in week W", not "Strava runs". Per-source
-- raw tables (S5) feed in via the soft (FK-less) raw_ref_id pointer (S4).
--
-- Schema content copied verbatim from fitness-schema.md §3. Activity-type
-- collapsing rules (Strava sport_type → coarse 7-value enum) and the
-- authoritativeness rule (largest fetched_at wins on a re-published Garmin
-- daily) are documented there and enforced by the normalize service.

CREATE TABLE IF NOT EXISTS fitness_activities (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id),
    source              TEXT    NOT NULL CHECK(source IN ('strava', 'garmin')),
    source_id           TEXT    NOT NULL,
    activity_type       TEXT    NOT NULL CHECK(activity_type IN (
                            'run', 'ride', 'swim', 'walk', 'hike', 'strength', 'other'
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

CREATE INDEX IF NOT EXISTS idx_fit_act_user_date
    ON fitness_activities(user_id, local_date);
CREATE INDEX IF NOT EXISTS idx_fit_act_user_type_date
    ON fitness_activities(user_id, activity_type, local_date);
CREATE INDEX IF NOT EXISTS idx_fit_act_start
    ON fitness_activities(user_id, start_time);

CREATE TABLE IF NOT EXISTS fitness_daily (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                  INTEGER NOT NULL REFERENCES users(id),
    source                   TEXT    NOT NULL CHECK(source IN ('strava', 'garmin')),
    local_date               TEXT    NOT NULL,
    sleep_score              INTEGER CHECK(sleep_score IS NULL OR sleep_score BETWEEN 0 AND 100),
    sleep_duration_s         INTEGER CHECK(sleep_duration_s IS NULL OR sleep_duration_s >= 0),
    sleep_efficiency_pct     REAL    CHECK(sleep_efficiency_pct IS NULL OR sleep_efficiency_pct BETWEEN 0 AND 100),
    hrv_overnight_ms         REAL    CHECK(hrv_overnight_ms IS NULL OR hrv_overnight_ms > 0),
    resting_hr_bpm           INTEGER CHECK(resting_hr_bpm IS NULL OR resting_hr_bpm BETWEEN 20 AND 200),
    body_battery_high        INTEGER CHECK(body_battery_high IS NULL OR body_battery_high BETWEEN 0 AND 100),
    body_battery_low         INTEGER CHECK(body_battery_low IS NULL OR body_battery_low BETWEEN 0 AND 100),
    stress_avg               INTEGER CHECK(stress_avg IS NULL OR stress_avg BETWEEN 0 AND 100),
    training_load_acute      REAL    CHECK(training_load_acute IS NULL OR training_load_acute >= 0),
    training_load_chronic    REAL    CHECK(training_load_chronic IS NULL OR training_load_chronic >= 0),
    training_readiness       INTEGER CHECK(training_readiness IS NULL OR training_readiness BETWEEN 0 AND 100),
    extras_json              TEXT    NOT NULL DEFAULT '{}',
    raw_ref_ids_json         TEXT    NOT NULL DEFAULT '[]',
    normalized_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(user_id, source, local_date)
);

CREATE INDEX IF NOT EXISTS idx_fit_daily_user_date
    ON fitness_daily(user_id, local_date);
