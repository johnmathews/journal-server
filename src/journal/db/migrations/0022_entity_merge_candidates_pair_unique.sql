-- Per-pair uniqueness for entity_merge_candidates.
--
-- The original schema (migration 0008) keyed on (a, b, extraction_run_id),
-- which let the same entity pair accumulate one row per extraction run.
-- That produced "3 of 10 are identical" lists in the merge UI and meant
-- a dismissal only blocked the candidate for that one run — a new run
-- would re-insert the pair as 'pending'.
--
-- Migration 0021 added the entity_pair_decisions table for cross-run
-- "not a duplicate" memory. This migration finishes the job by making
-- the candidates table itself per-pair-unique. From now on, repeated
-- extraction merely UPSERTs the existing row.
--
-- SQLite cannot drop a UNIQUE constraint in place — rebuild the table.
-- During the rebuild we collapse historical rows by pair, taking the
-- highest similarity and a sensible aggregate status (accepted >
-- dismissed > pending) so resolution history is preserved.
--
-- Idempotency: Python's sqlite3 ``executescript`` does not roll back on
-- mid-script failure (each statement is autocommitted). If a prior run
-- of this migration crashed partway, the ``_new`` table will still be
-- present and the next attempt would die at "table already exists".
-- Drop the leftover up-front so a retry can succeed cleanly.

DROP TABLE IF EXISTS entity_merge_candidates_new;

CREATE TABLE entity_merge_candidates_new (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id_a       INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    entity_id_b       INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    similarity        REAL    NOT NULL,
    status            TEXT    NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'accepted', 'dismissed')),
    extraction_run_id TEXT,                    -- last run that touched the pair (informational)
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    resolved_at       TEXT,
    CHECK (entity_id_a < entity_id_b),
    UNIQUE (entity_id_a, entity_id_b)
);

-- Collapse rows by normalised pair. MIN/MAX establish the (lo, hi) ordering
-- enforced by the new CHECK constraint. Status precedence is hand-rolled
-- since SQLite has no MAX over arbitrary string ordering.
--
-- INNER JOINs against ``entities`` filter out orphan rows whose
-- entity_id_a or entity_id_b references a deleted entity — the new
-- table's FK constraints would reject them and abort the migration.
-- Orphans should not exist (the source table has ON DELETE CASCADE on
-- both sides) but real prod DBs accumulate them when something has
-- bypassed FK enforcement at any point in their history.
INSERT INTO entity_merge_candidates_new
    (entity_id_a, entity_id_b, similarity, status,
     extraction_run_id, created_at, updated_at, resolved_at)
SELECT
    MIN(c.entity_id_a, c.entity_id_b),
    MAX(c.entity_id_a, c.entity_id_b),
    MAX(c.similarity),
    CASE
        WHEN SUM(CASE WHEN c.status = 'accepted'  THEN 1 ELSE 0 END) > 0 THEN 'accepted'
        WHEN SUM(CASE WHEN c.status = 'dismissed' THEN 1 ELSE 0 END) > 0 THEN 'dismissed'
        ELSE 'pending'
    END,
    MAX(c.extraction_run_id),
    MIN(c.created_at),
    MAX(COALESCE(c.resolved_at, c.created_at)),
    MAX(c.resolved_at)
FROM entity_merge_candidates c
INNER JOIN entities ea ON ea.id = c.entity_id_a
INNER JOIN entities eb ON eb.id = c.entity_id_b
GROUP BY MIN(c.entity_id_a, c.entity_id_b), MAX(c.entity_id_a, c.entity_id_b);

DROP TABLE entity_merge_candidates;
ALTER TABLE entity_merge_candidates_new RENAME TO entity_merge_candidates;

CREATE INDEX IF NOT EXISTS idx_merge_candidates_status
    ON entity_merge_candidates(status);
