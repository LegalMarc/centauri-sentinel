"""Tests for sentinel/notify/ntfy.py."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from sentinel.config import Settings
from sentinel.notify.ntfy import NtfyNotifier

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _disabled_settings() -> Settings:
    return Settings(printer_ip="10.0.0.1")


def _enabled_settings(*, token: str | None = None) -> Settings:
    return Settings(
        printer_ip="10.0.0.1",
        ntfy_url="https://my-ntfy.local/test",
        ntfy_token=token,
    )


def _make_http_client(status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=MagicMock()
        )

    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


# ---------------------------------------------------------------------------
# Disabled mode
# ---------------------------------------------------------------------------


async def test_disabled_detection_alert_noop() -> None:
    notifier = NtfyNotifier(_disabled_settings())
    await notifier.send_detection_alert(0.9)  # must not raise


async def test_disabled_stall_alert_noop() -> None:
    notifier = NtfyNotifier(_disabled_settings())
    await notifier.send_stall_alert()


async def test_disabled_camera_offline_noop() -> None:
    notifier = NtfyNotifier(_disabled_settings())
    await notifier.send_camera_offline_alert()


# ---------------------------------------------------------------------------
# Enabled mode — posts to ntfy
# ---------------------------------------------------------------------------


async def test_detection_alert_posts() -> None:
    mock_client = _make_http_client()
    with patch("sentinel.notify.ntfy.httpx.AsyncClient", return_value=mock_client):
        notifier = NtfyNotifier(_enabled_settings())
        await notifier.send_detection_alert(0.75)
    mock_client.post.assert_called_once()
    call_kwargs = mock_client.post.call_args
    assert "https://my-ntfy.local/test" in str(call_kwargs)


async def test_stall_alert_posts() -> None:
    mock_client = _make_http_client()
    with patch("sentinel.notify.ntfy.httpx.AsyncClient", return_value=mock_client):
        notifier = NtfyNotifier(_enabled_settings())
        await notifier.send_stall_alert()
    mock_client.post.assert_called_once()


async def test_camera_offline_posts() -> None:
    mock_client = _make_http_client()
    with patch("sentinel.notify.ntfy.httpx.AsyncClient", return_value=mock_client):
        notifier = NtfyNotifier(_enabled_settings())
        await notifier.send_camera_offline_alert()
    mock_client.post.assert_called_once()


# ---------------------------------------------------------------------------
# Auth header
# ---------------------------------------------------------------------------


async def test_auth_header_present_when_token_set() -> None:
    mock_client = _make_http_client()
    with patch("sentinel.notify.ntfy.httpx.AsyncClient", return_value=mock_client):
        notifier = NtfyNotifier(_enabled_settings(token="my-token"))
        await notifier.send_detection_alert(0.5)

    call_kwargs = mock_client.post.call_args.kwargs
    headers = call_kwargs.get("headers", {})
    assert headers.get("Authorization") == "Bearer my-token"


async def test_no_auth_header_when_no_token() -> None:
    mock_client = _make_http_client()
    with patch("sentinel.notify.ntfy.httpx.AsyncClient", return_value=mock_client):
        notifier = NtfyNotifier(_enabled_settings(token=None))
        await notifier.send_detection_alert(0.5)

    call_kwargs = mock_client.post.call_args.kwargs
    headers = call_kwargs.get("headers", {})
    assert "Authorization" not in headers


# ---------------------------------------------------------------------------
# Priority and tags
# ---------------------------------------------------------------------------


async def test_detection_alert_high_priority() -> None:
    mock_client = _make_http_client()
    with patch("sentinel.notify.ntfy.httpx.AsyncClient", return_value=mock_client):
        notifier = NtfyNotifier(_enabled_settings())
        await notifier.send_detection_alert(0.5)

    headers = mock_client.post.call_args.kwargs.get("headers", {})
    assert headers.get("Priority") == "high"


# ---------------------------------------------------------------------------
# Retry on RequestError
# ---------------------------------------------------------------------------


async def test_retry_on_request_error() -> None:
    call_count = 0

    async def _flaky_post(*args: object, **kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise httpx.ConnectError("refused")
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        return resp

    mock_client = MagicMock()
    mock_client.post = _flaky_post
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("sentinel.notify.ntfy.httpx.AsyncClient", return_value=mock_client):
        notifier = NtfyNotifier(_enabled_settings())
        await notifier.send_detection_alert(0.5)

    assert call_count == 2


async def test_retry_exhausted_reraises() -> None:
    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("always fails"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("sentinel.notify.ntfy.httpx.AsyncClient", return_value=mock_client):
        notifier = NtfyNotifier(_enabled_settings())
        with pytest.raises(httpx.ConnectError):
            await notifier.send_detection_alert(0.5)


async def test_detection_alert_with_photo_uploads() -> None:
    mock_client = _make_http_client()
    with patch("sentinel.notify.ntfy.httpx.AsyncClient", return_value=mock_client):
        notifier = NtfyNotifier(_enabled_settings())
        await notifier.send_detection_alert(0.85, jpeg=b"test_image_data")

    mock_client.post.assert_called_once()
    call_kwargs = mock_client.post.call_args.kwargs
    assert call_kwargs.get("content") == b"test_image_data"
    headers = call_kwargs.get("headers", {})
    assert headers.get("X-Filename") == "snapshot.jpg"
    x_msg = headers.get("X-Message", "")
    if x_msg.startswith("=?utf-8?B?"):
        import base64

        x_msg = base64.b64decode(x_msg[10:-2]).decode("utf-8")
    assert "85%" in x_msg


async def test_detection_alert_loads_photo_from_disk(tmp_path: Any) -> None:
    db_path = str(tmp_path / "sentinel.db")
    settings = Settings(
        printer_ip="10.0.0.1",
        ntfy_url="https://my-ntfy.local/test",
        db_path=db_path,
    )
    # Save a fake snapshot file
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    (snapshots_dir / "mysnap.jpg").write_bytes(b"disk_jpeg_data")

    mock_client = _make_http_client()
    with patch("sentinel.notify.ntfy.httpx.AsyncClient", return_value=mock_client):
        notifier = NtfyNotifier(settings)
        await notifier.send_detection_alert(0.85, snapshot_id="mysnap")

    mock_client.post.assert_called_once()
    call_kwargs = mock_client.post.call_args.kwargs
    assert call_kwargs.get("content") == b"disk_jpeg_data"


async def test_detection_alert_disk_read_failure(tmp_path: Any) -> None:
    db_path = str(tmp_path / "sentinel.db")
    settings = Settings(
        printer_ip="10.0.0.1",
        ntfy_url="https://my-ntfy.local/test",
        db_path=db_path,
    )
    # Don't create the file, so it fails to find/read it.
    mock_client = _make_http_client()
    with patch("sentinel.notify.ntfy.httpx.AsyncClient", return_value=mock_client):
        notifier = NtfyNotifier(settings)
        await notifier.send_detection_alert(0.85, snapshot_id="nonexistent")

    mock_client.post.assert_called_once()
    call_kwargs = mock_client.post.call_args.kwargs
    # Content should default to message because snapshot read failed
    headers = call_kwargs.get("headers", {})
    assert "X-Filename" not in headers
    assert "Confidence 85%." in call_kwargs.get("content", "")


async def test_send_text_calls_post() -> None:
    settings = Settings(printer_ip="10.0.0.1", ntfy_url="https://my-ntfy.local/test")
    mock_client = _make_http_client()
    with patch("sentinel.notify.ntfy.httpx.AsyncClient", return_value=mock_client):
        notifier = NtfyNotifier(settings)
        await notifier.send_text("hello")
    mock_client.post.assert_called_once()
    assert mock_client.post.call_args.kwargs.get("content") == "hello"


async def test_send_print_started_alert() -> None:
    settings = Settings(printer_ip="10.0.0.1", ntfy_url="https://my-ntfy.local/test")
    mock_client = _make_http_client()
    with patch("sentinel.notify.ntfy.httpx.AsyncClient", return_value=mock_client):
        notifier = NtfyNotifier(settings)
        await notifier.send_print_started_alert("file.gcode", b"jpeg")
    mock_client.post.assert_called_once()


async def test_send_print_completed_alert() -> None:
    settings = Settings(printer_ip="10.0.0.1", ntfy_url="https://my-ntfy.local/test")
    mock_client = _make_http_client()
    with patch("sentinel.notify.ntfy.httpx.AsyncClient", return_value=mock_client):
        notifier = NtfyNotifier(settings)
        await notifier.send_print_completed_alert("file.gcode", 3661.0, b"jpeg")
    mock_client.post.assert_called_once()


async def test_send_external_pause_alert() -> None:
    settings = Settings(printer_ip="10.0.0.1", ntfy_url="https://my-ntfy.local/test")
    mock_client = _make_http_client()
    with patch("sentinel.notify.ntfy.httpx.AsyncClient", return_value=mock_client):
        notifier = NtfyNotifier(settings)
        await notifier.send_external_pause_alert(b"jpeg")
    mock_client.post.assert_called_once()


async def test_ntfy_rfc2047_header_encoding() -> None:
    import base64

    settings = Settings(printer_ip="10.0.0.1", ntfy_url="https://my-ntfy.local/test")
    mock_client = _make_http_client()

    with patch("sentinel.notify.ntfy.httpx.AsyncClient", return_value=mock_client):
        notifier = NtfyNotifier(settings)
        # Message with newlines and non-ASCII (emojis) via print started alert
        await notifier.send_print_started_alert("My\nFile 🚀", b"jpeg_data")

    mock_client.post.assert_called_once()
    headers = mock_client.post.call_args.kwargs.get("headers", {})

    # Check X-Message is encoded
    x_msg = headers.get("X-Message", "")
    assert x_msg.startswith("=?utf-8?B?")
    assert x_msg.endswith("?=")

    # Decode it and verify
    encoded_payload = x_msg[10:-2]
    decoded_msg = base64.b64decode(encoded_payload.encode()).decode("utf-8")
    assert "My" in decoded_msg
    assert "\n" in decoded_msg
    assert "🚀" in decoded_msg


async def test_ntfy_no_encoding_for_pure_ascii() -> None:
    settings = Settings(printer_ip="10.0.0.1", ntfy_url="https://my-ntfy.local/test")
    mock_client = _make_http_client()

    with patch("sentinel.notify.ntfy.httpx.AsyncClient", return_value=mock_client):
        notifier = NtfyNotifier(settings)
        # Message without newlines and pure ASCII
        await notifier.send_camera_offline_alert()

    mock_client.post.assert_called_once()
    headers = mock_client.post.call_args.kwargs.get("headers", {})
    title = headers.get("Title", "")
    assert not title.startswith("=?utf-8?B?")
    assert title == "Camera offline"


async def test_ntfy_edge_cases(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Public ntfy.sh privacy check value error
    settings_public = Settings(
        printer_ip="10.0.0.1",
        ntfy_url="https://ntfy.sh/my-topic",
        ntfy_enabled=True,
        ntfy_token=None,
    )
    with pytest.raises(ValueError, match="PRIVACY RISK"):
        NtfyNotifier(settings_public)

    # 2. OSError during reading snapshot
    db_path = str(tmp_path / "sentinel.db")
    settings = Settings(
        printer_ip="10.0.0.1",
        ntfy_url="https://my-ntfy.local/test",
        db_path=db_path,
    )
    # Save a fake snapshot file
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    p = snapshots_dir / "mysnap.jpg"
    p.write_bytes(b"disk_jpeg_data")

    # Mock read_bytes to raise OSError
    from pathlib import Path

    orig_read_bytes = Path.read_bytes

    def mock_read_bytes(self: Path) -> bytes:
        if "mysnap" in str(self):
            raise OSError("read error")
        return orig_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", mock_read_bytes)

    mock_client = _make_http_client()
    with patch("sentinel.notify.ntfy.httpx.AsyncClient", return_value=mock_client):
        notifier = NtfyNotifier(settings)
        await notifier.send_detection_alert(0.85, snapshot_id="mysnap")
    # Verify post was called even though disk read raised OSError (which was swallowed)
    mock_client.post.assert_called_once()

    # 3. disabled alerts checks
    notifier_disabled = NtfyNotifier(_disabled_settings())
    # calling methods when disabled must return immediately (noop)
    await notifier_disabled.send_text("hello")
    await notifier_disabled.send_print_started_alert("file.gcode")
    await notifier_disabled.send_print_completed_alert("file.gcode", 10.0)
    await notifier_disabled.send_external_pause_alert()

    # 4. close method
    mock_client_close = _make_http_client()
    mock_client_close.aclose = AsyncMock()
    with patch("sentinel.notify.ntfy.httpx.AsyncClient", return_value=mock_client_close):
        notifier = NtfyNotifier(settings)
        await notifier.close()
    mock_client_close.aclose.assert_called_once()
