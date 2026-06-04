# Full Table Scan on detection_events Table During Snapshot Cleanup

**ID:** PERF-05
**Severity:** Medium
**Category:** Performance
**Status:** Closed

## Affected Files
- `sentinel/db/schema.sql`
- `sentinel/db/migrate.py`

## Description
Both `get_snapshots_for_cleanup` and `fallback_directory_cleanup` query `detection_events` with `WHERE snapshot_path IS NOT NULL`. The database schema has no index on the `snapshot_path` column, leading to a full table scan as the database grows.

## Acceptance Criteria
- [x] Add a partial index on `snapshot_path` where it is not null: `CREATE INDEX IF NOT EXISTS idx_detection_events_snapshot_path ON detection_events(snapshot_path) WHERE snapshot_path IS NOT NULL;`.
- [x] Bump the database schema version to 5 in `migrate.py` and implement the migration.
- [x] Verify migrations run successfully and unit tests pass.
