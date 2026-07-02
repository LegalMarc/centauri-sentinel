"""MJPEG single-frame grabber with reconnection and backoff.

Protocol: the printer streams multipart/x-mixed-replace over HTTP.
Each part is delimited by a boundary and contains a JPEG image.
We detect JPEG by scanning for SOI (\\xFF\\xD8) and EOI (\\xFF\\xD9) markers.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from sentinel.camera.errors import CameraOfflineError, CameraReadError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sentinel.config import Settings

logger = logging.getLogger(__name__)

_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"
_CHUNK_SIZE = 8192
_OFFLINE_THRESHOLD = 3
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 30.0
_READ_TIMEOUT = 10.0
_MAX_BUF_BYTES = 10 * 1024 * 1024  # 10 MB limit to prevent OOM on unbounded stream


def _extract_jpeg(buf: bytes) -> bytes | None:
    """Return the first complete JPEG from *buf*, or None if incomplete."""
    start = buf.find(_SOI)
    if start == -1:
        return None
    end = buf.find(_EOI, start)
    if end == -1:
        return None
    return buf[start : end + 2]


def _format_host_for_url(host: str) -> str:
    """Bracket an IPv6 literal for embedding in a URL netloc (e.g. "::1" -> "[::1]").

    Hostnames and IPv4 literals are returned unchanged.
    """
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(addr, ipaddress.IPv6Address):
        return f"[{host}]"
    return host


class MjpegGrabber:
    """Grabs single JPEG frames from an MJPEG HTTP stream."""

    def __init__(self, settings: Settings) -> None:
        host = _format_host_for_url(settings.printer_ip)
        url = f"http://{host}:{settings.printer_mjpeg_port}{settings.printer_mjpeg_path}"
        self._settings = settings
        self._url = url
        self._consecutive_failures = 0
        self._last_success: datetime | None = None
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=_READ_TIMEOUT, write=5.0, pool=5.0)
        )
        self._listeners: set[asyncio.Queue[Any]] = set()
        self._broadcaster_task: asyncio.Task[None] | None = None
        self._latest_frame: bytes | None = None
        self._latest_frame_time: float = 0.0
        self._last_grab_time: float = 0.0
        self._latest_exception: Exception | None = None

    @property
    def is_connected(self) -> bool:
        """Return True if the camera has not exceeded the offline threshold."""
        return self._consecutive_failures < _OFFLINE_THRESHOLD

    @property
    def last_success_utc(self) -> datetime | None:
        """UTC timestamp of the last successful grab, or None."""
        return self._last_success

    async def grab(self) -> bytes:
        """Return one complete JPEG frame.

        Uses a persistent background task to grab frames continuously,
        minimizing TCP connection churn.
        """
        self._last_grab_time = time.monotonic()

        # Start broadcaster task if not already running
        if self._broadcaster_task is None or self._broadcaster_task.done():
            self._latest_frame = None
            self._latest_exception = None
            self._broadcaster_task = asyncio.create_task(self._broadcast_loop())

        # Wait for the first frame if none has been received yet
        try:
            async with asyncio.timeout(_READ_TIMEOUT):
                while self._latest_frame is None and self._latest_exception is None:
                    # Capture into a local once per iteration: a concurrent close()/
                    # reconfigure() can set self._broadcaster_task = None between
                    # iterations (after the await below), so we must null-check the
                    # freshly-read local rather than re-reading the attribute into
                    # .done() directly.
                    task = self._broadcaster_task
                    if task is None or task.done():
                        # If the task failed/finished, check for exception
                        task_exc: BaseException | None = None
                        if task is not None:
                            try:
                                task_exc = task.exception()
                            except asyncio.CancelledError as exc:
                                raise CameraReadError("Broadcaster task was cancelled") from exc
                        if task_exc:
                            raise CameraReadError(f"Stream failed: {task_exc}") from task_exc
                        raise CameraReadError("Stream ended before first frame")
                    await asyncio.sleep(0.05)
        except TimeoutError as exc:
            self._consecutive_failures += 1
            if self._consecutive_failures >= _OFFLINE_THRESHOLD:
                raise CameraOfflineError(
                    f"Camera offline after {self._consecutive_failures} consecutive failures"
                ) from exc
            raise CameraReadError("Timeout waiting for camera frame") from exc

        # Check if the broadcaster task has failed recently. Re-read into a local
        # and null-check it — close()/reconfigure() may have concurrently cleared
        # self._broadcaster_task while we were awaiting above.
        task = self._broadcaster_task
        if task is not None and task.done():
            try:
                task_exc = task.exception()
            except asyncio.CancelledError as exc:
                raise CameraReadError("Broadcaster task was cancelled") from exc
            if task_exc:
                self._consecutive_failures += 1
                if self._consecutive_failures >= _OFFLINE_THRESHOLD:
                    raise CameraOfflineError(
                        f"Camera offline after {self._consecutive_failures} consecutive failures"
                    ) from task_exc
                raise CameraReadError(f"Stream failed: {task_exc}") from task_exc

        if self._latest_exception is not None:
            latest_exc = self._latest_exception
            self._latest_exception = None  # clear
            is_offline = self._consecutive_failures >= _OFFLINE_THRESHOLD or isinstance(
                latest_exc, CameraOfflineError
            )
            if is_offline:
                raise CameraOfflineError(
                    f"Camera offline after {self._consecutive_failures} consecutive failures"
                ) from latest_exc
            if isinstance(latest_exc, CameraReadError):
                raise latest_exc
            raise CameraReadError(f"Grab failed: {latest_exc}") from latest_exc

        self._consecutive_failures = 0
        self._last_success = datetime.now(tz=UTC)
        if self._latest_frame is None:
            raise CameraReadError("No camera frame available")
        return self._latest_frame

    async def _stream_proxy_internal(self) -> AsyncIterator[bytes]:
        """Yield JPEG frames continuously using a single persistent connection.

        Attempts to reconnect with exponential backoff if the stream disconnects.
        """
        import urllib.parse

        from sentinel.network import resolve_and_validate_printer_ip

        delay = _BACKOFF_BASE
        while True:
            try:
                parsed = urllib.parse.urlparse(self._url)
                if parsed.hostname:
                    resolved_ip = await resolve_and_validate_printer_ip(parsed.hostname)
                    netloc = _format_host_for_url(resolved_ip)
                    if parsed.port is not None:
                        netloc = f"{netloc}:{parsed.port}"
                    url_to_fetch = urllib.parse.urlunparse(
                        (
                            parsed.scheme,
                            netloc,
                            parsed.path,
                            parsed.params,
                            parsed.query,
                            parsed.fragment,
                        )
                    )
                else:
                    url_to_fetch = self._url

                async with self._client.stream("GET", url_to_fetch) as resp:
                    resp.raise_for_status()
                    buf = b""
                    search_offset = 0

                    aiter = resp.aiter_bytes(_CHUNK_SIZE)
                    frame_start_time = time.monotonic()
                    while True:
                        try:
                            remaining = _READ_TIMEOUT - (time.monotonic() - frame_start_time)
                            if remaining <= 0:
                                raise TimeoutError("Timeout waiting for complete frame")
                            async with asyncio.timeout(remaining):
                                chunk = await aiter.__anext__()
                        except StopAsyncIteration:
                            break

                        buf += chunk
                        if len(buf) > _MAX_BUF_BYTES:
                            raise CameraReadError("Buffer size limit exceeded 10 MB")
                        while True:
                            start = buf.find(_SOI)
                            if start == -1:
                                break
                            eoi_search_start = max(start, search_offset)
                            end = buf.find(_EOI, eoi_search_start)
                            if end == -1:
                                search_offset = max(0, len(buf) - 2)
                                break
                            frame = buf[start : end + 2]
                            self._consecutive_failures = 0
                            self._last_success = datetime.now(tz=UTC)
                            delay = _BACKOFF_BASE
                            yield frame
                            buf = buf[end + 2 :]
                            search_offset = 0
                            frame_start_time = time.monotonic()
                    self._latest_frame = None
                    raise ConnectionError("Stream ended prematurely")
            except CameraReadError as exc:
                # A single-grab error raised locally (e.g. the buffer-overflow guard
                # above) — mirror the bookkeeping of the block below (so repeated
                # failures still trip the offline threshold) but re-raise immediately
                # instead of retrying internally, since the caller (grab()/
                # stream_proxy()) should see each such failure as it happens.
                self._consecutive_failures += 1
                self._latest_exception = exc
                self._latest_frame = None
                logger.warning(
                    "Stream proxy connection failed: %s (consecutive=%d)",
                    exc,
                    self._consecutive_failures,
                )
                if self._consecutive_failures >= _OFFLINE_THRESHOLD:
                    raise CameraOfflineError(
                        f"Camera offline after {self._consecutive_failures} consecutive failures"
                    ) from exc
                raise
            except (TimeoutError, httpx.HTTPError, OSError, ValueError) as exc:
                self._consecutive_failures += 1
                self._latest_exception = exc
                self._latest_frame = None
                logger.warning(
                    "Stream proxy connection failed: %s (consecutive=%d)",
                    exc,
                    self._consecutive_failures,
                )
                if self._consecutive_failures >= _OFFLINE_THRESHOLD:
                    raise CameraOfflineError(
                        f"Camera offline after {self._consecutive_failures} consecutive failures"
                    ) from exc
                logger.debug("Backing off %.1fs before reconnecting", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, _BACKOFF_CAP)
            except asyncio.CancelledError:
                raise

    async def _broadcast_loop(self) -> None:
        while True:
            try:
                async for frame in self._stream_proxy_internal():
                    self._latest_frame = frame
                    self._latest_frame_time = time.monotonic()
                    self._latest_exception = None

                    for q in list(self._listeners):
                        if q.full():
                            with contextlib.suppress(asyncio.QueueEmpty):
                                q.get_nowait()
                        with contextlib.suppress(asyncio.QueueFull):
                            q.put_nowait(frame)

                    # Yield to the event loop so listener consumer tasks get a chance
                    # to drain their queue before the next frame in a burst arrives —
                    # otherwise a buffer containing several complete frames back-to-back
                    # would push them all in before any consumer is scheduled, and the
                    # maxsize=2 "drop oldest" logic would evict frames unnecessarily.
                    await asyncio.sleep(0)

                    # Idle check: if no grabs or stream proxy listeners for 30s, shut down stream
                    if not self._listeners and (time.monotonic() - self._last_grab_time > 30.0):
                        logger.info("Camera stream idle for 30s — shutting down connection")
                        self._latest_frame = None
                        return
            except asyncio.CancelledError:
                self._latest_frame = None
                raise
            except Exception as exc:
                self._latest_frame = None
                self._latest_exception = exc
                for q in list(self._listeners):
                    if q.full():
                        with contextlib.suppress(asyncio.QueueEmpty):
                            q.get_nowait()
                    with contextlib.suppress(asyncio.QueueFull):
                        q.put_nowait(exc)
                if isinstance(exc, CameraOfflineError):
                    raise
                await asyncio.sleep(1)

    async def stream_proxy(self) -> AsyncIterator[bytes]:
        if len(self._listeners) >= self._settings.camera_max_streams:
            raise CameraReadError("Max concurrent stream proxies reached")

        q: asyncio.Queue[bytes | Exception] = asyncio.Queue(maxsize=2)
        self._listeners.add(q)
        if self._broadcaster_task is None or self._broadcaster_task.done():
            # Mirror grab()'s restart handling: clear stale state so a concurrent
            # grab() doesn't see a leftover frame/exception from the dead task.
            self._latest_frame = None
            self._latest_exception = None
            self._broadcaster_task = asyncio.create_task(self._broadcast_loop())

        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=1.0)
                except TimeoutError:
                    continue

                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            self._listeners.discard(q)

    async def reconfigure(self, url: str) -> None:
        """Update the camera URL and restart the connection."""
        self._url = url
        await self.close()
        # Recreate the HTTP client so subsequent grab()/stream_proxy() calls
        # do not raise "Cannot send a request, as the client has been closed".
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=_READ_TIMEOUT, write=5.0, pool=5.0)
        )

    async def close(self) -> None:
        """Cancel the broadcaster task and clean up listeners."""
        if self._broadcaster_task and not self._broadcaster_task.done():
            self._broadcaster_task.cancel()
        self._broadcaster_task = None
        from sentinel.camera.errors import CameraClosedError

        sentinel = CameraClosedError("Camera closed/reconfigured")
        for q in list(self._listeners):
            # Guarantee sentinel delivery: if queue is full, drop one frame to make room.
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(sentinel)
        self._listeners.clear()

        await self._client.aclose()
