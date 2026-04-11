-- Async batch job tracking. Rows persist the state of long-running
-- operations (entity extraction, mood backfill) so that progress and
-- results survive the lifetime of the HTTP request that kicked them
-- off. A future JobRunner service owns transitions: handlers insert a
-- 'queued' row and return its id, the runner flips it to 'running'
-- while work happens, and finally to 'succeeded' or 'failed'. On
-- server restart, any rows still in 'queued' or 'running' are
-- reconciled to 'failed' at startup — jobs do not resume across
-- processes.

CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT    PRIMARY KEY,                         -- UUID v4 string
    type             TEXT    NOT NULL,                             -- 'entity_extraction' | 'mood_backfill'
    status           TEXT    NOT NULL,                             -- 'queued' | 'running' | 'succeeded' | 'failed'
    params_json      TEXT    NOT NULL,                             -- JSON-encoded input params
    progress_current INTEGER NOT NULL DEFAULT 0,
    progress_total   INTEGER NOT NULL DEFAULT 0,
    result_json      TEXT,                                         -- JSON-encoded result summary when done
    error_message    TEXT,
    created_at       TEXT    NOT NULL,                             -- ISO 8601
    started_at       TEXT,                                         -- ISO 8601, null until running
    finished_at      TEXT                                          -- ISO 8601, null until terminal
);

CREATE INDEX IF NOT EXISTS idx_jobs_status     ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);
