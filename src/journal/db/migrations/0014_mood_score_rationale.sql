-- Migration 0014: Add rationale column to mood_scores.
--
-- Stores the 1-2 sentence explanation from the LLM alongside each
-- numeric score so the Insights page can show *why* a mood score
-- was given. NULL for rows created before this migration; populate
-- by running `journal backfill-mood --force`.

ALTER TABLE mood_scores ADD COLUMN rationale TEXT;
