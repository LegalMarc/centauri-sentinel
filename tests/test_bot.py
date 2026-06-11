"""Tests for Telegram bot command handlers — ticket #11."""

from __future__ import annotations

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
        printer_ip="192.168.1.10",
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
        db.get_heartbeat.return_value = {
            "last_tick_utc": "2026-01-01T00:00:00+00:00",
            "state": "ARMED",
        }
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
    watcher.last_printer_status = None

    async def get_fresh_status(force: bool = False) -> object:
        return watcher.last_printer_status

    watcher.get_fresh_status = get_fresh_status

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
    is_photo: bool = False,
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
        cq.message.photo = (MagicMock(),) if is_photo else ()
        cq.message.chat = MagicMock()
        cq.message.chat.id = chat_id
        cq.answer = AsyncMock()
        cq.edit_message_text = AsyncMock()
        cq.edit_message_caption = AsyncMock()
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
    assert update.message.reply_photo.called
    caption = update.message.reply_photo.call_args[1]["caption"]
    assert "ARMED" in caption


# ---------------------------------------------------------------------------
# /snapshot
# ---------------------------------------------------------------------------


async def test_cmd_snapshot_sends_photo() -> None:
    handler = _make_handler()
    update = _make_update()
    await handler.cmd_snapshot(update, None)
    assert update.message.reply_photo.called
    assert update.message.reply_photo.call_args[1]["photo"] == b"\xff\xd8\xff\xd9"


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
    handler._watcher.state = WatcherState.PAUSED
    update = _make_update()
    await handler.cmd_resume(update, None)
    printer.resume.assert_called_once()
    assert handler._watcher.state == WatcherState.ARMED
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
    # Must clear snooze_until_utc AND set detection_enabled
    db.set_setting.assert_any_call("snooze_until_utc", "0")
    db.set_setting.assert_any_call("detection_enabled", "true")


async def test_cmd_disable_sets_db_setting() -> None:
    db = AsyncMock()
    db.get_heartbeat.return_value = None
    db.get_recent_detections.return_value = []
    db.get_setting.return_value = "true"
    db.set_setting.return_value = None
    handler = _make_handler(db=db)
    update = _make_update()
    await handler.cmd_disable(update, None)
    # Must clear snooze_until_utc AND set detection_enabled
    db.set_setting.assert_any_call("snooze_until_utc", "0")
    db.set_setting.assert_any_call("detection_enabled", "false")


# ---------------------------------------------------------------------------
# Inline keyboard callbacks
# ---------------------------------------------------------------------------


async def test_callback_resume_calls_printer() -> None:
    printer = AsyncMock()
    handler = _make_handler(printer=printer)
    handler._watcher.state = WatcherState.PAUSED
    update = _make_update(callback_data="resume")
    await handler.handle_callback(update, None)
    printer.resume.assert_called_once()
    assert handler._watcher.state == WatcherState.ARMED
    update.callback_query.edit_message_text.assert_called_once()
    assert "resumed" in update.callback_query.edit_message_text.call_args[0][0].lower()


async def test_callback_stop_sets_pending() -> None:
    handler = _make_handler()
    update = _make_update(callback_data="stop")
    await handler.handle_callback(update, None)
    assert _AUTHORIZED_USER in handler._pending_stops


async def test_callback_snooze_calls_watcher_snooze() -> None:
    handler = _make_handler(snooze_seconds=3600.0)
    handler._watcher.snooze = AsyncMock()
    update = _make_update(callback_data="snooze")
    await handler.handle_callback(update, None)

    handler._watcher.snooze.assert_called_once_with(3600.0)
    assert "snoozed" in update.callback_query.edit_message_text.call_args[0][0].lower()


async def test_callback_unauthorized_no_action() -> None:
    printer = AsyncMock()
    handler = _make_handler(printer=printer)
    update = _make_update(user_id=_OTHER_USER, callback_data="resume")
    await handler.handle_callback(update, None)
    printer.resume.assert_not_called()
    update.callback_query.answer.assert_called_once()
    update.callback_query.edit_message_text.assert_not_called()


async def test_pending_stops_leak_cleanup() -> None:
    handler = _make_handler()
    # Plant an expired stop and a valid stop
    handler._pending_stops[111] = time.monotonic() - 35.0
    handler._pending_stops[222] = time.monotonic() - 5.0

    # Authorized checks must trigger cleanup
    update = _make_update(user_id=_AUTHORIZED_USER)
    assert handler._authorized(update) is True

    assert 111 not in handler._pending_stops
    assert 222 in handler._pending_stops


async def test_cmd_status_with_printer_status() -> None:
    from sentinel.printer.types import PrinterStatus

    handler = _make_handler()
    handler._watcher.last_printer_status = PrinterStatus(
        printing=True,
        elapsed_seconds=3600.0,
        current_layer=25,
        total_layers=100,
        filename="test_print.gcode",
        extruder_temp=220.0,
        extruder_target=220.0,
        bed_temp=60.0,
        bed_target=60.0,
        progress=25.0,
        remaining_seconds=7200.0,
        print_state="printing",
        camera_connected=True,
    )
    update = _make_update()
    await handler.cmd_status(update, None)
    assert update.message.reply_photo.called
    caption = update.message.reply_photo.call_args[1]["caption"]
    assert "test_print.gcode" in caption
    assert "2h 0m 0s" in caption
    assert "25.0%" in caption

    # Test when remaining time is less than 1 hour (minutes/seconds only)
    handler._watcher.last_printer_status.remaining_seconds = 1500.0
    await handler.cmd_status(update, None)
    caption2 = update.message.reply_photo.call_args[1]["caption"]
    assert "25m 0s" in caption2

    # Test camera failure path
    handler._camera.grab.side_effect = RuntimeError("offline")
    await handler.cmd_status(update, None)
    update.message.reply_text.assert_called_once()
    assert "Chamber feed unavailable" in update.message.reply_text.call_args[0][0]


async def test_authorized_edge_cases() -> None:
    handler = _make_handler()

    # 1. callback query message is None
    update_cq_no_msg = MagicMock()
    update_cq_no_msg.message = None
    cq = MagicMock()
    cq.from_user = MagicMock()
    cq.from_user.id = _AUTHORIZED_USER
    cq.message = None
    cq.answer = AsyncMock()
    update_cq_no_msg.callback_query = cq
    assert handler._authorized(update_cq_no_msg) is False

    # 2. user is None in regular message
    update_no_user = MagicMock()
    msg = MagicMock()
    msg.chat_id = _AUTHORIZED_CHAT
    msg.from_user = None
    update_no_user.message = msg
    update_no_user.callback_query = None
    assert handler._authorized(update_no_user) is False

    # 3. neither message nor callback_query
    update_empty = MagicMock()
    update_empty.message = None
    update_empty.callback_query = None
    assert handler._authorized(update_empty) is False


async def test_callback_resume_failure_edits_text() -> None:
    printer = AsyncMock()
    printer.resume.side_effect = RuntimeError("resume fail")
    handler = _make_handler(printer=printer)
    update = _make_update(callback_data="resume")
    await handler.handle_callback(update, None)
    update.callback_query.edit_message_text.assert_called_once()
    assert "failed" in update.callback_query.edit_message_text.call_args[0][0].lower()


async def test_cmd_pause_already_paused() -> None:
    printer = AsyncMock()
    printer.pause.return_value = False
    handler = _make_handler(printer=printer)
    update = _make_update()
    await handler.cmd_pause(update, None)
    update.message.reply_text.assert_called_once()
    assert "already" in update.message.reply_text.call_args[0][0].lower()


async def test_cmd_resume_failure() -> None:
    printer = AsyncMock()
    printer.resume.side_effect = RuntimeError("resume fail")
    handler = _make_handler(printer=printer)
    update = _make_update()
    await handler.cmd_resume(update, None)
    update.message.reply_text.assert_called_once()
    assert "failed" in update.message.reply_text.call_args[0][0].lower()


async def test_unauthorized_all_commands() -> None:
    handler = _make_handler()
    update = _make_update(user_id=_OTHER_USER)
    await handler.cmd_help(update, None)
    await handler.cmd_snapshot(update, None)
    await handler.cmd_pause(update, None)
    await handler.cmd_resume(update, None)
    await handler.cmd_stop(update, None)
    await handler.cmd_confirm(update, None)
    await handler.cmd_enable(update, None)
    await handler.cmd_disable(update, None)
    update.message.reply_text.assert_not_called()


async def test_handle_callback_no_cq() -> None:
    handler = _make_handler()
    update = _make_update()
    update.callback_query = None
    await handler.handle_callback(update, None)


async def test_cmd_status_with_recent_detection() -> None:
    handler = _make_handler()
    handler._db.get_recent_detections.return_value = [
        {"score": 0.85, "ts_utc": "2026-06-01T12:00:00Z"}
    ]
    update = _make_update()
    await handler.cmd_status(update, None)
    caption = update.message.reply_photo.call_args[1]["caption"]
    assert "Last detection: score=0.85" in caption


async def test_cmd_confirm_printer_stop_fails() -> None:
    handler = _make_handler()
    handler._printer.stop.side_effect = Exception("failed")
    handler._pending_stops[_AUTHORIZED_USER] = time.monotonic()
    update = _make_update()
    await handler.cmd_confirm(update, None)
    assert "failed" in update.message.reply_text.call_args[0][0].lower()


async def test_handle_callback_cancel_stop() -> None:
    handler = _make_handler()
    handler._pending_stops[_AUTHORIZED_USER] = time.monotonic()
    update = _make_update(callback_data="cancel_stop")
    await handler.handle_callback(update, None)
    assert _AUTHORIZED_USER not in handler._pending_stops
    update.callback_query.edit_message_reply_markup.assert_called_once()


async def test_handle_callback_confirm_stop_success() -> None:
    handler = _make_handler()
    handler._pending_stops[_AUTHORIZED_USER] = time.monotonic()
    update = _make_update(callback_data="confirm_stop")
    await handler.handle_callback(update, None)
    handler._printer.stop.assert_called_once()
    assert "cancelled" in update.callback_query.edit_message_text.call_args[0][0].lower()


async def test_handle_callback_confirm_stop_expired() -> None:
    handler = _make_handler()
    handler._pending_stops[_AUTHORIZED_USER] = time.monotonic() - 100.0
    update = _make_update(callback_data="confirm_stop")
    await handler.handle_callback(update, None)
    handler._printer.stop.assert_not_called()
    assert "expired" in update.callback_query.edit_message_text.call_args[0][0].lower()


async def test_handle_callback_confirm_stop_fails() -> None:
    handler = _make_handler()
    handler._printer.stop.side_effect = Exception("failed")
    handler._pending_stops[_AUTHORIZED_USER] = time.monotonic()
    update = _make_update(callback_data="confirm_stop")
    await handler.handle_callback(update, None)
    assert "failed" in update.callback_query.edit_message_text.call_args[0][0].lower()


async def test_handle_callback_enable() -> None:
    handler = _make_handler()
    update = _make_update(callback_data="enable")
    await handler.handle_callback(update, None)
    # Must clear snooze_until_utc AND set detection_enabled
    handler._db.set_setting.assert_any_call("snooze_until_utc", "0")
    handler._db.set_setting.assert_any_call("detection_enabled", "true")
    assert "re-enabled" in update.callback_query.edit_message_text.call_args[0][0].lower()


async def test_telegram_bot_rate_limiting() -> None:
    notifier = _make_notifier(user_ids="222222,99999")
    handler = _make_handler(notifier=notifier)

    update1 = _make_update(user_id=_AUTHORIZED_USER)
    for _ in range(5):
        await handler.cmd_help(update1, None)

    assert update1.message.reply_text.call_count == 5

    await handler.cmd_help(update1, None)
    assert update1.message.reply_text.call_count == 6
    last_call = update1.message.reply_text.call_args[0][0]
    assert "Slow down" in last_call

    update2 = _make_update(user_id=99999)
    await handler.cmd_help(update2, None)
    assert update2.message.reply_text.call_count == 1
    assert "Slow down" not in update2.message.reply_text.call_args[0][0]


async def test_callback_resume_photo_message() -> None:
    printer = AsyncMock()
    handler = _make_handler(printer=printer)
    handler._watcher.state = WatcherState.PAUSED
    update = _make_update(callback_data="resume", is_photo=True)
    await handler.handle_callback(update, None)
    printer.resume.assert_called_once()
    update.callback_query.edit_message_caption.assert_called_once()
    assert "resumed" in update.callback_query.edit_message_caption.call_args[1]["caption"].lower()
    update.callback_query.edit_message_text.assert_not_called()


async def test_callback_snooze_photo_message() -> None:
    handler = _make_handler(snooze_seconds=3600.0)
    handler._watcher.snooze = AsyncMock()
    update = _make_update(callback_data="snooze", is_photo=True)
    await handler.handle_callback(update, None)
    handler._watcher.snooze.assert_called_once_with(3600.0)
    update.callback_query.edit_message_caption.assert_called_once()
    assert "snoozed" in update.callback_query.edit_message_caption.call_args[1]["caption"].lower()
    update.callback_query.edit_message_text.assert_not_called()


async def test_callback_confirm_stop_photo_message() -> None:
    handler = _make_handler()
    handler._pending_stops[_AUTHORIZED_USER] = time.monotonic()
    update = _make_update(callback_data="confirm_stop", is_photo=True)
    await handler.handle_callback(update, None)
    handler._printer.stop.assert_called_once()
    update.callback_query.edit_message_caption.assert_called_once()
    assert "cancelled" in update.callback_query.edit_message_caption.call_args[1]["caption"].lower()
    update.callback_query.edit_message_text.assert_not_called()
