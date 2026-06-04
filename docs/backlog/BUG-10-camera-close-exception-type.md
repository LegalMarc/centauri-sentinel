# Incompatible Exception Type in Camera Close Queue

**ID:** BUG-10
**Severity:** High
**Category:** Bugs
**Status:** Closed

## Affected Files
- `sentinel/camera/mjpeg.py` — `MjpegGrabber.close` puts `asyncio.CancelledError` in the listener queue

## Description
In `MjpegGrabber.close`, the queue is loaded with an `asyncio.CancelledError` instance. However, in Python 3.8+, `asyncio.CancelledError` inherits from `BaseException` rather than `Exception`. In `stream_proxy`, the check `isinstance(item, Exception)` fails to match, causing `item` (the `CancelledError` object) to be yielded as a normal frame. In `routes.py`, the streaming generator attempts to concatenate bytes with this object, raising a `TypeError` and crashing the generator task.

## Evidence
- `mjpeg.py` line 270:
  ```python
  q.put_nowait(asyncio.CancelledError("Camera closed/reconfigured"))
  ```
- `mjpeg.py` lines 253-255:
  ```python
  if isinstance(item, Exception):
      raise item
  yield item
  ```

## Impact
- Streaming responses for the web UI `/stream` route crash with `TypeError: can't concat bytes to CancelledError` when the camera is reconfigured or closed, leading to improper resource cleanups.

## Acceptance Criteria
- [x] In `MjpegGrabber.close()`, put a custom exception or a subclass of `Exception` in the queue (e.g. `CameraClosedError` or similar).
- [x] Or, modify `stream_proxy()` to handle `BaseException` or the specific cancellation condition gracefully.
- [x] Ensure that `TypeError` is not raised, and the connection closes cleanly.
- [x] Unit tests verify that closing/reconfiguring the camera terminates `stream_proxy` generators cleanly.
