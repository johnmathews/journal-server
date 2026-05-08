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
INSERT INTO entity_merge_candidates_new
    (entity_id_a, entity_id_b, similarity, status,
     extraction_run_id, created_at, updated_at, resolved_at)
SELECT
    MIN(entity_id_a, entity_id_b),
    MAX(entity_id_a, entity_id_b),
    MAX(similarity),
    CASE
        WHEN SUM(CASE WHEN status = 'accepted'  THEN 1 ELSE 0 END) > 0 THEN 'accepted'
        WHEN SUM(CASE WHEN status = 'dismissed' THEN 1 ELSE 0 END) > 0 THEN 'dismissed'
        ELSE 'pending'
    END,
    MAX(extraction_run_id),
    MIN(created_at),
    MAX(COALESCE(resolved_at, created_at)),
    MAX(resolved_at)
FROM entity_merge_candidates
GROUP BY MIN(entity_id_a, entity_id_b), MAX(entity_id_a, entity_id_b);

DROP TABLE entity_merge_candidates;
ALTER TABLE entity_merge_candidates_new RENAME TO entity_merge_candidates;

CREATE INDEX IF NOT EXISTS idx_merge_candidates_status
    ON entity_merge_candidates(status);
