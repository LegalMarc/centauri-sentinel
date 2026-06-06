"""Tests for sentinel/notify/dispatcher.py"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from sentinel.notify.dispatcher import NotificationDispatcher


async def test_dispatch_all_methods_fire_and_forget():
    notifier1 = MagicMock()
    notifier1.send_detection_alert = AsyncMock()
    notifier1.send_stall_alert = AsyncMock()
    notifier1.send_camera_offline_alert = AsyncMock()
    notifier1.send_text = AsyncMock()
    notifier1.send_print_started_alert = AsyncMock()
    notifier1.send_print_completed_alert = AsyncMock()
    notifier1.send_external_pause_alert = AsyncMock()

    dispatcher = NotificationDispatcher([notifier1])

    # Fire them all
    dispatcher.dispatch_detection(0.9, "snap1", b"jpeg")
    dispatcher.dispatch_stall()
    dispatcher.dispatch_camera_offline()
    dispatcher.dispatch_text("hello")
    dispatcher.dispatch_print_started("file.gcode", b"jpeg")
    dispatcher.dispatch_print_completed("file.gcode", 100.0, b"jpeg")
    dispatcher.dispatch_external_pause(b"jpeg")

    # Tasks are fired and forgotten, so we yield to let them execute
    await asyncio.sleep(0.01)

    notifier1.send_detection_alert.assert_called_once_with(0.9, "snap1", b"jpeg")
    notifier1.send_stall_alert.assert_called_once()
    notifier1.send_camera_offline_alert.assert_called_once()
    notifier1.send_text.assert_called_once_with("hello")
    notifier1.send_print_started_alert.assert_called_once_with("file.gcode", b"jpeg")
    notifier1.send_print_completed_alert.assert_called_once_with("file.gcode", 100.0, b"jpeg")
    notifier1.send_external_pause_alert.assert_called_once_with(b"jpeg")


async def test_dispatch_retry_swallows_exceptions():
    notifier1 = MagicMock()
    # Always fails
    notifier1.send_stall_alert = AsyncMock(side_effect=Exception("network error"))

    dispatcher = NotificationDispatcher([notifier1])

    # We want to patch tenacity wait so it doesn't take 60s
    import tenacity

    async def fast_retry(fn, channel_name, snapshot_id=None):
        retryer = tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(2),
            wait=tenacity.wait_fixed(0.01),
        )
        try:
            async for attempt in retryer:
                with attempt:
                    await fn()
        except Exception:
            pass

    dispatcher._with_retry = fast_retry

    dispatcher.dispatch_stall()
    await asyncio.sleep(0.05)

    # It should have tried twice and then swallowed the exception
    assert notifier1.send_stall_alert.call_count == 2


async def test_concurrent_tasks_limit():
    notifier = MagicMock()

    # Slow call that sleeps
    async def slow_call(*args, **kwargs):
        await asyncio.sleep(10.0)

    notifier.send_text = AsyncMock(side_effect=slow_call)
    dispatcher = NotificationDispatcher([notifier])

    # Fire 25 notifications
    for i in range(25):
        dispatcher.dispatch_text(f"text {i}")

    # The tasks queue size should be capped at 20
    assert len(dispatcher._tasks) == 20

    # Clean up by cancelling remaining tasks
    for task in list(dispatcher._tasks):
        task.cancel()
    await asyncio.sleep(0.01)


async def test_dispatcher_errors_and_cleanup() -> None:
    from unittest.mock import patch

    import tenacity

    notifier = MagicMock()
    # Always raise OSError
    notifier.send_detection_alert = AsyncMock(side_effect=OSError("network error"))

    dispatcher = NotificationDispatcher([notifier])

    # 1. Test persistent failure logging & storing failed snapshot ID
    orig_retry = tenacity.AsyncRetrying

    def mock_retry(*args: object, **kwargs: object) -> object:
        # Override stop to 1 attempt and wait to 0
        kwargs["stop"] = tenacity.stop_after_attempt(1)
        kwargs["wait"] = tenacity.wait_fixed(0)
        return orig_retry(*args, **kwargs)

    with patch("tenacity.AsyncRetrying", mock_retry):
        dispatcher.dispatch_detection(0.9, "snap-failed", b"jpeg")
        # Yield to let the tasks execute
        await asyncio.sleep(0.05)

    assert dispatcher.failed_channels.get("MagicMock") == "snap-failed"

    # 2. Test timeout error handling
    async def slow_call(*args: object, **kwargs: object) -> object:
        await asyncio.sleep(10.0)

    notifier.send_detection_alert = AsyncMock(side_effect=slow_call)

    dispatcher = NotificationDispatcher([notifier])
    orig_timeout = asyncio.timeout

    def mock_timeout(delay: float) -> object:
        return orig_timeout(0.01)

    with patch("asyncio.timeout", mock_timeout):
        dispatcher.dispatch_detection(0.9, "snap-timeout", b"jpeg")
        await asyncio.sleep(0.05)

    assert dispatcher.failed_channels.get("MagicMock") == "snap-timeout"

    # 3. Test the clean up done tasks path in _fire_and_forget
    dispatcher = NotificationDispatcher([notifier])
    task = asyncio.create_task(asyncio.sleep(0.0))
    await task
    dispatcher._tasks[task] = None

    # Fire any notification, which calls _fire_and_forget
    dispatcher.dispatch_text("hello")
    # This should have cleaned up the done task from _tasks
    assert task not in dispatcher._tasks
