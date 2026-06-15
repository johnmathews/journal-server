-- Migration 0031: chapter sectioning columns (locks + cached word count).
--
-- Lays the foundation for word-sized auto-split chapters
-- (see docs/storylines.md — chapter sectioning):
--
--   * title_locked         — 1 once the user manually renames a chapter.
--                            Re-segment must never overwrite a locked title.
--   * boundary_locked      — 1 once the user hand-paints a chapter's window
--                            (creates / splits / date-edits it). Re-segment
--                            must never carve into a hand-painted boundary.
--   * narrative_word_count — cached word count of the chapter's narrative
--                            prose, used by the segmenter to size chapters.
--
-- The runner (db/migrations.py) is forward-only: it skips any migration
-- whose version is <= PRAGMA user_version, so this file applies at most
-- once in normal operation. The three statements below are each plain
-- additive ALTER TABLE ADD COLUMN. SQLite has no ADD COLUMN IF NOT EXISTS,
-- so re-runnability after a partial failure is provided by the runner,
-- which catches "duplicate column name" and treats it as a no-op. Each ADD
-- is its own statement, so a re-run that already has (say) the first column
-- swallows that one duplicate and proceeds to add the remaining two.

ALTER TABLE storyline_chapters ADD COLUMN title_locked INTEGER NOT NULL DEFAULT 0;
ALTER TABLE storyline_chapters ADD COLUMN boundary_locked INTEGER NOT NULL DEFAULT 0;
ALTER TABLE storyline_chapters ADD COLUMN narrative_word_count INTEGER NOT NULL DEFAULT 0;
