-- Initial schema for journal analysis tool

CREATE TABLE IF NOT EXISTS entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_date    TEXT NOT NULL,
    source_type   TEXT NOT NULL CHECK(source_type IN ('ocr', 'voice')),
    raw_text      TEXT NOT NULL,
    word_count    INTEGER NOT NULL,
    language      TEXT DEFAULT 'en',
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(entry_date);
CREATE INDEX IF NOT EXISTS idx_entries_source ON entries(source_type);

CREATE TABLE IF NOT EXISTS mood_scores (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id      INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    dimension     TEXT NOT NULL DEFAULT 'overall',
    score         REAL NOT NULL CHECK(score >= -1.0 AND score <= 1.0),
    confidence    REAL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_mood_entry ON mood_scores(entry_id);

CREATE TABLE IF NOT EXISTS people (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    first_seen    TEXT NOT NULL,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS entry_people (
    entry_id      INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    person_id     INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    PRIMARY KEY (entry_id, person_id)
);

CREATE TABLE IF NOT EXISTS places (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    first_seen    TEXT NOT NULL,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS entry_places (
    entry_id      INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    place_id      INTEGER NOT NULL REFERENCES places(id) ON DELETE CASCADE,
    PRIMARY KEY (entry_id, place_id)
);

CREATE TABLE IF NOT EXISTS tags (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS entry_tags (
    entry_id      INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    tag_id        INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (entry_id, tag_id)
);

CREATE TABLE IF NOT EXISTS source_files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id      INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    file_path     TEXT NOT NULL,
    file_type     TEXT NOT NULL,
    file_hash     TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_source_files_hash ON source_files(file_hash);

-- Full-text search index
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    raw_text,
    content='entries',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid, raw_text) VALUES (new.id, new.raw_text);
END;

CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, raw_text) VALUES ('delete', old.id, old.raw_text);
END;

CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, raw_text) VALUES ('delete', old.id, old.raw_text);
    INSERT INTO entries_fts(rowid, raw_text) VALUES (new.id, new.raw_text);
END;
