-- Soft-quarantine flag for entities.
--
-- Quarantined entities remain in the DB (description, aliases, merge history
-- preserved) but are excluded from default entity-list and chart endpoints.
-- A separate endpoint exposes them so operators can review and either release
-- or merge them. See docs/entity-tracking.md "Quarantine" section.

ALTER TABLE entities ADD COLUMN is_quarantined INTEGER NOT NULL DEFAULT 0;
ALTER TABLE entities ADD COLUMN quarantine_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE entities ADD COLUMN quarantined_at TEXT NOT NULL DEFAULT '';

-- Partial index keeps the active-entity path fast while the rare
-- "list quarantined" lookup remains O(log n).
CREATE INDEX IF NOT EXISTS idx_entities_quarantined
    ON entities(is_quarantined) WHERE is_quarantined = 1;
