-- Enforce one mood_scores row per (entry_id, dimension).
--
-- The mood pipeline is meant to hold a single score per facet per entry:
-- `replace_mood_scores` already does delete-then-insert per dimension, and
-- `add_mood_score` was the only path that could ever create a duplicate.
-- Nothing enforced uniqueness at the schema level, so historical rows (or a
-- crash between delete and insert) could leave two rows for the same
-- (entry_id, dimension). This migration collapses any such duplicates and
-- adds the missing constraint.
--
-- SQLite cannot ALTER-ADD a table-level UNIQUE constraint, so a UNIQUE INDEX
-- is the right tool — it enforces the same invariant and reads/writes exactly
-- like a table constraint would.
--
-- Re-runnable from any partial state:
--   * The dedup DELETE is a no-op once the index exists (no dupes can remain),
--     and harmless on an empty / already-clean table.
--   * CREATE UNIQUE INDEX IF NOT EXISTS is a no-op on the second pass.
-- The forward-only runner also version-skips this file after user_version >= 38.

-- 1. Dedup: keep the newest row (largest id) per (entry_id, dimension).
DELETE FROM mood_scores
WHERE id NOT IN (
    SELECT MAX(id) FROM mood_scores GROUP BY entry_id, dimension
);

-- 2. Enforce the invariant going forward.
CREATE UNIQUE INDEX IF NOT EXISTS idx_mood_entry_dimension
    ON mood_scores(entry_id, dimension);
