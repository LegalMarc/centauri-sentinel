-- centauri-sentinel SQLite schema
-- Managed by sentinel/db/migrate.py; do not run directly.

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL,
    applied_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS detection_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    score       REAL    NOT NULL,
    snapshot_id TEXT,
    action      TEXT    NOT NULL DEFAULT 'alert'  -- alert | paused | ignored
);

CREATE TABLE IF NOT EXISTS pause_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    ended_at    TEXT,
    reason      TEXT    NOT NULL DEFAULT 'detection'
);

CREATE TABLE IF NOT EXISTS runtime_settings (
    key         TEXT    PRIMARY KEY,
    value       TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS watcher_heartbeat (
    id          INTEGER PRIMARY KEY CHECK (id = 1),  -- single row
    last_beat   TEXT    NOT NULL
);
