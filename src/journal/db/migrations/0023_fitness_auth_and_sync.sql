-- Fitness pipeline — auth state + sync run history.
--
-- First of three fitness migrations (0023 → 0024 → 0025); see
-- docs/fitness-tier-plan.md and docs/fitness-schema.md for the design.
-- This file ships first because both downstream migrations reference
-- fitness_sync_runs.id (raw rows carry a sync_run_id FK), and because
-- the CLI re-auth flow needs auth state before any fetch can run.
--
-- Schema content here is copied verbatim from fitness-schema.md §4.
-- Decisions (single-user posture, ON DELETE CASCADE from users, app-managed
-- updated_at, alerting taxonomy) are documented in fitness-integration-plan.md
-- (master) and the schema doc; do not relitigate here.

CREATE TABLE IF NOT EXISTS fitness_auth_state (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source                      TEXT    NOT NULL CHECK(source IN ('strava', 'garmin')),
    access_token                TEXT,
    refresh_token               TEXT,
    token_expires_at            TEXT,
    extra_state_json            TEXT NOT NULL DEFAULT '{}',
    last_successful_login_at    TEXT,
    last_refresh_at             TEXT,
    auth_status                 TEXT NOT NULL DEFAULT 'unknown'
                                CHECK(auth_status IN ('unknown', 'ok', 'broken')),
    auth_broken_since           TEXT,
    created_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(user_id, source)
);

CREATE TABLE IF NOT EXISTS fitness_sync_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source          TEXT    NOT NULL CHECK(source IN ('strava', 'garmin')),
    started_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    finished_at     TEXT,
    status          TEXT    NOT NULL CHECK(status IN
                        ('running', 'success', 'auth_broken', 'transient_failure',
                         'normalize_drift')),
    error_class     TEXT,
    error_message   TEXT,
    rows_fetched    INTEGER NOT NULL DEFAULT 0,
    rows_normalized INTEGER NOT NULL DEFAULT 0,
    notes_json      TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_fit_sync_user_started
    ON fitness_sync_runs(user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_fit_sync_user_source_started
    ON fitness_sync_runs(user_id, source, started_at DESC);
