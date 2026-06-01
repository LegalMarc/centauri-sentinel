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
    original_retry = dispatcher._with_retry
    
    async def fast_retry(fn):
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
