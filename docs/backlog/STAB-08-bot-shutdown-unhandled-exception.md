# Potential Unhandled Exceptions During Telegram Bot Shutdown Blocking Cleanup

**ID:** STAB-08
**Severity:** Low
**Category:** Stability
**Status:** Closed

## Affected Files
- `sentinel/bot/runner.py`

## Description
`BotRunner._real_stop` catches `TimeoutError` but does not catch other exceptions that could be raised during `app.updater.stop()`, `app.stop()`, or `app.shutdown()`. Any unhandled exception will propagate and block the rest of the application shutdown sequence.

## Acceptance Criteria
- [x] Wrap bot stop and shutdown calls in a try-except block that logs the exception and ensures `self._app = None` is set, allowing other cleanup tasks to proceed.
- [x] Verify that bot shutdown succeeds cleanly even in the presence of connection errors.
