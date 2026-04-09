-- Multi-page entries and OCR correction support

-- 1. Add new columns to entries
ALTER TABLE entries ADD COLUMN final_text TEXT;
ALTER TABLE entries ADD COLUMN chunk_count INTEGER NOT NULL DEFAULT 0;

-- 2. Backfill final_text from raw_text
UPDATE entries SET final_text = raw_text;

-- 3. Create entry_pages table
CREATE TABLE IF NOT EXISTS entry_pages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id      INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    page_number   INTEGER NOT NULL,
    raw_text      TEXT NOT NULL,
    source_file_id INTEGER REFERENCES source_files(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(entry_id, page_number)
);

CREATE INDEX IF NOT EXISTS idx_entry_pages_entry ON entry_pages(entry_id);

-- 4. Migrate existing OCR entries into entry_pages (one page per entry)
INSERT INTO entry_pages (entry_id, page_number, raw_text, source_file_id)
    SELECT e.id, 1, e.raw_text, sf.id
    FROM entries e
    LEFT JOIN source_files sf ON sf.entry_id = e.id
    WHERE e.source_type = 'ocr';

-- 5. Rebuild FTS to index final_text instead of raw_text
DROP TRIGGER IF EXISTS entries_ai;
DROP TRIGGER IF EXISTS entries_ad;
DROP TRIGGER IF EXISTS entries_au;
DROP TABLE IF EXISTS entries_fts;

CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    final_text,
    content='entries',
    content_rowid='id',
    tokenize='porter unicode61'
);

INSERT INTO entries_fts(rowid, final_text)
    SELECT id, final_text FROM entries;

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
