-- Capture quarantine state in merge history snapshots so the audit trail
-- isn't lost when a quarantined entity is merged into a clean survivor.

ALTER TABLE entity_merge_history
  ADD COLUMN absorbed_is_quarantined INTEGER NOT NULL DEFAULT 0;
ALTER TABLE entity_merge_history
  ADD COLUMN absorbed_quarantine_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE entity_merge_history
  ADD COLUMN absorbed_quarantined_at TEXT NOT NULL DEFAULT '';
