-- T7 (fitness-tile-layout-plan): split the pooled rows_fetched / rows_normalized
-- counters on fitness_sync_runs into workouts vs. wellness buckets. The Garmin
-- "Fetched" column in the webapp's Recent runs panel pooled both buckets, which
-- obscured what each sync actually pulled. The new columns let the UI display
-- the split (Strava is workouts-only so its wellness counts are always 0).
--
-- Additive change — existing rows get default 0 for the new columns. The
-- legacy rows_fetched / rows_normalized columns stay populated as the sum of
-- the two buckets, so any code path that hasn't migrated to the split still
-- works. The webapp Recent-runs panel reads the split columns directly.
--
-- Re-runnable: the ALTER TABLE statements use IF NOT EXISTS-style guards via
-- SQLite's table_info pragma, but the migrations runner already keys off
-- PRAGMA user_version so a successful run will not re-apply.

ALTER TABLE fitness_sync_runs ADD COLUMN workouts_fetched     INTEGER NOT NULL DEFAULT 0;
ALTER TABLE fitness_sync_runs ADD COLUMN wellness_fetched     INTEGER NOT NULL DEFAULT 0;
ALTER TABLE fitness_sync_runs ADD COLUMN workouts_normalized  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE fitness_sync_runs ADD COLUMN wellness_normalized  INTEGER NOT NULL DEFAULT 0;
