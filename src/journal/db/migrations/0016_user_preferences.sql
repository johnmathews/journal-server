-- User preferences: per-user key-value store with JSON values.
-- Used for dashboard layout, filter defaults, and other UI preferences.

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id    INTEGER NOT NULL,
    key        TEXT    NOT NULL,
    value      TEXT    NOT NULL,  -- JSON-encoded
    updated_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (user_id, key),
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);
