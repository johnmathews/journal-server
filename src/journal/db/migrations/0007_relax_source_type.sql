-- Relax the source_type CHECK constraint to allow 'manual' and 'import'
-- in addition to 'ocr' and 'voice'. SQLite does not support ALTER COLUMN,
-- so the table must be recreated. FTS and triggers are preserved on
-- final_text (as established by migration 0002).

-- 1. Drop all triggers on entries (FTS + entity stale flag).
DROP TRIGGER IF EXISTS entries_ai;
DROP TRIGGER IF EXISTS entries_ad;
DROP TRIGGER IF EXISTS entries_au;
DROP TRIGGER IF EXISTS entries_entity_stale_on_final_text;
DROP TABLE IF EXISTS entries_fts;

-- 2. Create replacement table without the restrictive CHECK.
CREATE TABLE entries_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_date    TEXT NOT NULL,
    source_type   TEXT NOT NULL,
    raw_text      TEXT NOT NULL,
    word_count    INTEGER NOT NULL,
    language      TEXT DEFAULT 'en',
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    final_text    TEXT,
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    entity_extraction_stale INTEGER NOT NULL DEFAULT 1
);

-- 3. Copy existing rows.
INSERT INTO entries_new (
    id, entry_date, source_type, raw_text, word_count, language,
    created_at, updated_at, final_text, chunk_count, entity_extraction_stale
)
SELECT
    id, entry_date, source_type, raw_text, word_count, language,
    created_at, updated_at, final_text, chunk_count, entity_extraction_stale
FROM entries;

-- 4. Drop old table and rename.
DROP TABLE entries;
ALTER TABLE entries_new RENAME TO entries;

-- 5. Recreate indexes.
CREATE INDEX IF NOT EXISTS idx_entries_date   ON entries(entry_date);
CREATE INDEX IF NOT EXISTS idx_entries_source ON entries(source_type);

-- 6. Recreate FTS on final_text (matching migration 0002).
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    final_text,
    content='entries',
    content_rowid='id',
    tokenize='porter unicode61'
);

INSERT INTO entries_fts(entries_fts) VALUES('rebuild');

-- 7. Recreate FTS sync triggers on final_text.
CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid, final_text) VALUES (new.id, new.final_text);
END;

CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, final_text) VALUES ('delete', old.id, old.final_text);
END;

CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE OF final_text ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, final_text) VALUES ('delete', old.id, old.final_text);
    INSERT INTO entries_fts(rowid, final_text) VALUES (new.id, new.final_text);
END;

-- 8. Recreate entity stale-flag trigger (from migration 0004).
CREATE TRIGGER IF NOT EXISTS entries_entity_stale_on_final_text
AFTER UPDATE OF final_text ON entries
BEGIN
    UPDATE entries SET entity_extraction_stale = 1 WHERE id = new.id;
END;
