-- Fitness pipeline — raw payload archive (Strava + Garmin).
--
-- Second of three fitness migrations. Per fitness-integration-plan.md D3
-- ("sacred raw archive"), these tables are append-only — new fetches that
-- match an existing (user_id, …, payload_sha256) tuple are no-ops via
-- INSERT OR IGNORE, while a changed payload (different sha256) inserts a
-- new row, preserving history. No UPDATEs by callers, ever.
--
-- Schema content copied verbatim from fitness-schema.md §2. The Garmin
-- source_id convention (ISO date for daily endpoints, activity id for
-- per-activity endpoints) is documented there.
--
-- The sync_run_id FK back into fitness_sync_runs (created in 0023) uses
-- ON DELETE SET NULL: pruning old sync rows must not delete raw data.
-- See fitness-schema.md §6 for the full cascade rationale.

CREATE TABLE IF NOT EXISTS fitness_raw_strava (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    source          TEXT    NOT NULL DEFAULT 'strava' CHECK(source = 'strava'),
    source_id       TEXT    NOT NULL,
    endpoint        TEXT    NOT NULL CHECK(endpoint IN
                        ('activities', 'activity_detail', 'athlete')),
    fetched_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    payload_json    TEXT    NOT NULL,
    payload_sha256  TEXT    NOT NULL,
    sync_run_id     INTEGER REFERENCES fitness_sync_runs(id) ON DELETE SET NULL,
    UNIQUE(user_id, source_id, endpoint, payload_sha256)
);

CREATE INDEX IF NOT EXISTS idx_fit_raw_strava_user_fetched
    ON fitness_raw_strava(user_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_fit_raw_strava_source_id
    ON fitness_raw_strava(user_id, source_id);

CREATE TABLE IF NOT EXISTS fitness_raw_garmin (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    source          TEXT    NOT NULL DEFAULT 'garmin' CHECK(source = 'garmin'),
    endpoint        TEXT    NOT NULL CHECK(endpoint IN (
                        'sleep', 'hrv', 'body_battery', 'training_load',
                        'training_readiness', 'stress', 'activities', 'activity_detail'
                    )),
    source_id       TEXT    NOT NULL,
    fetched_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    payload_json    TEXT    NOT NULL,
    payload_sha256  TEXT    NOT NULL,
    sync_run_id     INTEGER REFERENCES fitness_sync_runs(id) ON DELETE SET NULL,
    UNIQUE(user_id, endpoint, source_id, payload_sha256)
);

CREATE INDEX IF NOT EXISTS idx_fit_raw_garmin_user_fetched
    ON fitness_raw_garmin(user_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_fit_raw_garmin_endpoint_key
    ON fitness_raw_garmin(user_id, endpoint, source_id);
