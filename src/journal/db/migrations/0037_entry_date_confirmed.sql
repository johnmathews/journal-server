-- Quarantine flag for entries whose detected date failed bounds
-- validation and could not be auto-repaired (spec
-- docs/superpowers/specs/2026-07-13-entry-date-integrity-design.md).
-- Existing rows are all confirmed. NOTE: the storyline_panels_legacy
-- drop is deliberately NOT in this migration — it ships separately,
-- in a later release than 0036 per the rollout runbook.
ALTER TABLE entries ADD COLUMN date_confirmed INTEGER NOT NULL DEFAULT 1;
