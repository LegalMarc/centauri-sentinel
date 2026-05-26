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

