# Race Condition in fallback_directory_cleanup Can Delete Fresh Snapshots

**ID:** BUG-12
**Severity:** High
**Category:** Correctness
**Status:** Closed

## Affected Files
- `sentinel/watcher/loop.py`

## Description
In `_on_confirmed_detection()`, a failure snapshot is written to disk before the printer is paused and before the event is written to the database. Since pausing can take up to 5 seconds, if the periodic cleanup task runs `fallback_directory_cleanup()` concurrently during this window, the database will not have the snapshot path yet, so it is identified as orphaned and deleted.

## Acceptance Criteria
- [x] Modify `fallback_directory_cleanup()` to exclude files modified within the last 60 seconds.
- [x] Add unit test verifying that fresh snapshots are not deleted during the cleanup sweep.
