"""MJPEG single-frame grabber with reconnection and backoff.

Protocol: the printer streams multipart/x-mixed-replace over HTTP.
Each part is delimited by a boundary and contains a JPEG image.
We detect JPEG by scanning for SOI (\\xFF\\xD8) and EOI (\\xFF\\xD9) markers.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

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


def _extract_jpeg(buf: bytes) -> bytes | None:
    """Return the first complete JPEG from *buf*, or None if incomplete."""
    start = buf.find(_SOI)
    if start == -1:
        return None
    end = buf.find(_EOI, start)
    if end == -1:
        return None
    return buf[start : end + 2]


class MjpegGrabber:
    """Grabs single JPEG frames from an MJPEG HTTP stream."""

    def __init__(self, settings: Settings) -> None:
        url = (
            f"http://{settings.printer_ip}:{settings.printer_mjpeg_port}"
            f"{settings.printer_mjpeg_path}"
        )
        self._url = url
        self._consecutive_failures = 0
        self._last_success: datetime | None = None

    @property
    def last_success_utc(self) -> datetime | None:
        """UTC timestamp of the last successful grab, or None."""
        return self._last_success

    async def grab(self) -> bytes:
        """Return one complete JPEG frame.

        Raises CameraReadError on a single failure.
        Raises CameraOfflineError after _OFFLINE_THRESHOLD consecutive failures.
        """
        try:
            frame = await self._grab_once()
            self._consecutive_failures = 0
            self._last_success = datetime.now(tz=UTC)
            return frame
        except CameraReadError:
            self._consecutive_failures += 1
            logger.warning("Camera grab failed (consecutive=%d)", self._consecutive_failures)
            if self._consecutive_failures >= _OFFLINE_THRESHOLD:
                raise CameraOfflineError(
                    f"Camera offline after {self._consecutive_failures} consecutive failures"
                ) from None
            raise

    async def _grab_once(self) -> bytes:
        """Open the stream, read until a complete JPEG is found, then close."""
        try:
            async with (
                httpx.AsyncClient(timeout=_READ_TIMEOUT) as client,
                client.stream("GET", self._url) as resp,
            ):
                resp.raise_for_status()
                buf = b""
                async with asyncio.timeout(_READ_TIMEOUT):
                    async for chunk in resp.aiter_bytes(_CHUNK_SIZE):
                        buf += chunk
                        frame = _extract_jpeg(buf)
                        if frame is not None:
                            return frame
        except (TimeoutError, httpx.HTTPError, OSError) as exc:
            raise CameraReadError(f"Grab failed: {exc}") from exc

        raise CameraReadError("Stream ended without a complete JPEG frame")

    async def stream_proxy(self) -> AsyncIterator[bytes]:
        """Yield JPEG frames continuously with exponential backoff on failure."""
        delay = _BACKOFF_BASE
        while True:
            try:
                frame = await self.grab()
                delay = _BACKOFF_BASE  # reset on success
                yield frame
            except CameraOfflineError:
                raise
            except CameraReadError:
                logger.debug("Backing off %.1fs before retry", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, _BACKOFF_CAP)
