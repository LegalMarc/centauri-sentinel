-- centauri-sentinel SQLite schema
-- Managed by sentinel/db/migrate.py; do not run directly.

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS detection_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    score           REAL    NOT NULL,
    consecutive     INTEGER NOT NULL,
    confirmed       INTEGER NOT NULL,      -- 0/1; 1 means this event triggered a pause
    snapshot_path   TEXT                   -- optional, on confirmed only
);

CREATE TABLE IF NOT EXISTS pause_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    source          TEXT    NOT NULL,      -- 'auto' | 'telegram' | 'web'
    result          TEXT    NOT NULL,      -- 'ok' | 'error'
    error_message   TEXT
);

CREATE TABLE IF NOT EXISTS runtime_settings (
    key             TEXT    PRIMARY KEY,
    value           TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS watcher_heartbeat (
    id              INTEGER PRIMARY KEY CHECK (id = 1),  -- single row
    last_tick_utc   TEXT    NOT NULL,
    state           TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS print_jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    filename            TEXT    NOT NULL,
    started_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    ended_at            TEXT,
    duration_seconds    INTEGER,
    filament_used_g     REAL,
    status              TEXT    NOT NULL,  -- 'printing' | 'completed' | 'failed'
    pauses_count        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_print_jobs_status ON print_jobs(status);
CREATE INDEX IF NOT EXISTS idx_detection_events_confirmed ON detection_events(confirmed);
CREATE INDEX IF NOT EXISTS idx_pause_history_result ON pause_history(result);
CREATE INDEX IF NOT EXISTS idx_detection_events_snapshot_path ON detection_events(snapshot_path) WHERE snapshot_path IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_detection_events_ts_utc ON detection_events(ts_utc);
CREATE INDEX IF NOT EXISTS idx_print_jobs_started_at ON print_jobs(started_at);
CREATE INDEX IF NOT EXISTS idx_pause_history_ts_utc ON pause_history(ts_utc);
