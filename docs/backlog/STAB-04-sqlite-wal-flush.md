# No Explicit WAL Checkpoint During Graceful Shutdown

**ID:** STAB-04
**Severity:** Medium
**Category:** Stability
**Status:** Closed

## Affected Files
- `sentinel/__main__.py` — shutdown handler does not trigger WAL checkpoint
- `sentinel/db/repo.py` — no checkpoint method exposed

## Description
SQLite WAL (Write-Ahead Logging) mode defers writing to the main database file. During graceful shutdown, no explicit `PRAGMA wal_checkpoint(TRUNCATE)` is issued before closing the database connection. This can leave data in the WAL file that may be lost or cause slow recovery on next startup.

## Evidence
- `__main__.py` shutdown sequence calls `db.close()` without a prior checkpoint.
- `repo.py` does not expose a checkpoint or flush method.
- WAL file can grow unbounded between automatic checkpoints.

## Impact
- Data written shortly before shutdown may not be persisted to the main DB file.
- Large WAL files slow down startup recovery.
- Risk of data loss on unclean container termination following a missed checkpoint.

## Acceptance Criteria
- [x] `PRAGMA wal_checkpoint(TRUNCATE)` called before `db.close()` in shutdown sequence
- [x] `repo.py` exposes a `checkpoint()` method
- [x] Checkpoint errors logged but do not prevent shutdown
- [x] Tests pass
- [x] Coverage maintained ≥ 85%
