"""ntfy notifier — POSTs to a self-hosted or public ntfy server."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import tenacity

from sentinel.network import validate_https

if TYPE_CHECKING:
    from sentinel.config import Settings

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0
_RETRIES = 3


class NtfyNotifier:
    """Sends ntfy push notifications; no-op when ntfy_enabled=False."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._enabled = settings.ntfy_enabled

        url = settings.ntfy_url or ""
        if self._enabled and url:
            url = validate_https(url)

        self._url = url
        self._token = settings.ntfy_token
        self._snapshots_dir = Path(settings.db_path).parent / "snapshots"
        self._client = httpx.AsyncClient(timeout=_TIMEOUT)

        if self._enabled and "ntfy.sh" in self._url.lower() and not self._token:
            raise ValueError(
                "PRIVACY RISK: Using public ntfy.sh without an auth token is blocked. "
                "Anyone who guesses your topic URL can view your 3D printer snapshots. "
                "Please configure an auth token or use a self-hosted instance."
            )

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
            message=(
                f"Confidence {score:.0%}.\n(Hint: if false positive, "
                f"raise ML_SCORE_THRESHOLD, currently {self._settings.ml_score_threshold})"
            ),
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

    async def send_text(self, text: str) -> None:
        if not self._enabled:
            return
        await self._post(
            title="Sentinel Alert",
            message=text,
            priority="default",
            tags=["information_source"],
        )

    async def send_print_started_alert(
        self, filename: str | None, jpeg: bytes | None = None
    ) -> None:
        if not self._enabled:
            return
        name = filename or "Unknown file"
        await self._post(
            title="Print Started",
            message=f"Started printing: {name}",
            priority="default",
            tags=["rocket"],
            jpeg=jpeg,
        )

    async def send_print_completed_alert(
        self, filename: str | None, elapsed_seconds: float, jpeg: bytes | None = None
    ) -> None:
        if not self._enabled:
            return
        name = filename or "Unknown file"
        hours = int(elapsed_seconds // 3600)
        minutes = int((elapsed_seconds % 3600) // 60)
        time_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
        await self._post(
            title="Print Completed",
            message=f"Completed {name} in {time_str}.",
            priority="default",
            tags=["white_check_mark"],
            jpeg=jpeg,
        )

    async def send_external_pause_alert(self, jpeg: bytes | None = None) -> None:
        if not self._enabled:
            return
        await self._post(
            title="Printer Paused",
            message="Printer paused externally (possible filament runout or manual pause).",
            priority="high",
            tags=["pause_button"],
            jpeg=jpeg,
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
        import base64

        def _encode_header(val: str) -> str:
            if any(ord(c) < 32 or ord(c) > 126 for c in val):
                encoded = base64.b64encode(val.encode("utf-8")).decode("utf-8")
                return f"=?utf-8?B?{encoded}?="
            return val

        def _sanitize(val: str) -> str:
            return val.replace("\r", " ").replace("\n", " ").strip()

        headers: dict[str, str] = {
            "Title": _encode_header(title),
            "Priority": _sanitize(priority),
            "Tags": _sanitize(",".join(tags or [])),
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        if jpeg:
            headers["X-Message"] = _encode_header(message)
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
                resp = await self._client.post(self._url, content=content, headers=headers)
                resp.raise_for_status()

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
