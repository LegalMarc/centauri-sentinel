# Watcher Loop Permanent Crash via Stream Proxy Cancellation

**ID:** BUG-11
**Severity:** Critical
**Category:** Stability
**Status:** Closed

## Affected Files
- `sentinel/camera/mjpeg.py` — `MjpegGrabber.grab` calls `.exception()` on a cancelled task
- `sentinel/watcher/loop.py` — watcher loop propagates `CancelledError`

## Description
When the last stream proxy listener disconnects, `stream_proxy()` cancels `self._broadcaster_task`. If the main watcher task is currently in `grab()` waiting for a frame, it monitors the broadcaster task's status. If the broadcaster task is cancelled, `grab()` calls `self._broadcaster_task.exception()`, which raises `asyncio.CancelledError`. Because this is not caught locally, it propagates out of `_tick()` and through the watcher `_loop()`, permanently stopping the watcher loop task.

## Evidence
- `mjpeg.py` lines 98-101:
  ```python
  task_exc = self._broadcaster_task.exception()
  if task_exc:
      raise CameraReadError(f"Stream failed: {task_exc}") from task_exc
  ```
  Calling `.exception()` on a cancelled task raises `asyncio.CancelledError`.

## Impact
- A normal browser interaction (disconnecting from the `/stream` MJPEG stream) can permanently kill the background print-failure-detection watcher loop without any self-healing mechanism.

## Acceptance Criteria
- [x] In `MjpegGrabber.grab()`, handle the case where the broadcaster task was cancelled without raising `asyncio.CancelledError` out of the method.
- [x] Raise a `CameraReadError` or similar exception instead, so that the watcher loop treats it as a temporary camera glitch instead of task cancellation.
- [x] Add unit tests simulating stream proxy cancellation while a concurrent `grab()` is in progress, verifying that the watcher loop continues to run.
