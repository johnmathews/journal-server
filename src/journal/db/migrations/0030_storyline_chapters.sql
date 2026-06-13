-- Storyline chapters (docs/superpowers/specs/2026-06-13-storyline-chapters-design.md, Phase 1).
--
-- A new storyline_chapters table sits between storylines and
-- storyline_panels. Panels move from referencing storyline_id to
-- chapter_id. Anchors (storyline_entities) stay storyline-level.
--
-- Re-runnability: the chapters table uses IF NOT EXISTS; the backfill is
-- NOT EXISTS-guarded; the panel rebuild is wrapped in an explicit
-- transaction so a partial failure rolls back to the pre-migration state
-- (the runner rolls back any open transaction on error). Each existing
-- storyline becomes a single open chapter (seq 1) with no data loss.
--
-- The runner (db/migrations.py) is forward-only: it skips any migration
-- whose version is <= PRAGMA user_version, so this file is applied at
-- most once in normal operation. The panel rebuild references the OLD
-- storyline_panels.storyline_id column, which only exists pre-migration;
-- that is correct under forward-only application. (A forced re-apply
-- after rewinding user_version is not a supported operation for a plain
-- table-rebuild migration — see test_migration_0030_chapters.py, which
-- verifies the real forward-only re-run is a clean no-op.)

-- 1. Chapters table + indexes (idempotent).
CREATE TABLE IF NOT EXISTS storyline_chapters (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    storyline_id             INTEGER NOT NULL REFERENCES storylines(id) ON DELETE CASCADE,
    seq                      INTEGER NOT NULL,
    title                    TEXT    NOT NULL DEFAULT '',
    start_date               TEXT,
    end_date                 TEXT,
    state                    TEXT    NOT NULL DEFAULT 'open'
                                 CHECK(state IN ('open', 'closed')),
    last_generated_at        TEXT,
    summary_embedding_json   TEXT,
    created_at               TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at               TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(storyline_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_storyline_chapters_storyline
    ON storyline_chapters(storyline_id);
-- At most one open chapter per storyline.
CREATE UNIQUE INDEX IF NOT EXISTS idx_storyline_chapters_one_open
    ON storyline_chapters(storyline_id) WHERE state = 'open';

-- 2. Backfill: one open chapter per existing storyline (idempotent).
INSERT INTO storyline_chapters
    (storyline_id, seq, title, start_date, end_date, state,
     last_generated_at, summary_embedding_json)
SELECT s.id, 1, s.name, s.start_date, s.end_date, 'open',
       s.last_generated_at, s.summary_embedding_json
FROM storylines s
WHERE NOT EXISTS (
    SELECT 1 FROM storyline_chapters c WHERE c.storyline_id = s.id
);

-- 3. Rebuild storyline_panels to key on chapter_id with
--    UNIQUE(chapter_id, panel_kind). SQLite can't drop the old
--    NOT NULL + UNIQUE(storyline_id, panel_kind), so rebuild atomically.
DROP TABLE IF EXISTS storyline_panels_new;
BEGIN;
CREATE TABLE storyline_panels_new (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id              INTEGER NOT NULL REFERENCES storyline_chapters(id) ON DELETE CASCADE,
    panel_kind              TEXT    NOT NULL
                                CHECK(panel_kind IN ('curation', 'narrative')),
    segments_json           TEXT    NOT NULL DEFAULT '[]',
    source_entry_ids_json   TEXT    NOT NULL DEFAULT '[]',
    citation_count          INTEGER NOT NULL DEFAULT 0,
    model_used              TEXT    NOT NULL DEFAULT '',
    generated_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(chapter_id, panel_kind)
);
INSERT INTO storyline_panels_new
    (id, chapter_id, panel_kind, segments_json,
     source_entry_ids_json, citation_count, model_used, generated_at)
SELECT p.id, c.id, p.panel_kind, p.segments_json,
       p.source_entry_ids_json, p.citation_count, p.model_used, p.generated_at
FROM storyline_panels p
JOIN storyline_chapters c
    ON c.storyline_id = p.storyline_id AND c.seq = 1;
DROP TABLE storyline_panels;
ALTER TABLE storyline_panels_new RENAME TO storyline_panels;
COMMIT;

CREATE INDEX IF NOT EXISTS idx_storyline_panels_chapter
    ON storyline_panels(chapter_id);
