-- Add status_detail column for communicating transient state to the UI.
-- Used during retries to show e.g. "Retrying at 14:05 — Google API overloaded".
-- Cleared on terminal transitions (succeeded / failed).

ALTER TABLE jobs ADD COLUMN status_detail TEXT;
