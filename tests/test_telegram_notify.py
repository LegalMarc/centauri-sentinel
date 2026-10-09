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


async def test_bad_request_is_not_retried() -> None:
    """BadRequest subclasses NetworkError but is permanent: one attempt only."""
    from telegram.error import BadRequest

    notifier, mock_bot = _make_notifier_enabled()
    mock_bot.send_message = AsyncMock(side_effect=BadRequest("chat not found"))

    with pytest.raises(BadRequest):
        await notifier.send_text("hello")
    assert mock_bot.send_message.call_count == 1


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
# Photo fallback: only a rejected image falls back to text
# ---------------------------------------------------------------------------


def _photo_send_calls(
    notifier: TelegramNotifier,
) -> dict[str, Any]:
    """The four photo-capable senders, each invoked with a JPEG."""
    return {
        "detection": lambda: notifier.send_detection_alert(score=0.85, jpeg=b"jpeg"),
        "print_started": lambda: notifier.send_print_started_alert("f.gcode", b"jpeg"),
        "print_completed": lambda: notifier.send_print_completed_alert("f.gcode", 61.0, b"jpeg"),
        "external_pause": lambda: notifier.send_external_pause_alert(b"jpeg"),
    }


@pytest.mark.parametrize(
    "sender", ["detection", "print_started", "print_completed", "external_pause"]
)
async def test_photo_timeout_retries_photo_without_text_fallback(
    sender: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TimedOut photo may already be delivered: the text fallback would
    duplicate the alert. It must go through the retry loop as a photo instead.
    """
    from telegram.error import TimedOut

    notifier, mock_bot = _make_notifier_enabled()
    mock_bot.send_photo.side_effect = [TimedOut("Timed out"), None]

    async def _fake_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr("asyncio.sleep", _fake_sleep)

    await _photo_send_calls(notifier)[sender]()

    assert mock_bot.send_photo.call_count == 2
    mock_bot.send_message.assert_not_called()


@pytest.mark.parametrize(
    "sender", ["detection", "print_started", "print_completed", "external_pause"]
)
async def test_photo_retry_after_waits_and_retries_photo(
    sender: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a flood-control error the text send would hit the same limit and
    the photo would be lost; honor retry_after and resend the photo.
    """
    from telegram.error import RetryAfter

    notifier, mock_bot = _make_notifier_enabled()
    mock_bot.send_photo.side_effect = [RetryAfter(30), None]

    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr("asyncio.sleep", _fake_sleep)

    await _photo_send_calls(notifier)[sender]()

    assert sleep_calls == [30.0]
    assert mock_bot.send_photo.call_count == 2
    mock_bot.send_message.assert_not_called()


async def test_photo_network_error_exhausted_reraises_without_text_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from telegram.error import NetworkError

    notifier, mock_bot = _make_notifier_enabled()
    mock_bot.send_photo.side_effect = NetworkError("down")

    async def _fake_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr("asyncio.sleep", _fake_sleep)

    with pytest.raises(NetworkError):
        await notifier.send_detection_alert(score=0.85, jpeg=b"jpeg")
    mock_bot.send_message.assert_not_called()


async def test_photo_bad_request_then_text_failure_does_not_resend_photo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the image is known-bad it is dropped for the retries too."""
    from telegram.error import BadRequest, NetworkError

    notifier, mock_bot = _make_notifier_enabled()
    mock_bot.send_photo.side_effect = BadRequest("IMAGE_PROCESS_FAILED")
    mock_bot.send_message.side_effect = [NetworkError("blip"), None]

    async def _fake_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr("asyncio.sleep", _fake_sleep)

    await notifier.send_print_started_alert("f.gcode", b"jpeg")

    mock_bot.send_photo.assert_called_once()
    assert mock_bot.send_message.call_count == 2


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
    from telegram.error import BadRequest

    notifier, mock_bot = _make_notifier_enabled()
    mock_bot.send_photo.side_effect = BadRequest("IMAGE_PROCESS_FAILED")
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
    from telegram.error import BadRequest

    notifier, mock_bot = _make_notifier_enabled()
    mock_bot.send_photo.side_effect = BadRequest("IMAGE_PROCESS_FAILED")
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
    from telegram.error import BadRequest

    notifier, mock_bot = _make_notifier_enabled()
    mock_bot.send_photo.side_effect = BadRequest("IMAGE_PROCESS_FAILED")
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
