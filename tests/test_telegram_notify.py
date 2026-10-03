"""Tests for sentinel/notify/telegram.py."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sentinel.config import Settings
from sentinel.notify.telegram import TelegramNotifier, _parse_user_ids

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _disabled_settings() -> Settings:
    return Settings(printer_ip="10.0.0.1")


def _enabled_settings() -> Settings:
    return Settings(
        printer_ip="10.0.0.1",
        telegram_bot_token="tok",
        telegram_chat_id="99",
        telegram_user_ids="1,2,3",
        telegram_send_snapshots=True,
    )


def _make_notifier_enabled() -> tuple[TelegramNotifier, MagicMock]:
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    mock_bot.send_photo = AsyncMock()
    mock_bot.edit_message_media = AsyncMock()
    mock_bot.edit_message_text = AsyncMock()
    mock_bot.delete_message = AsyncMock()

    with patch("sentinel.notify.telegram.Bot", return_value=mock_bot):
        notifier = TelegramNotifier(_enabled_settings())

    notifier._bot = mock_bot
    return notifier, mock_bot


# ---------------------------------------------------------------------------
# _parse_user_ids
# ---------------------------------------------------------------------------


def test_parse_user_ids_normal() -> None:
    ids = _parse_user_ids("1,2,3")
    assert ids == frozenset({1, 2, 3})


def test_parse_user_ids_empty() -> None:
    assert _parse_user_ids(None) == frozenset()
    assert _parse_user_ids("") == frozenset()


def test_parse_user_ids_invalid_entry_skipped() -> None:
    ids = _parse_user_ids("1,bad,3")
    assert ids == frozenset({1, 3})


# ---------------------------------------------------------------------------
# Disabled mode — no-op
# ---------------------------------------------------------------------------


async def test_disabled_send_detection_alert_noop() -> None:
    with patch("sentinel.notify.telegram.Bot"):
        notifier = TelegramNotifier(_disabled_settings())
    await notifier.send_detection_alert(0.9)  # must not raise


async def test_disabled_send_stall_alert_noop() -> None:
    with patch("sentinel.notify.telegram.Bot"):
        notifier = TelegramNotifier(_disabled_settings())
    await notifier.send_stall_alert()


async def test_disabled_send_camera_offline_noop() -> None:
    with patch("sentinel.notify.telegram.Bot"):
        notifier = TelegramNotifier(_disabled_settings())
    await notifier.send_camera_offline_alert()


async def test_disabled_send_text_noop() -> None:
    with patch("sentinel.notify.telegram.Bot"):
        notifier = TelegramNotifier(_disabled_settings())
    await notifier.send_text("hello")


# ---------------------------------------------------------------------------
# is_authorized
# ---------------------------------------------------------------------------


def test_is_authorized_correct_chat_and_user() -> None:
    notifier, _ = _make_notifier_enabled()
    assert notifier.is_authorized(chat_id=99, user_id=1) is True


def test_is_authorized_wrong_chat() -> None:
    notifier, _ = _make_notifier_enabled()
    assert notifier.is_authorized(chat_id=999, user_id=1) is False


def test_is_authorized_wrong_user() -> None:
    notifier, _ = _make_notifier_enabled()
    assert notifier.is_authorized(chat_id=99, user_id=999) is False


def test_is_authorized_disabled_returns_false() -> None:
    with patch("sentinel.notify.telegram.Bot"):
        notifier = TelegramNotifier(_disabled_settings())
    assert notifier.is_authorized(chat_id=99, user_id=1) is False


# ---------------------------------------------------------------------------
# Enabled mode — sends messages
# ---------------------------------------------------------------------------


async def test_send_detection_alert_calls_bot() -> None:
    notifier, mock_bot = _make_notifier_enabled()
    await notifier.send_detection_alert(score=0.85)
    mock_bot.send_message.assert_called_once()
    call_kwargs = mock_bot.send_message.call_args
    assert "85%" in call_kwargs.kwargs.get("text", "")
    assert "reply_markup" in call_kwargs.kwargs


async def test_send_detection_alert_with_photo_calls_bot() -> None:
    notifier, mock_bot = _make_notifier_enabled()
    await notifier.send_detection_alert(score=0.85, jpeg=b"fake_jpeg")
    mock_bot.send_photo.assert_called_once()
    call_kwargs = mock_bot.send_photo.call_args
    assert call_kwargs.kwargs.get("photo") == b"fake_jpeg"
    assert "85%" in call_kwargs.kwargs.get("caption", "")
    assert "reply_markup" in call_kwargs.kwargs


async def test_send_detection_alert_with_photo_fallback() -> None:
    """A non-network send_photo failure (e.g. BadRequest on a bad image) must
    fall back to send_message instead of propagating and dropping the alert.
    """
    from telegram.error import BadRequest

    notifier, mock_bot = _make_notifier_enabled()
    mock_bot.send_photo.side_effect = BadRequest("IMAGE_PROCESS_FAILED")

    await notifier.send_detection_alert(score=0.85, jpeg=b"fake_jpeg")

    mock_bot.send_photo.assert_called_once()
    mock_bot.send_message.assert_called_once()
    call_kwargs = mock_bot.send_message.call_args
    assert "85%" in call_kwargs.kwargs.get("text", "")
    assert "reply_markup" in call_kwargs.kwargs


async def test_send_stall_alert_calls_bot() -> None:
    notifier, mock_bot = _make_notifier_enabled()
    await notifier.send_stall_alert()
    mock_bot.send_message.assert_called_once()


async def test_send_camera_offline_alert_calls_bot() -> None:
    notifier, mock_bot = _make_notifier_enabled()
    await notifier.send_camera_offline_alert()
    mock_bot.send_message.assert_called_once()


async def test_send_text_calls_bot() -> None:
    notifier, mock_bot = _make_notifier_enabled()
    await notifier.send_text("custom message")
    mock_bot.send_message.assert_called_once()
    call_kwargs = mock_bot.send_message.call_args
    assert "custom message" in call_kwargs.kwargs.get("text", "")


# ---------------------------------------------------------------------------
# Retry on transient failure
# ---------------------------------------------------------------------------


async def test_retry_on_transient_failure() -> None:
    from telegram.error import NetworkError

    call_count = 0

    async def _flaky_send(**kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise NetworkError("network blip")

    notifier, mock_bot = _make_notifier_enabled()
    mock_bot.send_message.side_effect = _flaky_send

    await notifier.send_text("hello")
    assert call_count == 2


async def test_retry_exhausted_reraises() -> None:
    from telegram.error import NetworkError

    notifier, mock_bot = _make_notifier_enabled()
    mock_bot.send_message = AsyncMock(side_effect=NetworkError("always fails"))

    with pytest.raises(NetworkError, match="always fails"):
        await notifier.send_text("hello")


async def test_retry_after_honors_server_wait_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """RetryAfter(retry_after=30) must wait ~30s, not the ~8s exponential cap."""
    from telegram.error import RetryAfter

    call_count = 0

    async def _flaky_send(**kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise RetryAfter(30)

    notifier, mock_bot = _make_notifier_enabled()
    mock_bot.send_message.side_effect = _flaky_send

    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr("asyncio.sleep", _fake_sleep)

    await notifier.send_text("hello")

    assert call_count == 2
    assert sleep_calls == [30.0]


async def test_retry_after_falls_back_to_exponential_for_other_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-RetryAfter transient errors still use the small exponential backoff."""
    from telegram.error import NetworkError

    call_count = 0

    async def _flaky_send(**kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise NetworkError("network blip")

    notifier, mock_bot = _make_notifier_enabled()
    mock_bot.send_message.side_effect = _flaky_send

    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr("asyncio.sleep", _fake_sleep)

    await notifier.send_text("hello")

    assert call_count == 2
    assert sleep_calls == [0.5]


# ---------------------------------------------------------------------------
# Startup validation — fail loudly on empty or invalid user IDs
# ---------------------------------------------------------------------------


def test_init_raises_if_telegram_enabled_with_empty_user_ids() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="TELEGRAM_USER_IDS"):
        Settings(
            printer_ip="10.0.0.1",
            telegram_bot_token="tok",
            telegram_chat_id="99",
            telegram_user_ids="",
        )


def test_init_raises_if_all_user_ids_are_invalid() -> None:
    settings = Settings(
        printer_ip="10.0.0.1",
        telegram_bot_token="tok",
        telegram_chat_id="99",
        telegram_user_ids="not_a_number,also_bad",
    )
    with (
        patch("sentinel.notify.telegram.Bot"),
        pytest.raises(ValueError, match="TELEGRAM_USER_IDS"),
    ):
        TelegramNotifier(settings)


async def test_send_print_started_alert() -> None:
    notifier, mock_bot = _make_notifier_enabled()
    await notifier.send_print_started_alert("file.gcode")
    mock_bot.send_message.assert_called_once()


async def test_send_print_started_alert_with_photo() -> None:
    notifier, mock_bot = _make_notifier_enabled()
    await notifier.send_print_started_alert("file.gcode", b"jpeg")
    mock_bot.send_photo.assert_called_once()


async def test_send_print_started_alert_with_photo_fallback() -> None:
    notifier, mock_bot = _make_notifier_enabled()
    mock_bot.send_photo.side_effect = Exception("failed")
    await notifier.send_print_started_alert("file.gcode", b"jpeg")
    mock_bot.send_message.assert_called_once()


async def test_send_print_completed_alert() -> None:
    notifier, mock_bot = _make_notifier_enabled()
    await notifier.send_print_completed_alert("file.gcode", 3661.0)
    mock_bot.send_message.assert_called_once()


async def test_send_print_completed_alert_with_photo() -> None:
    notifier, mock_bot = _make_notifier_enabled()
    await notifier.send_print_completed_alert("file.gcode", 3661.0, b"jpeg")
    mock_bot.send_photo.assert_called_once()


async def test_send_print_completed_alert_with_photo_fallback() -> None:
    notifier, mock_bot = _make_notifier_enabled()
    mock_bot.send_photo.side_effect = Exception("failed")
    await notifier.send_print_completed_alert("file.gcode", 3661.0, b"jpeg")
    mock_bot.send_message.assert_called_once()


async def test_send_external_pause_alert() -> None:
    notifier, mock_bot = _make_notifier_enabled()
    await notifier.send_external_pause_alert()
    mock_bot.send_message.assert_called_once()


async def test_send_external_pause_alert_with_photo() -> None:
    notifier, mock_bot = _make_notifier_enabled()
    await notifier.send_external_pause_alert(b"jpeg")
    mock_bot.send_photo.assert_called_once()


async def test_send_external_pause_alert_with_photo_fallback() -> None:
    notifier, mock_bot = _make_notifier_enabled()
    mock_bot.send_photo.side_effect = Exception("failed")
    await notifier.send_external_pause_alert(b"jpeg")
    mock_bot.send_message.assert_called_once()


async def test_send_detection_alert_disk_read_failure(tmp_path: Any) -> None:
    notifier, mock_bot = _make_notifier_enabled()
    notifier._snapshots_dir = tmp_path
    await notifier.send_detection_alert(0.9, snapshot_id="missing")
    mock_bot.send_message.assert_called_once()


async def test_telegram_edge_cases(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. OSError handling during snapshot disk read
    notifier, mock_bot = _make_notifier_enabled()
    notifier._snapshots_dir = tmp_path
    p = tmp_path / "failed_snap.jpg"
    p.write_bytes(b"disk_data")

    from pathlib import Path

    orig_read_bytes = Path.read_bytes

    def mock_read_bytes(self: Path) -> bytes:
        if "failed_snap" in str(self):
            raise OSError("read error")
        return orig_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", mock_read_bytes)

    await notifier.send_detection_alert(0.9, snapshot_id="failed_snap")
    mock_bot.send_message.assert_called_once()

    # 2. Disabled mode noops
    with patch("sentinel.notify.telegram.Bot"):
        notifier_disabled = TelegramNotifier(_disabled_settings())

    await notifier_disabled.send_print_started_alert("file.gcode")
    await notifier_disabled.send_print_completed_alert("file.gcode", 10.0)
    await notifier_disabled.send_external_pause_alert()

    # 3. close() method on disabled
    await notifier_disabled.close()

    # 4. close() method on enabled
    notifier_enabled, mock_bot_enabled = _make_notifier_enabled()
    mock_bot_enabled.shutdown = AsyncMock()
    await notifier_enabled.close()
    mock_bot_enabled.shutdown.assert_called_once()


# ---------------------------------------------------------------------------
# Live print status card
# ---------------------------------------------------------------------------


def _progress_status(progress: float, filename: str = "benchy.gcode") -> MagicMock:
    status = MagicMock()
    status.progress = progress
    status.filename = filename
    return status


async def test_print_progress_first_update_sends_photo() -> None:
    notifier, mock_bot = _make_notifier_enabled()
    message = MagicMock()
    message.message_id = 123
    mock_bot.send_photo.return_value = message

    with patch("sentinel.notify.telegram.time.monotonic", return_value=1000.0):
        await notifier.send_print_progress(
            _progress_status(10.0),
            jpeg=b"jpeg-1",
            caption="status 10%",
        )

    mock_bot.send_photo.assert_called_once()
    assert mock_bot.send_photo.call_args.kwargs["photo"] == b"jpeg-1"
    assert mock_bot.send_photo.call_args.kwargs["caption"] == "status 10%"
    assert mock_bot.send_photo.call_args.kwargs["disable_notification"] is False
    assert notifier._progress_message_id == 123


async def test_print_progress_after_five_minutes_edits_media() -> None:
    notifier, mock_bot = _make_notifier_enabled()
    message = MagicMock()
    message.message_id = 123
    mock_bot.send_photo.return_value = message

    with patch(
        "sentinel.notify.telegram.time.monotonic",
        side_effect=[1000.0, 1301.0],
    ):
        await notifier.send_print_progress(
            _progress_status(10.0),
            jpeg=b"jpeg-1",
            caption="status 10%",
        )
        await notifier.send_print_progress(
            _progress_status(20.0),
            jpeg=b"jpeg-2",
            caption="status 20%",
        )

    mock_bot.send_photo.assert_called_once()
    mock_bot.edit_message_media.assert_called_once()

    kwargs = mock_bot.edit_message_media.call_args.kwargs
    assert kwargs["message_id"] == 123
    assert kwargs["media"].caption == "status 20%"
    assert kwargs["media"].media is not None


async def test_print_progress_milestone_sends_silent_new_card() -> None:
    notifier, mock_bot = _make_notifier_enabled()

    first = MagicMock()
    first.message_id = 123
    second = MagicMock()
    second.message_id = 456
    mock_bot.send_photo.side_effect = [first, second]

    with patch(
        "sentinel.notify.telegram.time.monotonic",
        side_effect=[1000.0, 1301.0],
    ):
        await notifier.send_print_progress(
            _progress_status(20.0),
            jpeg=b"jpeg-1",
            caption="status 20%",
        )
        await notifier.send_print_progress(
            _progress_status(26.0),
            jpeg=b"jpeg-2",
            caption="status 26%",
        )

    assert mock_bot.send_photo.call_count == 2

    kwargs = mock_bot.send_photo.call_args.kwargs
    assert kwargs["photo"] == b"jpeg-2"
    assert kwargs["caption"] == "status 26%"
    assert kwargs["disable_notification"] is True

    mock_bot.delete_message.assert_awaited_once_with(
        chat_id="99",
        message_id=123,
    )
    assert notifier._progress_message_id == 456
    assert notifier._progress_milestone == 25
