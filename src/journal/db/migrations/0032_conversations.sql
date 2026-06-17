-- Conversations: persisted multi-turn chats about a journal answer.
--
-- A conversation is seeded from an existing Search answer (one user turn
-- + one assistant turn) and grows by re-grounding each follow-up against
-- the journal. Both tables are user-scoped; deleting a conversation
-- cascades to its messages, and deleting a user cascades to their
-- conversations. Timestamps are ISO-8601 strings, matching the rest of
-- the schema. Re-runnable: CREATE TABLE IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_user
    ON conversations(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS conversation_messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role             TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content          TEXT NOT NULL,
    citations        TEXT,            -- JSON [{entry_id,entry_date,snippet}], assistant only
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversation_messages_conv
    ON conversation_messages(conversation_id, id);
