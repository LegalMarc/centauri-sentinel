"""Tests for Telegram bot command handlers — ticket #11."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from sentinel.bot.commands import BotCommandHandler
from sentinel.config import Settings
from sentinel.watcher.state import WatcherState

if TYPE_CHECKING:
    import pytest

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

_AUTHORIZED_CHAT = 111111
_AUTHORIZED_USER = 222222
_OTHER_USER = 999999
_OTHER_CHAT = 888888


def _settings(
    *, chat_id: int = _AUTHORIZED_CHAT, user_ids: str = str(_AUTHORIZED_USER)
) -> Settings:
    return Settings(
        printer_ip="127.0.0.1",
        printer_access_code="000000",
        telegram_bot_token="fake:token",
        telegram_chat_id=str(chat_id),
        telegram_user_ids=user_ids,
        bind_host="127.0.0.1",
        external_bind_allowed=True,
    )


def _make_notifier(
    chat_id: int = _AUTHORIZED_CHAT, user_ids: str = str(_AUTHORIZED_USER)
) -> MagicMock:
    from sentinel.notify.telegram import TelegramNotifier

    notifier = MagicMock(spec=TelegramNotifier)

    def _is_authorized(cid: object, uid: int) -> bool:
        return str(cid) == str(chat_id) and uid in {int(u) for u in user_ids.split(",")}

    notifier.is_authorized.side_effect = _is_authorized
    notifier.send_text = AsyncMock()
    return notifier


def _make_handler(
    *,
    notifier: MagicMock | None = None,
    db: AsyncMock | None = None,
    printer: AsyncMock | None = None,
    camera: AsyncMock | None = None,
    snooze_seconds: float = 0.05,
) -> BotCommandHandler:
    if notifier is None:
        notifier = _make_notifier()
    if db is None:
        db = AsyncMock()
        db.get_heartbeat.return_value = "2026-01-01T00:00:00+00:00"
        db.get_recent_detections.return_value = []
        db.get_setting.return_value = "true"
        db.set_setting.return_value = None
        db.record_pause.return_value = 1
    if printer is None:
        printer = AsyncMock()
    if camera is None:
        camera = AsyncMock()
        camera.grab.return_value = b"\xff\xd8\xff\xd9"

    watcher = MagicMock()
    watcher.state = WatcherState.ARMED

    return BotCommandHandler(
        _settings(),
        printer,
        camera,
        db,
        watcher,
        notifier,
        snooze_seconds=snooze_seconds,
    )


def _make_update(
    user_id: int = _AUTHORIZED_USER,
    chat_id: int = _AUTHORIZED_CHAT,
    *,
    callback_data: str | None = None,
) -> MagicMock:
    """Build a minimal mock Update, either a message or a callback_query."""
    update = MagicMock()

    user = MagicMock()
    user.id = user_id

    if callback_data is not None:
        # Callback query update (inline keyboard button press)
        update.message = None
        cq = AsyncMock()
        cq.data = callback_data
        cq.from_user = user
        cq.message = MagicMock()
        cq.message.chat = MagicMock()
        cq.message.chat.id = chat_id
        cq.answer = AsyncMock()
        cq.edit_message_text = AsyncMock()
        update.callback_query = cq
    else:
        # Regular command message
        msg = AsyncMock()
        msg.chat_id = chat_id
        msg.from_user = user
        msg.reply_text = AsyncMock()
        msg.reply_photo = AsyncMock()
        update.message = msg
        update.callback_query = None

    return update


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------


async def test_unauthorized_user_ignored() -> None:
    handler = _make_handler()
    update = _make_update(user_id=_OTHER_USER)
    await handler.cmd_status(update, None)
    update.message.reply_text.assert_not_called()


async def test_unauthorized_chat_ignored() -> None:
    handler = _make_handler()
    update = _make_update(chat_id=_OTHER_CHAT)
    await handler.cmd_status(update, None)
    update.message.reply_text.assert_not_called()


async def test_unauthorized_user_warning_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = _make_handler()
    update = _make_update(user_id=_OTHER_USER)
    with caplog.at_level("WARNING", logger="sentinel.bot.commands"):
        await handler.cmd_status(update, None)
    assert any("Unauthorized" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------


async def test_cmd_help_lists_commands() -> None:
    handler = _make_handler()
    update = _make_update()
    await handler.cmd_help(update, None)
    update.message.reply_text.assert_called_once()
    text: str = update.message.reply_text.call_args[0][0]
    for cmd in (
        "/status",
        "/snapshot",
        "/pause",
        "/resume",
        "/stop",
        "/confirm",
        "/enable",
        "/disable",
    ):
        assert cmd in text


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------


async def test_cmd_status_includes_state() -> None:
    handler = _make_handler()
    update = _make_update()
    await handler.cmd_status(update, None)
    text: str = update.message.reply_text.call_args[0][0]
    assert "ARMED" in text


# ---------------------------------------------------------------------------
# /snapshot
# ---------------------------------------------------------------------------


async def test_cmd_snapshot_sends_photo() -> None:
    handler = _make_handler()
    update = _make_update()
    await handler.cmd_snapshot(update, None)
    update.message.reply_photo.assert_called_once_with(photo=b"\xff\xd8\xff\xd9")


async def test_cmd_snapshot_camera_error_replies_text() -> None:
    cam = AsyncMock()
    cam.grab.side_effect = RuntimeError("offline")
    handler = _make_handler(camera=cam)
    update = _make_update()
    await handler.cmd_snapshot(update, None)
    update.message.reply_text.assert_called_once()
    assert "unavailable" in update.message.reply_text.call_args[0][0].lower()


# ---------------------------------------------------------------------------
# /pause and /resume
# ---------------------------------------------------------------------------


async def test_cmd_pause_calls_printer() -> None:
    printer = AsyncMock()
    handler = _make_handler(printer=printer)
    update = _make_update()
    await handler.cmd_pause(update, None)
    printer.pause.assert_called_once()
    update.message.reply_text.assert_called_once()
    assert "paused" in update.message.reply_text.call_args[0][0].lower()


async def test_cmd_resume_calls_printer() -> None:
    printer = AsyncMock()
    handler = _make_handler(printer=printer)
    update = _make_update()
    await handler.cmd_resume(update, None)
    printer.resume.assert_called_once()
    update.message.reply_text.assert_called_once()
    assert "resumed" in update.message.reply_text.call_args[0][0].lower()


async def test_cmd_pause_failure_replies_error() -> None:
    printer = AsyncMock()
    printer.pause.side_effect = RuntimeError("mqtt down")
    handler = _make_handler(printer=printer)
    update = _make_update()
    await handler.cmd_pause(update, None)
    assert "failed" in update.message.reply_text.call_args[0][0].lower()


# ---------------------------------------------------------------------------
# /stop + /confirm
# ---------------------------------------------------------------------------


async def test_stop_sets_pending_and_prompts() -> None:
    handler = _make_handler()
    update = _make_update()
    await handler.cmd_stop(update, None)
    text: str = update.message.reply_text.call_args[0][0]
    assert "/confirm" in text
    assert _AUTHORIZED_USER in handler._pending_stops


async def test_confirm_within_window_stops_printer() -> None:
    printer = AsyncMock()
    handler = _make_handler(printer=printer)
    update = _make_update()
    # Issue /stop then /confirm immediately
    await handler.cmd_stop(update, None)
    await handler.cmd_confirm(update, None)
    printer.stop.assert_called_once()
    assert "cancelled" in update.message.reply_text.call_args[0][0].lower()


async def test_confirm_after_expiry_rejected() -> None:
    printer = AsyncMock()
    handler = _make_handler(printer=printer)
    update = _make_update()
    # Plant an expired stop
    handler._pending_stops[_AUTHORIZED_USER] = time.monotonic() - 31.0
    await handler.cmd_confirm(update, None)
    printer.stop.assert_not_called()
    text: str = update.message.reply_text.call_args[0][0]
    assert "expired" in text.lower() or "stop" in text.lower()


async def test_confirm_without_stop_rejected() -> None:
    printer = AsyncMock()
    handler = _make_handler(printer=printer)
    update = _make_update()
    await handler.cmd_confirm(update, None)
    printer.stop.assert_not_called()


async def test_confirm_wrong_user_rejected() -> None:
    printer = AsyncMock()
    handler = _make_handler(printer=printer)
    # User A issues /stop
    update_a = _make_update(user_id=_AUTHORIZED_USER)
    await handler.cmd_stop(update_a, None)
    # User B tries /confirm — not in pending
    other_notifier = _make_notifier(user_ids=f"{_AUTHORIZED_USER},{_OTHER_USER}")
    handler._notifier = other_notifier
    update_b = _make_update(user_id=_OTHER_USER)
    await handler.cmd_confirm(update_b, None)
    printer.stop.assert_not_called()


# ---------------------------------------------------------------------------
# /enable and /disable
# ---------------------------------------------------------------------------


async def test_cmd_enable_sets_db_setting() -> None:
    db = AsyncMock()
    db.get_heartbeat.return_value = None
    db.get_recent_detections.return_value = []
    db.get_setting.return_value = "false"
    db.set_setting.return_value = None
    handler = _make_handler(db=db)
    update = _make_update()
    await handler.cmd_enable(update, None)
    db.set_setting.assert_called_once_with("detection_enabled", "true")


async def test_cmd_disable_sets_db_setting() -> None:
    db = AsyncMock()
    db.get_heartbeat.return_value = None
    db.get_recent_detections.return_value = []
    db.get_setting.return_value = "true"
    db.set_setting.return_value = None
    handler = _make_handler(db=db)
    update = _make_update()
    await handler.cmd_disable(update, None)
    db.set_setting.assert_called_once_with("detection_enabled", "false")


# ---------------------------------------------------------------------------
# Inline keyboard callbacks
# ---------------------------------------------------------------------------


async def test_callback_resume_calls_printer() -> None:
    printer = AsyncMock()
    handler = _make_handler(printer=printer)
    update = _make_update(callback_data="resume")
    await handler.handle_callback(update, None)
    printer.resume.assert_called_once()
    update.callback_query.edit_message_text.assert_called_once()
    assert "resumed" in update.callback_query.edit_message_text.call_args[0][0].lower()


async def test_callback_stop_sets_pending() -> None:
    handler = _make_handler()
    update = _make_update(callback_data="stop")
    await handler.handle_callback(update, None)
    assert _AUTHORIZED_USER in handler._pending_stops


async def test_callback_snooze_disables_detection_and_reenables() -> None:
    db = AsyncMock()
    db.set_setting.return_value = None
    notifier = _make_notifier()
    handler = _make_handler(db=db, notifier=notifier, snooze_seconds=0.05)
    update = _make_update(callback_data="snooze")
    await handler.handle_callback(update, None)

    # Check that detection was disabled immediately
    db.set_setting.assert_any_call("detection_enabled", "false")

    # Wait for the re-enable task to fire
    await asyncio.sleep(0.2)
    db.set_setting.assert_any_call("detection_enabled", "true")
    notifier.send_text.assert_called_once()


async def test_callback_unauthorized_no_action() -> None:
    printer = AsyncMock()
    handler = _make_handler(printer=printer)
    update = _make_update(user_id=_OTHER_USER, callback_data="resume")
    await handler.handle_callback(update, None)
    printer.resume.assert_not_called()
    update.callback_query.answer.assert_called_once()
    update.callback_query.edit_message_text.assert_not_called()
