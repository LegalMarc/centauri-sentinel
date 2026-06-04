# Some httpx Exceptions Escape Without CameraReadError Wrapping

**ID:** BUG-07
**Severity:** Low
**Category:** Bugs
**Status:** Closed

## Affected Files
- `sentinel/camera/mjpeg.py` — `_grab_once` does not catch all `httpx` exception types

## Description
The `_grab_once` method in `mjpeg.py` catches specific `httpx` exceptions but may miss others (e.g., `httpx.ReadError`, `httpx.RemoteProtocolError`). Unhandled exceptions propagate up as raw `httpx` errors instead of being wrapped in the domain-specific `CameraReadError`.

## Evidence
- `_grab_once` has targeted `except` clauses for some `httpx` exceptions.
- `httpx` defines additional exception types that are not covered.
- Callers expect `CameraReadError` for all camera-related failures.

## Impact
- Unexpected exception types crash the watcher loop instead of triggering graceful retry/backoff.
- Error handling logic in callers that catches `CameraReadError` will miss unwrapped exceptions.

## Acceptance Criteria
- [x] All exceptions in `_grab_once` wrapped as `CameraReadError`
- [x] Broad `except httpx.HTTPError` or `except Exception` with `CameraReadError` re-raise
- [x] Original exception preserved via `from` clause for debugging
- [x] Tests pass
- [x] Coverage maintained ≥ 85%
