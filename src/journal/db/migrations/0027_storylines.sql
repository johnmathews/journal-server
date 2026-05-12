-- Storylines feature (docs/storylines-plan.md, W2).
--
-- A storyline is a synthesized cross-entry narrative anchored on a single
-- entity (e.g. Atlas, Running). It is regenerated on demand and extended
-- automatically when new entries arrive that the extension classifier
-- judges relevant.
--
-- Two tables:
--
--   storylines        — one row per storyline (user x entity x name)
--   storyline_panels  — two rows per storyline: 'curation' and 'narrative'.
--                       Panels are split across rows so we can update one
--                       panel without rewriting the other (e.g. when only
--                       the Haiku glue is iterated) and so they can be
--                       returned independently to the webapp's two-column
--                       layout.
--
-- `segments_json` stores a JSON list of segment dicts following the
-- `Segment` shape in `services/storylines/segments.py`:
--   {"kind": "text", "text": "..."}
--   {"kind": "citation", "entry_id": 123, "quote": "..."}
-- The webapp renders text runs as plain text and citations as
-- `<RouterLink :to="`/entries/${entry_id}`">` links, so no markdown
-- renderer is needed.
--
-- `summary_embedding_json` is the embedding of the latest narrative panel
-- text, cached on the storyline row. Used by the extension classifier to
-- score new entries against the storyline without re-embedding every
-- regeneration. JSON-encoded float list.
--
-- Re-runnable: CREATE IF NOT EXISTS on every object; tolerates re-apply.

CREATE TABLE IF NOT EXISTS storylines (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                     INTEGER NOT NULL REFERENCES users(id),
    entity_id                   INTEGER NOT NULL REFERENCES entities(id),
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
    updated_at                  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(user_id, entity_id, name)
);

CREATE INDEX IF NOT EXISTS idx_storylines_user
    ON storylines(user_id);
CREATE INDEX IF NOT EXISTS idx_storylines_user_status
    ON storylines(user_id, status);
CREATE INDEX IF NOT EXISTS idx_storylines_entity
    ON storylines(entity_id);

CREATE TABLE IF NOT EXISTS storyline_panels (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    storyline_id            INTEGER NOT NULL REFERENCES storylines(id) ON DELETE CASCADE,
    panel_kind              TEXT    NOT NULL
                                CHECK(panel_kind IN ('curation', 'narrative')),
    segments_json           TEXT    NOT NULL DEFAULT '[]',
    source_entry_ids_json   TEXT    NOT NULL DEFAULT '[]',
    citation_count          INTEGER NOT NULL DEFAULT 0,
    model_used              TEXT    NOT NULL DEFAULT '',
    generated_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(storyline_id, panel_kind)
);

CREATE INDEX IF NOT EXISTS idx_storyline_panels_storyline
    ON storyline_panels(storyline_id);
