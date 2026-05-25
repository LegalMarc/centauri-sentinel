"""Telegram notifier — sends rich alerts with inline keyboard.

Security: only messages from authorized chat_id AND user_id are processed.
The bot token and chat/user IDs are set in settings.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import tenacity
from telegram import Bot

if TYPE_CHECKING:
    from sentinel.config import Settings

logger = logging.getLogger(__name__)

_TIMEOUT = 10
_RETRIES = 3


def _parse_user_ids(raw: str | None) -> frozenset[int]:
    if not raw:
        return frozenset()
    result = set()
    for part in raw.split(","):
        try:
            result.add(int(part.strip()))
        except ValueError:
            logger.warning("Invalid telegram_user_ids entry: %r (raw=%r)", part, raw)
    return frozenset(result)


class TelegramNotifier:
    """Sends Telegram alerts; no-op when telegram_enabled=False."""

    def __init__(self, settings: Settings) -> None:
        self._enabled = settings.telegram_enabled
        if not self._enabled:
            return

        self._bot = Bot(token=settings.telegram_bot_token or "")
        self._chat_id = settings.telegram_chat_id or ""
        self._allowed_users = _parse_user_ids(settings.telegram_user_ids)
        if not self._allowed_users:
            raise ValueError(
                "TELEGRAM_USER_IDS is required when Telegram is enabled but is empty or "
                "contains no valid integer IDs. Bot command authorization will deny everyone. "
                f"Raw value: {settings.telegram_user_ids!r}"
            )

    def is_authorized(self, chat_id: int | str, user_id: int) -> bool:
        """Return True iff chat_id matches and user_id is in the allowlist."""
        if not self._enabled:
            return False
        return str(chat_id) == str(self._chat_id) and user_id in self._allowed_users

    async def send_detection_alert(self, score: float, snapshot_id: str | None = None) -> None:
        if not self._enabled:
            return
        text = f"⚠️ Failure detected — confidence {score:.0%}\nSnapshot: {snapshot_id or 'N/A'}"
        await self._send_with_retry(text)

    async def send_stall_alert(self) -> None:
        if not self._enabled:
            return
        await self._send_with_retry("⚠️ Sentinel watcher stalled — please check the service.")

    async def send_camera_offline_alert(self) -> None:
        if not self._enabled:
            return
        await self._send_with_retry("📷 Camera offline — detection suspended.")

    async def send_text(self, text: str) -> None:
        if not self._enabled:
            return
        await self._send_with_retry(text)

    async def _send_with_retry(self, text: str) -> None:
        async def _attempt() -> None:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                read_timeout=_TIMEOUT,
                write_timeout=_TIMEOUT,
                connect_timeout=_TIMEOUT,
            )

        retryer = tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(_RETRIES),
            wait=tenacity.wait_exponential(multiplier=0.5, min=0.5, max=8),
            retry=tenacity.retry_if_exception_type(Exception),
            reraise=True,
        )
        async for attempt in retryer:
            with attempt:
                await _attempt()
