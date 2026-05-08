-- Persistent "not a duplicate" decisions.
--
-- When the user dismisses a merge candidate we also record the pair here so
-- future extraction runs skip suggesting it again. Without this, the same
-- pair (e.g. "John Mathews" vs "John Mathews' mother") gets re-flagged on
-- every extraction.
--
-- Stored normalised (entity_id_lo < entity_id_hi) so lookup is
-- order-independent and (user_id, lo, hi) uniqueness is sufficient.

CREATE TABLE IF NOT EXISTS entity_pair_decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    entity_id_lo  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    entity_id_hi  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    decision      TEXT    NOT NULL DEFAULT 'rejected' CHECK(decision IN ('rejected')),
    decided_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK (entity_id_lo < entity_id_hi),
    UNIQUE (user_id, entity_id_lo, entity_id_hi)
);

CREATE INDEX IF NOT EXISTS idx_pair_decisions_user
    ON entity_pair_decisions(user_id);
CREATE INDEX IF NOT EXISTS idx_pair_decisions_lo
    ON entity_pair_decisions(entity_id_lo);
CREATE INDEX IF NOT EXISTS idx_pair_decisions_hi
    ON entity_pair_decisions(entity_id_hi);
