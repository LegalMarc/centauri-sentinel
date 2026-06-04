# Stale Print Duration Calculations on Sentinel Restart Mid-Print

**ID:** BUG-13
**Severity:** Low
**Category:** Correctness
**Status:** Closed

## Affected Files
- `sentinel/watcher/loop.py`

## Description
When a print job ends, `duration` is computed as `(now - self._print_start)` first. If sentinel is restarted mid-print, `self._print_start` is set to the restart time, resulting in a recorded duration that is significantly shorter than the actual print time, ignoring the printer's accurate `elapsed_seconds`.

## Acceptance Criteria
- [x] Prefer using the printer's `status.elapsed_seconds` or last known elapsed status when recording duration on print completion/termination.
- [x] Verify print duration accuracy is preserved across sentinel process restarts.
