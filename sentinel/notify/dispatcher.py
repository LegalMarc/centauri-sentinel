"""Notification dispatcher — hides async fan-out and retry lifecycle."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol

import tenacity

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    """Any object that can send alerts to users."""

    async def send_detection_alert(
        self,
        score: float,
        snapshot_id: str | None = None,
        jpeg: bytes | None = None,
    ) -> None: ...
    async def send_stall_alert(self) -> None: ...
    async def send_camera_offline_alert(self) -> None: ...
    async def send_text(self, text: str) -> None: ...
    async def send_print_started_alert(
        self, filename: str | None, jpeg: bytes | None = None
    ) -> None: ...
    async def send_print_completed_alert(
        self, filename: str | None, elapsed_seconds: float, jpeg: bytes | None = None
    ) -> None: ...
    async def send_external_pause_alert(self, jpeg: bytes | None = None) -> None: ...


class NotificationDispatcher:
    """Dispatches notifications to multiple endpoints concurrently.

    Hides the asyncio task management and resilience/retry wrapping from the domain loop.
    Methods are strictly fire-and-forget.
    """

    def __init__(self, notifiers: list[Notifier]) -> None:
        self._notifiers = notifiers
        self._tasks: dict[asyncio.Task[None], None] = {}
        # Keep track of active failures per channel
        # Format: {channel_name: last_failed_snapshot_id}
        self.failed_channels: dict[str, str] = {}

    def _fire_and_forget(self, coro: Coroutine[Any, Any, None]) -> None:
        """Schedule a coroutine in the background, keeping a strong reference."""
        # Enforce a limit on concurrent tasks to avoid OOM when notification services are down
        MAX_CONCURRENT_TASKS = 20

        # Clean up done tasks
        done_tasks = [t for t in self._tasks if t.done()]
        for t in done_tasks:
            self._tasks.pop(t, None)

        if len(self._tasks) >= MAX_CONCURRENT_TASKS:
            logger.warning(
                "Too many concurrent notifications — dropping oldest alert "
                "to prevent task/memory leak."
            )
            oldest_task = next(iter(self._tasks))
            oldest_task.cancel()
            self._tasks.pop(oldest_task, None)

        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._tasks[task] = None
        task.add_done_callback(lambda t: self._tasks.pop(t, None))

    async def _with_retry(
        self,
        fn: Callable[[], Coroutine[Any, Any, None]],
        channel_name: str,
        snapshot_id: str | None = None,
    ) -> None:
        # The individual notifiers (Telegram, ntfy) already have their own
        # fine-grained retry logic.  This outer retry acts as a safety net for
        # transient I/O errors that escape the notifier layer.  Permanent
        # failures (invalid config, revoked tokens, HTTP 4xx) are NOT retried
        # here to avoid holding JPEG bytes in memory for minutes on a problem
        # that cannot recover on its own.
        try:
            async with asyncio.timeout(90.0):
                retryer = tenacity.AsyncRetrying(
                    wait=tenacity.wait_exponential(multiplier=0.5, min=0.5, max=60.0),
                    stop=tenacity.stop_after_attempt(5),
                    retry=tenacity.retry_if_exception_type(
                        (OSError, TimeoutError, ConnectionError)
                    ),
                    before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
                )
                try:
                    async for attempt in retryer:
                        with attempt:
                            await fn()
                    # If we reach here, it succeeded! Dismiss previous failure for this channel.
                    self.failed_channels.pop(channel_name, None)
                except Exception:
                    logger.exception("Persistent notification alert failed completely")
                    if snapshot_id:
                        self.failed_channels[channel_name] = snapshot_id
        except TimeoutError:
            logger.error("Notification task timed out after 90 seconds")
            if snapshot_id:
                self.failed_channels[channel_name] = snapshot_id

    def dispatch_detection(
        self, score: float, snapshot_id: str | None = None, jpeg: bytes | None = None
    ) -> None:
        for n in self._notifiers:
            channel_name = type(n).__name__.replace("Notifier", "")
            async def _call(n: Notifier = n) -> None:
                await n.send_detection_alert(score, snapshot_id, jpeg)
            self._fire_and_forget(self._with_retry(_call, channel_name, snapshot_id))

    def dispatch_stall(self) -> None:
        for n in self._notifiers:
            channel_name = type(n).__name__.replace("Notifier", "")
            async def _call(n: Notifier = n) -> None:
                await n.send_stall_alert()
            self._fire_and_forget(self._with_retry(_call, channel_name))

    def dispatch_camera_offline(self) -> None:
        for n in self._notifiers:
            channel_name = type(n).__name__.replace("Notifier", "")
            async def _call(n: Notifier = n) -> None:
                await n.send_camera_offline_alert()
            self._fire_and_forget(self._with_retry(_call, channel_name))

    def dispatch_text(self, text: str) -> None:
        for n in self._notifiers:
            channel_name = type(n).__name__.replace("Notifier", "")
            async def _call(n: Notifier = n) -> None:
                await n.send_text(text)
            self._fire_and_forget(self._with_retry(_call, channel_name))

    def dispatch_print_started(self, filename: str | None, jpeg: bytes | None = None) -> None:
        for n in self._notifiers:
            channel_name = type(n).__name__.replace("Notifier", "")
            async def _call(n: Notifier = n) -> None:
                await n.send_print_started_alert(filename, jpeg)
            self._fire_and_forget(self._with_retry(_call, channel_name))

    def dispatch_print_completed(
        self, filename: str | None, elapsed_seconds: float, jpeg: bytes | None = None
    ) -> None:
        for n in self._notifiers:
            channel_name = type(n).__name__.replace("Notifier", "")
            async def _call(n: Notifier = n) -> None:
                await n.send_print_completed_alert(filename, elapsed_seconds, jpeg)
            self._fire_and_forget(self._with_retry(_call, channel_name))

    def dispatch_external_pause(self, jpeg: bytes | None = None) -> None:
        for n in self._notifiers:
            channel_name = type(n).__name__.replace("Notifier", "")
            async def _call(n: Notifier = n) -> None:
                await n.send_external_pause_alert(jpeg)
            self._fire_and_forget(self._with_retry(_call, channel_name))
