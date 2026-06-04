"""Telegram notifier — sends rich alerts with inline keyboard.

Security: only messages from authorized chat_id AND user_id are processed.
The bot token and chat/user IDs are set in settings.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import tenacity
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

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
        self._settings = settings
        self._enabled = settings.telegram_enabled
        if not self._enabled:
            return

        self._bot = Bot(token=settings.telegram_bot_token or "")
        self._chat_id = settings.telegram_chat_id or ""
        self._allowed_users = _parse_user_ids(settings.telegram_user_ids)
        self._snapshots_dir = Path(settings.db_path).parent / "snapshots"
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

    async def send_detection_alert(
        self,
        score: float,
        snapshot_id: str | None = None,
        jpeg: bytes | None = None,
    ) -> None:
        if not self._enabled:
            return

        photo_bytes = jpeg
        if not photo_bytes and snapshot_id and self._settings.telegram_send_snapshots:
            p = self._snapshots_dir / f"{snapshot_id}.jpg"
            if p.exists():
                try:
                    photo_bytes = await asyncio.to_thread(p.read_bytes)
                except OSError:
                    logger.exception("Failed to read snapshot file for Telegram: %s", p)

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Resume", callback_data="resume"),
                    InlineKeyboardButton("Stop", callback_data="stop"),
                    InlineKeyboardButton("Snooze 10m", callback_data="snooze"),
                ]
            ]
        )

        caption = (
            f"⚠️ Failure detected — confidence {score:.0%}\n"
            f"(Hint: if false positive, consider raising ML_SCORE_THRESHOLD, "
            f"currently {self._settings.ml_score_threshold})"
        )

        if photo_bytes and self._settings.telegram_send_snapshots:

            async def _attempt_photo() -> None:
                await self._bot.send_photo(
                    chat_id=self._chat_id,
                    photo=photo_bytes,
                    caption=caption,
                    reply_markup=keyboard,
                    read_timeout=_TIMEOUT,
                    write_timeout=_TIMEOUT,
                    connect_timeout=_TIMEOUT,
                )

            await self._send_with_retry_fn(_attempt_photo)
        else:
            text_to_send = caption
            if self._settings.telegram_send_snapshots:
                text_to_send += "\n(Snapshot not available)"

            async def _attempt_msg() -> None:
                await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=text_to_send,
                    reply_markup=keyboard,
                    read_timeout=_TIMEOUT,
                    write_timeout=_TIMEOUT,
                    connect_timeout=_TIMEOUT,
                )

            await self._send_with_retry_fn(_attempt_msg)

    async def send_stall_alert(self) -> None:
        if not self._enabled:
            return

        async def _send() -> None:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text="⚠️ Sentinel watcher stalled — please check the service.",
                read_timeout=_TIMEOUT,
                write_timeout=_TIMEOUT,
                connect_timeout=_TIMEOUT,
            )

        await self._send_with_retry_fn(_send)

    async def send_camera_offline_alert(self) -> None:
        if not self._enabled:
            return

        async def _send() -> None:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text="📷 Camera offline — detection suspended.",
                read_timeout=_TIMEOUT,
                write_timeout=_TIMEOUT,
                connect_timeout=_TIMEOUT,
            )

        await self._send_with_retry_fn(_send)

    async def send_text(self, text: str) -> None:
        if not self._enabled:
            return

        async def _send() -> None:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                read_timeout=_TIMEOUT,
                write_timeout=_TIMEOUT,
                connect_timeout=_TIMEOUT,
            )

        await self._send_with_retry_fn(_send)

    async def _send_with_retry_fn(self, fn: Callable[[], Awaitable[None]]) -> None:
        from telegram.error import NetworkError, RetryAfter, TimedOut

        # Only retry on transient network-level errors.
        # Permanent failures (invalid token, chat not found, etc.) are not
        # retried — they will re-raise immediately so the watcher loop can log
        # them without spending 3x the send time on certain failures.
        retryer = tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(_RETRIES),
            wait=tenacity.wait_exponential(multiplier=0.5, min=0.5, max=8),
            retry=tenacity.retry_if_exception_type((NetworkError, TimedOut, RetryAfter)),
            reraise=True,
        )
        async for attempt in retryer:
            with attempt:
                await fn()

    async def send_print_started_alert(
        self, filename: str | None, jpeg: bytes | None = None
    ) -> None:
        if not self._enabled:
            return

        name = filename or "Unknown file"
        caption = f"🚀 Print started: {name}"

        async def _send() -> None:
            if jpeg and self._settings.telegram_send_snapshots:
                try:
                    await self._bot.send_photo(
                        chat_id=self._chat_id,
                        photo=jpeg,
                        caption=caption,
                        read_timeout=_TIMEOUT,
                        write_timeout=_TIMEOUT,
                        connect_timeout=_TIMEOUT,
                    )
                except Exception:
                    logger.exception("Telegram photo send failed for print start")
                    await self._bot.send_message(
                        chat_id=self._chat_id,
                        text=caption,
                        read_timeout=_TIMEOUT,
                        write_timeout=_TIMEOUT,
                        connect_timeout=_TIMEOUT,
                    )
            else:
                await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=caption,
                    read_timeout=_TIMEOUT,
                    write_timeout=_TIMEOUT,
                    connect_timeout=_TIMEOUT,
                )

        await self._send_with_retry_fn(_send)

    async def send_print_completed_alert(
        self, filename: str | None, elapsed_seconds: float, jpeg: bytes | None = None
    ) -> None:
        if not self._enabled:
            return

        name = filename or "Unknown file"
        hours = int(elapsed_seconds // 3600)
        minutes = int((elapsed_seconds % 3600) // 60)
        time_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
        caption = f"✅ Print completed: {name}\nTime: {time_str}"

        async def _send() -> None:
            if jpeg and self._settings.telegram_send_snapshots:
                try:
                    await self._bot.send_photo(
                        chat_id=self._chat_id,
                        photo=jpeg,
                        caption=caption,
                        read_timeout=_TIMEOUT,
                        write_timeout=_TIMEOUT,
                        connect_timeout=_TIMEOUT,
                    )
                except Exception:
                    logger.exception("Telegram photo send failed for print completed")
                    await self._bot.send_message(
                        chat_id=self._chat_id,
                        text=caption,
                        read_timeout=_TIMEOUT,
                        write_timeout=_TIMEOUT,
                        connect_timeout=_TIMEOUT,
                    )
            else:
                await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=caption,
                    read_timeout=_TIMEOUT,
                    write_timeout=_TIMEOUT,
                    connect_timeout=_TIMEOUT,
                )

        await self._send_with_retry_fn(_send)

    async def send_external_pause_alert(self, jpeg: bytes | None = None) -> None:
        if not self._enabled:
            return

        caption = "⏸️ Printer paused externally (possible filament runout or manual pause)."

        async def _send() -> None:
            if jpeg and self._settings.telegram_send_snapshots:
                try:
                    await self._bot.send_photo(
                        chat_id=self._chat_id,
                        photo=jpeg,
                        caption=caption,
                        read_timeout=_TIMEOUT,
                        write_timeout=_TIMEOUT,
                        connect_timeout=_TIMEOUT,
                    )
                except Exception:
                    logger.exception("Telegram photo send failed for external pause")
                    await self._bot.send_message(
                        chat_id=self._chat_id,
                        text=caption,
                        read_timeout=_TIMEOUT,
                        write_timeout=_TIMEOUT,
                        connect_timeout=_TIMEOUT,
                    )
            else:
                await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=caption,
                    read_timeout=_TIMEOUT,
                    write_timeout=_TIMEOUT,
                    connect_timeout=_TIMEOUT,
                )

        await self._send_with_retry_fn(_send)
