-- Storylines redesign (spec: docs/superpowers/plans/../specs/2026-07-12-storylines-redesign-design.md).
--
-- Chapters become draft/published with explicit entry membership; the
-- narrative panel folds directly into the chapter row (segments_json,
-- source_entry_ids_json, citation_count, model_used, generated_at).
-- Curation panels are retired. Both panel kinds are preserved verbatim in
-- storyline_panels_legacy until a later post-bootstrap cleanup migration
-- drops them. storylines sheds its date-range/summary-embedding/
-- last-generated columns, which move onto (or are replaced by) chapters.
--
-- Schema change requires table rebuilds (SQLite's ALTER TABLE can't drop
-- columns or change CHECK constraints while indexes reference them). The
-- standard SQLite remedy — create-new / copy / drop-old / rename — is used
-- for both `storylines` and `storyline_chapters`, following the pattern
-- established in 0028_storyline_entities.sql: PRAGMA foreign_keys is
-- toggled off *outside* any transaction (SQLite ignores the PRAGMA as a
-- no-op inside one), the whole rebuild is wrapped in one explicit
-- transaction, and `_executescript_idempotent` in the migration runner
-- rolls back a dangling transaction on failure so a crash anywhere inside
-- leaves the pre-migration state intact for the next attempt.
--
-- Re-runnability: the runner is forward-only (it skips any migration whose
-- version is <= PRAGMA user_version), so in normal operation this file
-- applies at most once. `DROP TABLE IF EXISTS ..._new` / `CREATE TABLE IF
-- NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` on every additive statement
-- are belt-and-braces protection for a forced re-apply (PRAGMA
-- user_version rewound), covered by test_rerunnable_after_partial_failure.
--
-- A forced re-apply is trickier here than in most rebuild migrations: by
-- the time a second pass runs, `storyline_chapters` and `storylines` are
-- already in the NEW shape (no `last_generated_at`, `state` already
-- 'draft'/'published', `storyline_panels` already dropped), so a query
-- written against the OLD columns would fail to even compile. The fix is
-- the same trick already required for `storyline_panels_legacy`: freeze
-- read-only snapshots of the OLD-shaped `storyline_chapters` and
-- `storyline_panels` via `CREATE TABLE IF NOT EXISTS ... AS SELECT`
-- *before* touching anything, and have the transform below read from
-- those frozen snapshots instead of the live tables. Because `CREATE
-- TABLE IF NOT EXISTS x AS SELECT ...` short-circuits without even
-- evaluating the SELECT once `x` exists, the snapshot step is a silent
-- no-op on a second pass — it does not require `storyline_panels` (long
-- gone by then) to still exist. Everything downstream then reads
-- unconditionally-valid columns, so the whole file — not just the
-- additive steps — is safe to force-replay.

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

-- 0. Freeze OLD-shaped snapshots the transform below depends on. Plain
--    SQL has no conditional execution, so this is what makes the
--    transform safe to force-replay after it has already fully
--    succeeded once (see the header comment).
CREATE TABLE IF NOT EXISTS storyline_chapters_legacy AS
    SELECT * FROM storyline_chapters;
CREATE TABLE IF NOT EXISTS storyline_panels_legacy AS
    SELECT * FROM storyline_panels;

-- 1. Rebuild `storylines` without start/end dates, summary embedding,
--    last_generated_at (all superseded by per-chapter fields below). The
--    columns selected here are unchanged by this migration, so reading
--    from the live table (old- or new-shaped) is safe either way.
DROP TABLE IF EXISTS storylines_new;
CREATE TABLE storylines_new (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name                        TEXT    NOT NULL,
    description                 TEXT    NOT NULL DEFAULT '',
    status                      TEXT    NOT NULL DEFAULT 'active'
                                    CHECK(status IN ('active', 'archived')),
    last_extension_check_at     TEXT,
    created_at                  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at                  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
INSERT INTO storylines_new (
    id, user_id, name, description, status,
    last_extension_check_at, created_at, updated_at
)
SELECT
    id, user_id, name, description, status,
    last_extension_check_at, created_at, updated_at
FROM storylines;

-- 2. Rebuild `storyline_chapters` in the new shape, folding in the
--    narrative panel and mapping open -> draft / closed -> published.
--    Reads from the frozen OLD-shaped snapshots (step 0), not the live
--    tables — see the header comment on force-replay safety.
DROP TABLE IF EXISTS storyline_chapters_new;
CREATE TABLE storyline_chapters_new (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    storyline_id            INTEGER NOT NULL REFERENCES storylines(id) ON DELETE CASCADE,
    seq                     INTEGER NOT NULL,
    title                   TEXT    NOT NULL DEFAULT '',
    state                   TEXT    NOT NULL DEFAULT 'draft'
                                CHECK(state IN ('draft', 'published')),
    segments_json           TEXT    NOT NULL DEFAULT '[]',
    source_entry_ids_json   TEXT    NOT NULL DEFAULT '[]',
    citation_count          INTEGER NOT NULL DEFAULT 0,
    model_used              TEXT    NOT NULL DEFAULT '',
    generated_at            TEXT,
    published_at            TEXT,
    read_at                 TEXT,
    addenda_json            TEXT    NOT NULL DEFAULT '[]',
    draft_embedding_json    TEXT,
    created_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at               TEXT   NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(storyline_id, seq)
);
INSERT INTO storyline_chapters_new (
    id, storyline_id, seq, title, state,
    segments_json, source_entry_ids_json,
    citation_count, model_used, generated_at,
    published_at, read_at,
    created_at, updated_at
)
SELECT
    c.id, c.storyline_id, c.seq, c.title,
    CASE c.state WHEN 'open' THEN 'draft' ELSE 'published' END,
    COALESCE(p.segments_json, '[]'),
    COALESCE(p.source_entry_ids_json, '[]'),
    COALESCE(p.citation_count, 0),
    COALESCE(p.model_used, ''),
    c.last_generated_at,
    CASE c.state WHEN 'closed' THEN COALESCE(c.last_generated_at, c.updated_at) END,
    -- Pre-existing published chapters start read: the migration must not
    -- manufacture a wall of unread badges for content the user has
    -- already seen.
    CASE c.state WHEN 'closed' THEN strftime('%Y-%m-%dT%H:%M:%SZ', 'now') END,
    c.created_at, c.updated_at
FROM storyline_chapters_legacy c
LEFT JOIN storyline_panels_legacy p
    ON p.chapter_id = c.id AND p.panel_kind = 'narrative';

-- 3. `storyline_panels_legacy` was already frozen in step 0 (both panel
--    kinds, for the verification window). `CREATE TABLE ... AS SELECT`
--    strips constraints, so it carries no FKs into tables we are about
--    to drop.

-- 4. Swap the rebuilt tables in. `storyline_panels`/`storyline_chapters`
--    may already be gone/renamed on a forced replay, hence IF EXISTS.
DROP TABLE IF EXISTS storyline_panels;
DROP TABLE IF EXISTS storyline_chapters;
ALTER TABLE storyline_chapters_new RENAME TO storyline_chapters;
DROP TABLE IF EXISTS storylines;
ALTER TABLE storylines_new RENAME TO storylines;

-- 5. Membership + pending tables.
CREATE TABLE IF NOT EXISTS storyline_chapter_entries (
    chapter_id  INTEGER NOT NULL REFERENCES storyline_chapters(id) ON DELETE CASCADE,
    entry_id    INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    added_late  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chapter_id, entry_id)
);
CREATE INDEX IF NOT EXISTS idx_storyline_chapter_entries_entry
    ON storyline_chapter_entries(entry_id);

-- Matched-but-unassigned entries awaiting the next storyline_update run.
CREATE TABLE IF NOT EXISTS storyline_pending_entries (
    storyline_id  INTEGER NOT NULL REFERENCES storylines(id) ON DELETE CASCADE,
    entry_id      INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (storyline_id, entry_id)
);

-- 6. Backfill membership from the folded narrative source ids (best-effort
--    placeholder until the bootstrap sweep regenerates everything).
INSERT OR IGNORE INTO storyline_chapter_entries (chapter_id, entry_id)
SELECT c.id, je.value
FROM storyline_chapters c, json_each(c.source_entry_ids_json) je
WHERE EXISTS (SELECT 1 FROM entries e WHERE e.id = je.value);

-- 7. Invariant repair: every storyline gets exactly one draft. Storylines
--    whose chapters were all closed get an empty draft appended.
INSERT INTO storyline_chapters (storyline_id, seq, state)
SELECT s.id,
       COALESCE((SELECT MAX(seq) FROM storyline_chapters c
                 WHERE c.storyline_id = s.id), 0) + 1,
       'draft'
FROM storylines s
WHERE NOT EXISTS (
    SELECT 1 FROM storyline_chapters c
    WHERE c.storyline_id = s.id AND c.state = 'draft'
);

-- 8. Indexes.
CREATE UNIQUE INDEX IF NOT EXISTS idx_storyline_chapters_one_draft
    ON storyline_chapters(storyline_id) WHERE state = 'draft';
CREATE INDEX IF NOT EXISTS idx_storylines_user
    ON storylines(user_id);

COMMIT;

PRAGMA foreign_keys = ON;
