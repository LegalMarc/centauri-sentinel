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

from telegram import KeyboardButton, ReplyKeyboardMarkup

_background_tasks: set[asyncio.Task[None]] = set()

_TUI_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📊 Status"), KeyboardButton("📸 Snapshot")],
        [KeyboardButton("⏸️ Pause"), KeyboardButton("▶️ Resume"), KeyboardButton("⏹️ Stop")],
    ],
    resize_keyboard=True,
)

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
        # Clean up expired pending stop requests to prevent memory leaks (M10)
        now = time.monotonic()
        expired = [
            uid for uid, ts in list(self._pending_stops.items()) if now - ts > _STOP_CONFIRM_WINDOW
        ]
        for uid in expired:
            self._pending_stops.pop(uid, None)

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
            "/help — this message",
            reply_markup=_TUI_KEYBOARD,
        )

    async def cmd_status(self, update: Update, context: Any) -> None:
        if not self._authorized(update):
            return
        assert update.message is not None
        detection_enabled = await self._db.get_setting("detection_enabled", "true")
        recent = await self._db.get_recent_detections(limit=1)
        last_det = recent[0] if recent else None

        # Expose printer state and elapsed print time
        p_status = self._watcher.last_printer_status
        printer_state = "Offline"
        print_elapsed = "—"
        extruder_temp = None
        extruder_target = None
        bed_temp = None
        bed_target = None
        progress = 0.0
        remaining_seconds = 0.0
        filename = "—"
        current_layer = 0
        total_layers = 0

        if p_status:
            print_state = p_status.print_state or ("printing" if p_status.printing else "idle")
            printer_state = print_state.capitalize()
            is_active = p_status.printing or print_state == "paused"
            print_elapsed = f"{p_status.elapsed_seconds:.0f}s" if is_active else "—"
            extruder_temp = p_status.extruder_temp
            extruder_target = p_status.extruder_target
            bed_temp = p_status.bed_temp
            bed_target = p_status.bed_target
            progress = p_status.progress
            remaining_seconds = p_status.remaining_seconds
            filename = p_status.filename or "—"
            current_layer = p_status.current_layer
            total_layers = p_status.total_layers

        time_rem = "—"
        if remaining_seconds > 0:
            hours = int(remaining_seconds // 3600)
            minutes = int((remaining_seconds % 3600) // 60)
            secs = int(remaining_seconds % 60)
            time_rem = f"{hours}h {minutes}m {secs}s" if hours > 0 else f"{minutes}m {secs}s"

        ext_str = (
            f"{extruder_temp:.1f}°C / {extruder_target:.0f}°C"
            if (extruder_temp is not None and extruder_target is not None)
            else "—"
        )
        bed_str = (
            f"{bed_temp:.1f}°C / {bed_target:.0f}°C"
            if (bed_temp is not None and bed_target is not None)
            else "—"
        )

        lines = [
            f"👁️ Watcher: {self._watcher.state.name}",
            f"⚙️ Detection: {'enabled' if detection_enabled == 'true' else 'disabled'}",
            f"🖨️ Printer: {printer_state}",
            f"📄 File: {filename}",
            f"📊 Progress: {progress:.1f}% (Layer {current_layer}/{total_layers})",
            f"⏳ Remaining: {time_rem} (Elapsed: {print_elapsed})",
            f"🔥 Extruder: {ext_str}",
            f"🛏️ Bed: {bed_str}",
        ]
        if last_det:
            lines.append(f"⚠️ Last detection: score={last_det['score']:.2f} at {last_det['ts_utc']}")

        caption = "\n".join(lines)

        try:
            jpeg = await self._camera.grab()
            await update.message.reply_photo(
                photo=jpeg, caption=caption, reply_markup=_TUI_KEYBOARD
            )
        except Exception:
            logger.exception("Failed to grab snapshot for Telegram status")
            await update.message.reply_text(
                caption + "\n\n⚠️ Chamber feed unavailable.", reply_markup=_TUI_KEYBOARD
            )

    async def cmd_snapshot(self, update: Update, context: Any) -> None:
        if not self._authorized(update):
            return
        assert update.message is not None
        try:
            jpeg = await self._camera.grab()
            await update.message.reply_photo(photo=jpeg, reply_markup=_TUI_KEYBOARD)
        except Exception:
            logger.exception("Failed to grab snapshot for Telegram")
            await update.message.reply_text("Camera unavailable.", reply_markup=_TUI_KEYBOARD)

    async def cmd_pause(self, update: Update, context: Any) -> None:
        if not self._authorized(update):
            return
        assert update.message is not None
        try:
            sent = await self._printer.pause()
        except Exception as exc:
            logger.exception("Pause failed via Telegram command")
            await self._db.record_pause(source="telegram", result="error", error_message=str(exc))
            await update.message.reply_text(
                "Pause failed — check the printer.", reply_markup=_TUI_KEYBOARD
            )
            return
        if sent:
            await self._db.record_pause(source="telegram", result="ok")
            await update.message.reply_text("Print paused.", reply_markup=_TUI_KEYBOARD)
        else:
            await self._db.record_pause(
                source="telegram", result="error", error_message="Printer already paused"
            )
            await update.message.reply_text(
                "Printer is already paused.", reply_markup=_TUI_KEYBOARD
            )

    async def cmd_resume(self, update: Update, context: Any) -> None:
        if not self._authorized(update):
            return
        assert update.message is not None
        try:
            await self._printer.resume()
            from sentinel.watcher.state import WatcherState

            if self._watcher.state == WatcherState.PAUSED:
                self._watcher.state = WatcherState.ARMED
            await update.message.reply_text("Print resumed.", reply_markup=_TUI_KEYBOARD)
        except Exception:
            logger.exception("Resume failed via Telegram command")
            await update.message.reply_text(
                "Resume failed — check the printer.", reply_markup=_TUI_KEYBOARD
            )

    async def cmd_stop(self, update: Update, context: Any) -> None:
        if not self._authorized(update):
            return
        assert update.message is not None
        user = update.message.from_user
        assert user is not None
        self._pending_stops[user.id] = time.monotonic()
        await update.message.reply_text(
            "Reply /confirm within 30 s to cancel the print.", reply_markup=_TUI_KEYBOARD
        )

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
                "No active /stop request (or it expired). Use /stop first.",
                reply_markup=_TUI_KEYBOARD,
            )
            return

        self._pending_stops.pop(user.id)
        try:
            await self._printer.stop()
            await update.message.reply_text("Print cancelled.", reply_markup=_TUI_KEYBOARD)
        except Exception:
            logger.exception("Stop failed via Telegram /confirm")
            await update.message.reply_text(
                "Stop failed — check the printer.", reply_markup=_TUI_KEYBOARD
            )

    async def cmd_enable(self, update: Update, context: Any) -> None:
        if not self._authorized(update):
            return
        assert update.message is not None
        await self._db.set_setting("detection_enabled", "true")
        await update.message.reply_text("Failure detection enabled.", reply_markup=_TUI_KEYBOARD)

    async def cmd_disable(self, update: Update, context: Any) -> None:
        if not self._authorized(update):
            return
        assert update.message is not None
        await self._db.set_setting("detection_enabled", "false")
        await update.message.reply_text("Failure detection disabled.", reply_markup=_TUI_KEYBOARD)

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
                from sentinel.watcher.state import WatcherState

                if self._watcher.state == WatcherState.PAUSED:
                    self._watcher.state = WatcherState.ARMED
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

    def cancel_background_tasks(self) -> None:
        """Cancel all pending snooze / background tasks (M9)."""
        if _background_tasks:
            logger.info("Cancelling %d background bot tasks", len(_background_tasks))
            for task in list(_background_tasks):
                task.cancel()
