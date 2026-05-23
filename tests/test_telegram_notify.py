"""Tests for sentinel/notify/telegram.py."""

from __future__ import annotations

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
    )


def _make_notifier_enabled() -> tuple[TelegramNotifier, MagicMock]:
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

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
    call_count = 0

    async def _flaky_send(**kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise Exception("network blip")

    notifier, mock_bot = _make_notifier_enabled()
    mock_bot.send_message.side_effect = _flaky_send

    await notifier.send_text("hello")
    assert call_count == 2


async def test_retry_exhausted_reraises() -> None:
    notifier, mock_bot = _make_notifier_enabled()
    mock_bot.send_message = AsyncMock(side_effect=Exception("always fails"))

    with pytest.raises(Exception, match="always fails"):
        await notifier.send_text("hello")
