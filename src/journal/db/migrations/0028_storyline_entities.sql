-- Multi-entity storylines: introduce a join table and retire the legacy
-- single-anchor column on ``storylines``.
--
-- A storyline can now be anchored on 1..N entities (current soft cap is
-- 15, enforced in service code). The (storyline, entity) pairs live in
-- a new join table; ``storylines.entity_id`` and the UNIQUE constraint
-- that referenced it (``UNIQUE(user_id, entity_id, name)``) are
-- removed. Application-level dedup via ``find_by_anchor_set`` replaces
-- the DB-level uniqueness — different anchor sets can share a name.
--
-- Schema change requires a table rebuild. SQLite's ``ALTER TABLE DROP
-- COLUMN`` refuses to run while any index or table-level UNIQUE
-- constraint still references the column, and the implicit
-- ``sqlite_autoindex_storylines_1`` from the original UNIQUE clause
-- counts. The standard SQLite remedy (see
-- https://www.sqlite.org/lang_altertable.html, "Making Other Kinds Of
-- Table Schema Changes") is the create-new / copy / drop-old / rename
-- pattern, executed with FK enforcement temporarily off and wrapped
-- in an explicit transaction so the rebuild is atomic.
--
-- Re-runnability. The whole rebuild is wrapped in BEGIN / COMMIT, and
-- ``_executescript_idempotent`` in the migration runner rolls back any
-- dangling transaction on failure. So a crash anywhere inside the
-- migration leaves the database in the pre-migration state, and the
-- next run starts cleanly. ``CREATE TABLE IF NOT EXISTS`` /
-- ``DROP TABLE IF EXISTS`` / ``INSERT OR IGNORE`` are belt-and-braces
-- protection for the additive parts.

-- PRAGMA foreign_keys must be set *outside* any transaction; SQLite
-- ignores it as a no-op when a transaction is open. With FK still on,
-- ``DROP TABLE storylines`` below would cascade-delete the backfilled
-- ``storyline_entities`` rows we just inserted.
PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

-- New join table.
CREATE TABLE IF NOT EXISTS storyline_entities (
    storyline_id    INTEGER NOT NULL REFERENCES storylines(id) ON DELETE CASCADE,
    entity_id       INTEGER NOT NULL REFERENCES entities(id),
    PRIMARY KEY (storyline_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_storyline_entities_entity
    ON storyline_entities(entity_id);

-- Backfill from the legacy single-anchor column.
INSERT OR IGNORE INTO storyline_entities (storyline_id, entity_id)
SELECT id, entity_id FROM storylines WHERE entity_id IS NOT NULL;

-- Rebuild storylines without ``entity_id`` and without
-- ``UNIQUE(user_id, entity_id, name)``. The remaining columns and
-- their defaults are copied verbatim from migration 0027.
DROP TABLE IF EXISTS storylines_new;

CREATE TABLE storylines_new (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                     INTEGER NOT NULL REFERENCES users(id),
    name                        TEXT    NOT NULL,
    description                 TEXT    NOT NULL DEFAULT '',
    start_date                  TEXT,
    end_date                    TEXT,
    status                      TEXT    NOT NULL DEFAULT 'active'
                                    CHECK(status IN ('active', 'archived')),
    last_generated_at           TEXT,
    last_extension_check_at     TEXT,
    summary_embedding_json      TEXT,
    created_at                  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at                  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

INSERT INTO storylines_new (
    id, user_id, name, description, start_date, end_date, status,
    last_generated_at, last_extension_check_at, summary_embedding_json,
    created_at, updated_at
)
SELECT
    id, user_id, name, description, start_date, end_date, status,
    last_generated_at, last_extension_check_at, summary_embedding_json,
    created_at, updated_at
FROM storylines;

DROP TABLE storylines;
ALTER TABLE storylines_new RENAME TO storylines;

CREATE INDEX IF NOT EXISTS idx_storylines_user
    ON storylines(user_id);
CREATE INDEX IF NOT EXISTS idx_storylines_user_status
    ON storylines(user_id, status);

COMMIT;

PRAGMA foreign_keys = ON;
