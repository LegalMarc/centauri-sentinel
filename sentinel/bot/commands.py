"""Telegram bot command and callback handlers.

All commands require `is_authorized()` to pass (correct chat_id AND user_id).
Unauthorized messages are silently dropped with a WARNING log — the bot
never replies to unknown senders.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

_background_tasks: set[asyncio.Task[None]] = set()

if TYPE_CHECKING:
    from telegram import Update

    from sentinel.config import Settings
    from sentinel.db.repo import Database
    from sentinel.notify.telegram import TelegramNotifier

logger = logging.getLogger(__name__)

_STOP_CONFIRM_WINDOW = 30.0  # seconds


class BotCommandHandler:
    """Handles all bot commands and inline keyboard callbacks."""

    def __init__(
        self,
        settings: Settings,
        printer: Any,
        camera: Any,
        db: Database,
        watcher: Any,
        notifier: TelegramNotifier,
        *,
        snooze_seconds: float = 600.0,
    ) -> None:
        self._settings = settings
        self._printer = printer
        self._camera = camera
        self._db = db
        self._watcher = watcher
        self._notifier = notifier
        self._snooze_seconds = snooze_seconds
        # Maps user_id → timestamp of /stop command; cleared after confirm or expiry
        self._pending_stops: dict[int, float] = {}

    # ------------------------------------------------------------------
    # Auth guard
    # ------------------------------------------------------------------

    def _authorized(self, update: Update) -> bool:
        if update.message is not None:
            user = update.message.from_user
            chat_id: int | str = update.message.chat_id
        elif update.callback_query is not None:
            user = update.callback_query.from_user
            cq_msg = update.callback_query.message
            if cq_msg is None:
                return False
            chat_id = cq_msg.chat.id
        else:
            return False

        if user is None:
            return False

        authorized = self._notifier.is_authorized(chat_id, user.id)
        if not authorized:
            logger.warning(
                "Unauthorized Telegram interaction — chat=%s user=%s",
                str(chat_id)[:4] + "***",
                str(user.id)[:3] + "***",
            )
        return authorized

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def cmd_help(self, update: Update, context: Any) -> None:
        if not self._authorized(update):
            return
        assert update.message is not None
        await update.message.reply_text(
            "/status — watcher state + last detection\n"
            "/snapshot — current camera frame\n"
            "/pause — pause the print\n"
            "/resume — resume the print\n"
            "/stop — cancel the print (requires /confirm within 30 s)\n"
            "/confirm — confirm /stop\n"
            "/enable — enable failure detection\n"
            "/disable — disable failure detection\n"
            "/help — this message"
        )

    async def cmd_status(self, update: Update, context: Any) -> None:
        if not self._authorized(update):
            return
        assert update.message is not None
        heartbeat = await self._db.get_heartbeat()
        detection_enabled = await self._db.get_setting("detection_enabled", "true")
        recent = await self._db.get_recent_detections(limit=1)
        last_det = recent[0] if recent else None

        lines = [
            f"Watcher: {self._watcher.state.name}",
            f"Detection: {'enabled' if detection_enabled == 'true' else 'disabled'}",
            f"Last heartbeat: {heartbeat or 'never'}",
        ]
        if last_det:
            lines.append(f"Last detection: score={last_det['score']:.2f} at {last_det['ts']}")
        await update.message.reply_text("\n".join(lines))

    async def cmd_snapshot(self, update: Update, context: Any) -> None:
        if not self._authorized(update):
            return
        assert update.message is not None
        try:
            jpeg = await self._camera.grab()
            await update.message.reply_photo(photo=jpeg)
        except Exception:
            logger.exception("Failed to grab snapshot for Telegram")
            await update.message.reply_text("Camera unavailable.")

    async def cmd_pause(self, update: Update, context: Any) -> None:
        if not self._authorized(update):
            return
        assert update.message is not None
        try:
            await self._printer.pause()
            await self._db.record_pause(reason="telegram")
            await update.message.reply_text("Print paused.")
        except Exception:
            logger.exception("Pause failed via Telegram command")
            await update.message.reply_text("Pause failed — check the printer.")

    async def cmd_resume(self, update: Update, context: Any) -> None:
        if not self._authorized(update):
            return
        assert update.message is not None
        try:
            await self._printer.resume()
            await update.message.reply_text("Print resumed.")
        except Exception:
            logger.exception("Resume failed via Telegram command")
            await update.message.reply_text("Resume failed — check the printer.")

    async def cmd_stop(self, update: Update, context: Any) -> None:
        if not self._authorized(update):
            return
        assert update.message is not None
        user = update.message.from_user
        assert user is not None
        self._pending_stops[user.id] = time.monotonic()
        await update.message.reply_text("Reply /confirm within 30 s to cancel the print.")

    async def cmd_confirm(self, update: Update, context: Any) -> None:
        if not self._authorized(update):
            return
        assert update.message is not None
        user = update.message.from_user
        assert user is not None

        ts = self._pending_stops.get(user.id)
        if ts is None or (time.monotonic() - ts) > _STOP_CONFIRM_WINDOW:
            self._pending_stops.pop(user.id, None)
            await update.message.reply_text(
                "No active /stop request (or it expired). Use /stop first."
            )
            return

        self._pending_stops.pop(user.id)
        try:
            await self._printer.stop()
            await update.message.reply_text("Print cancelled.")
        except Exception:
            logger.exception("Stop failed via Telegram /confirm")
            await update.message.reply_text("Stop failed — check the printer.")

    async def cmd_enable(self, update: Update, context: Any) -> None:
        if not self._authorized(update):
            return
        assert update.message is not None
        await self._db.set_setting("detection_enabled", "true")
        await update.message.reply_text("Failure detection enabled.")

    async def cmd_disable(self, update: Update, context: Any) -> None:
        if not self._authorized(update):
            return
        assert update.message is not None
        await self._db.set_setting("detection_enabled", "false")
        await update.message.reply_text("Failure detection disabled.")

    # ------------------------------------------------------------------
    # Inline keyboard callbacks
    # ------------------------------------------------------------------

    async def handle_callback(self, update: Update, context: Any) -> None:
        """Dispatch inline keyboard button presses from alert messages."""
        cq = update.callback_query
        if cq is None:
            return
        if not self._authorized(update):
            await cq.answer()
            return

        await cq.answer()
        data: str = cq.data or ""

        if data == "resume":
            try:
                await self._printer.resume()
                await cq.edit_message_text("Print resumed.")
            except Exception:
                logger.exception("Resume failed via inline keyboard")
                await cq.edit_message_text("Resume failed — check the printer.")

        elif data == "stop":
            user = cq.from_user
            self._pending_stops[user.id] = time.monotonic()
            await cq.edit_message_text("Reply /confirm within 30 s to cancel the print.")

        elif data == "snooze":
            await self._db.set_setting("detection_enabled", "false")
            await cq.edit_message_text("Detection snoozed for 10 minutes.")
            task = asyncio.create_task(self._re_enable_after(self._snooze_seconds))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

    async def _re_enable_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._db.set_setting("detection_enabled", "true")
        await self._notifier.send_text("Detection re-enabled after snooze.")
        logger.info("Detection re-enabled after %.0fs snooze", delay)
