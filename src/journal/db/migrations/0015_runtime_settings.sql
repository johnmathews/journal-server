-- Runtime settings: key/value store for settings editable from the webapp
-- without container restart. Falls back to env-var defaults on first boot.

CREATE TABLE IF NOT EXISTS runtime_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
