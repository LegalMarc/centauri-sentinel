"""Notification dispatcher — hides async fan-out and retry lifecycle."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

import tenacity

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
        self._tasks: set[asyncio.Task[None]] = set()

    def _fire_and_forget(self, coro: Awaitable[None]) -> None:
        """Schedule a coroutine in the background, keeping a strong reference."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _with_retry(self, fn: Callable[[], Awaitable[None]]) -> None:
        retryer = tenacity.AsyncRetrying(
            wait=tenacity.wait_exponential(multiplier=0.5, min=0.5, max=60.0),
            before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
        )
        try:
            async for attempt in retryer:
                with attempt:
                    await fn()
        except Exception:
            logger.exception("Persistent notification alert failed completely")

    def dispatch_detection(self, score: float, snapshot_id: str | None = None, jpeg: bytes | None = None) -> None:
        for n in self._notifiers:
            self._fire_and_forget(self._with_retry(lambda n=n: n.send_detection_alert(score, snapshot_id, jpeg)))

    def dispatch_stall(self) -> None:
        for n in self._notifiers:
            self._fire_and_forget(self._with_retry(lambda n=n: n.send_stall_alert()))

    def dispatch_camera_offline(self) -> None:
        for n in self._notifiers:
            self._fire_and_forget(self._with_retry(lambda n=n: n.send_camera_offline_alert()))

    def dispatch_text(self, text: str) -> None:
        for n in self._notifiers:
            self._fire_and_forget(self._with_retry(lambda n=n: n.send_text(text)))

    def dispatch_print_started(self, filename: str | None, jpeg: bytes | None = None) -> None:
        for n in self._notifiers:
            self._fire_and_forget(self._with_retry(lambda n=n: n.send_print_started_alert(filename, jpeg)))

    def dispatch_print_completed(self, filename: str | None, elapsed_seconds: float, jpeg: bytes | None = None) -> None:
        for n in self._notifiers:
            self._fire_and_forget(self._with_retry(lambda n=n: n.send_print_completed_alert(filename, elapsed_seconds, jpeg)))

    def dispatch_external_pause(self, jpeg: bytes | None = None) -> None:
        for n in self._notifiers:
            self._fire_and_forget(self._with_retry(lambda n=n: n.send_external_pause_alert(jpeg)))
