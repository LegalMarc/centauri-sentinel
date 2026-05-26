"""ntfy notifier — POSTs to a self-hosted or public ntfy server."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import tenacity

if TYPE_CHECKING:
    from sentinel.config import Settings

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0
_RETRIES = 3


class NtfyNotifier:
    """Sends ntfy push notifications; no-op when ntfy_enabled=False."""

    def __init__(self, settings: Settings) -> None:
        self._enabled = settings.ntfy_enabled
        self._url = settings.ntfy_url or ""
        self._token = settings.ntfy_token
        self._snapshots_dir = Path(settings.db_path).parent / "snapshots"

    async def send_detection_alert(
        self,
        score: float,
        snapshot_id: str | None = None,
        jpeg: bytes | None = None,
    ) -> None:
        if not self._enabled:
            return

        photo_bytes = jpeg
        if not photo_bytes and snapshot_id:
            p = self._snapshots_dir / f"{snapshot_id}.jpg"
            if p.exists():
                try:
                    photo_bytes = await asyncio.to_thread(p.read_bytes)
                except OSError:
                    logger.exception("Failed to read snapshot file for ntfy: %s", p)

        await self._post(
            title="Failure detected",
            message=f"Confidence {score:.0%}.",
            priority="high",
            tags=["warning"],
            jpeg=photo_bytes,
        )

    async def send_stall_alert(self) -> None:
        if not self._enabled:
            return
        await self._post(
            title="Sentinel stalled",
            message="Watcher heartbeat stopped. Please check the service.",
            priority="high",
            tags=["warning"],
        )

    async def send_camera_offline_alert(self) -> None:
        if not self._enabled:
            return
        await self._post(
            title="Camera offline",
            message="Camera is unreachable. Detection suspended.",
            priority="default",
            tags=["camera"],
        )

    async def _post(
        self,
        *,
        title: str,
        message: str,
        priority: str = "default",
        tags: list[str] | None = None,
        jpeg: bytes | None = None,
    ) -> None:
        headers: dict[str, str] = {
            "Title": title,
            "Priority": priority,
            "Tags": ",".join(tags or []),
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        if jpeg:
            headers["X-Message"] = message
            headers["X-Filename"] = "snapshot.jpg"
            content: bytes | str = jpeg
        else:
            content = message

        retryer = tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(_RETRIES),
            wait=tenacity.wait_exponential(multiplier=0.5, min=0.5, max=8),
            retry=tenacity.retry_if_exception_type(httpx.RequestError),
            reraise=True,
        )
        async for attempt in retryer:
            with attempt:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.post(self._url, content=content, headers=headers)
                    resp.raise_for_status()
