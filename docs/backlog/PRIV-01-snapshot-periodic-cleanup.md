# Snapshot Cleanup Only Runs During Detection

**ID:** PRIV-01
**Severity:** Medium
**Category:** Privacy
**Status:** Completed

## Affected Files
- `sentinel/watcher/loop.py` — cleanup logic tied to `_on_confirmed_detection` code path

## Description
Snapshot file cleanup only executes as part of the `_on_confirmed_detection` flow. If the watcher is stopped or no detections occur for an extended period, old snapshot files persist on disk indefinitely, potentially retaining sensitive images of the user's environment.

## Evidence
- Cleanup function called only within `_on_confirmed_detection`.
- No periodic or scheduled cleanup task exists.
- Snapshot directory can grow unbounded when watcher is idle.

## Impact
- Privacy violation: old snapshots containing images of the user's space persist indefinitely.
- Disk usage grows unbounded on long-running deployments without detections.

## Acceptance Criteria
- [x] Periodic cleanup runs regardless of detection state (e.g., on a timer or at startup)
- [x] Cleanup interval configurable via environment variable
- [x] Snapshots older than retention limit deleted even when watcher is stopped
- [x] Tests pass
- [x] Coverage maintained ≥ 85%
