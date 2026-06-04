# Re-Detection on Stale Frames After Resume

**ID:** BUG-02
**Severity:** Medium
**Category:** Bugs
**Status:** Closed

## Affected Files
- `sentinel/watcher/loop.py` — post-resume cooldown partially mitigated but not configurable

## Description
After resuming a paused print, the watcher may re-detect on stale frames that were captured before the pause. A 5-second grace window and confirm-count reset partially mitigate this, but the cooldown is not configurable and may be insufficient for printers that take longer to visually clear the failure state.

## Evidence
- `loop.py` implements a 5-second grace window after resume.
- Confirm count is reset on resume.
- No configuration option to tune the cooldown period.

## Impact
- False-positive detections immediately after resume, leading to repeated pause cycles.
- User frustration from re-triggered alerts on prints that were already inspected and resumed.

## Acceptance Criteria
- [x] Configurable cooldown period after resume (e.g., `RESUME_COOLDOWN_SECONDS`)
- [x] Cooldown defaults to a sensible value (≥ 5s)
- [x] Frames captured during cooldown window are skipped for ML inference
- [x] Tests pass
- [x] Coverage maintained ≥ 85%
