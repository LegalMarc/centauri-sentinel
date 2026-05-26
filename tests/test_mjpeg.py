"""Tests for sentinel/camera/mjpeg.py."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import httpx
import pytest

from sentinel.camera.errors import CameraOfflineError, CameraReadError
from sentinel.camera.mjpeg import MjpegGrabber, _extract_jpeg
from sentinel.config import Settings

_SETTINGS = Settings(printer_ip="10.0.0.1")

# Minimal valid JPEG (SOI + 1 byte + EOI)
_JPEG = b"\xff\xd8\xff\xe0JFIF\xff\xd9"


# ---------------------------------------------------------------------------
# _extract_jpeg unit tests
# ---------------------------------------------------------------------------


def test_extract_jpeg_complete() -> None:
    assert _extract_jpeg(_JPEG) is not None


def test_extract_jpeg_no_soi() -> None:
    assert _extract_jpeg(b"no jpeg here") is None


def test_extract_jpeg_soi_no_eoi() -> None:
    assert _extract_jpeg(b"\xff\xd8partial") is None


def test_extract_jpeg_trims_leading_garbage() -> None:
    buf = b"garbage\xff\xd8data\xff\xd9more garbage"
    frame = _extract_jpeg(buf)
    assert frame is not None
    assert frame.startswith(b"\xff\xd8")
    assert frame.endswith(b"\xff\xd9")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stream_response(chunks: list[bytes]) -> MagicMock:
    """Mock httpx streaming response that yields chunks."""

    async def _aiter_bytes(_size: int) -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.aiter_bytes = _aiter_bytes
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_httpx_client(resp: MagicMock) -> MagicMock:
    client = MagicMock()
    client.stream = MagicMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


# ---------------------------------------------------------------------------
# grab() — success
# ---------------------------------------------------------------------------


async def test_grab_success() -> None:
    resp = _make_stream_response([_JPEG])
    mock_client = _make_httpx_client(resp)

    with patch("sentinel.camera.mjpeg.httpx.AsyncClient", return_value=mock_client):
        grabber = MjpegGrabber(_SETTINGS)
        frame = await grabber.grab()

    assert frame == _JPEG
    assert grabber.last_success_utc is not None


async def test_grab_jpeg_split_across_chunks() -> None:
    # JPEG split across two chunks
    half = len(_JPEG) // 2
    chunks = [_JPEG[:half], _JPEG[half:]]
    resp = _make_stream_response(chunks)
    mock_client = _make_httpx_client(resp)

    with patch("sentinel.camera.mjpeg.httpx.AsyncClient", return_value=mock_client):
        grabber = MjpegGrabber(_SETTINGS)
        frame = await grabber.grab()

    assert frame == _JPEG


async def test_grab_resets_consecutive_failures_on_success() -> None:
    resp = _make_stream_response([_JPEG])
    mock_client = _make_httpx_client(resp)

    with patch("sentinel.camera.mjpeg.httpx.AsyncClient", return_value=mock_client):
        grabber = MjpegGrabber(_SETTINGS)
        grabber._consecutive_failures = 2
        await grabber.grab()

    assert grabber._consecutive_failures == 0


# ---------------------------------------------------------------------------
# grab() — single failure (CameraReadError)
# ---------------------------------------------------------------------------


async def test_grab_http_error_raises_camera_read_error() -> None:
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("refused"))
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("sentinel.camera.mjpeg.httpx.AsyncClient", return_value=mock_client):
        grabber = MjpegGrabber(_SETTINGS)
        with pytest.raises(CameraReadError):
            await grabber.grab()


async def test_grab_mid_stream_disconnect_raises_camera_read_error() -> None:
    resp = _make_stream_response([b"\xff\xd8partial"])  # SOI but no EOI
    mock_client = _make_httpx_client(resp)

    with patch("sentinel.camera.mjpeg.httpx.AsyncClient", return_value=mock_client):
        grabber = MjpegGrabber(_SETTINGS)
        with pytest.raises(CameraReadError):
            await grabber.grab()


async def test_grab_increments_consecutive_failures() -> None:
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("refused"))
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("sentinel.camera.mjpeg.httpx.AsyncClient", return_value=mock_client):
        grabber = MjpegGrabber(_SETTINGS)
        with pytest.raises(CameraReadError):
            await grabber.grab()

    assert grabber._consecutive_failures == 1


# ---------------------------------------------------------------------------
# grab() — repeated failures → CameraOfflineError
# ---------------------------------------------------------------------------


async def test_grab_offline_after_threshold() -> None:
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("refused"))
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("sentinel.camera.mjpeg.httpx.AsyncClient", return_value=mock_client):
        grabber = MjpegGrabber(_SETTINGS)
        grabber._consecutive_failures = 2  # one more will hit threshold

        with pytest.raises(CameraOfflineError):
            await grabber.grab()


# ---------------------------------------------------------------------------
# grab() — read timeout
# ---------------------------------------------------------------------------


async def test_grab_timeout_raises_camera_read_error() -> None:
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(side_effect=TimeoutError())
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("sentinel.camera.mjpeg.httpx.AsyncClient", return_value=mock_client):
        grabber = MjpegGrabber(_SETTINGS)
        with pytest.raises(CameraReadError):
            await grabber.grab()


# ---------------------------------------------------------------------------
# last_success_utc — None initially
# ---------------------------------------------------------------------------


def test_last_success_utc_none_initially() -> None:
    grabber = MjpegGrabber(_SETTINGS)
    assert grabber.last_success_utc is None


# ---------------------------------------------------------------------------
# stream_proxy() — yields frames, stops on CameraOfflineError
# ---------------------------------------------------------------------------


async def test_stream_proxy_yields_frames() -> None:
    resp = _make_stream_response([_JPEG, _JPEG, _JPEG])
    mock_client = _make_httpx_client(resp)

    frames_captured: list[bytes] = []

    with patch("sentinel.camera.mjpeg.httpx.AsyncClient", return_value=mock_client):
        grabber = MjpegGrabber(_SETTINGS)
        async for frame in grabber.stream_proxy():
            frames_captured.append(frame)
            if len(frames_captured) == 3:
                break

    assert len(frames_captured) == 3
    assert frames_captured == [_JPEG, _JPEG, _JPEG]


async def test_stream_proxy_backs_off_on_read_error() -> None:
    mock_client1 = MagicMock()
    mock_client1.stream = MagicMock(side_effect=httpx.ConnectError("refused"))
    mock_client1.__aenter__ = AsyncMock(return_value=mock_client1)
    mock_client1.__aexit__ = AsyncMock(return_value=False)

    resp2 = _make_stream_response([_JPEG])
    mock_client2 = _make_httpx_client(resp2)

    client_calls = [mock_client1, mock_client2]

    def _get_client(*args: object, **kwargs: object) -> MagicMock:
        return client_calls.pop(0) if client_calls else mock_client1

    sleep_calls: list[float] = []

    async def _mock_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    with (
        patch("sentinel.camera.mjpeg.httpx.AsyncClient", side_effect=_get_client),
        patch("sentinel.camera.mjpeg.asyncio.sleep", side_effect=_mock_sleep),
    ):
        grabber = MjpegGrabber(_SETTINGS)
        async for frame in grabber.stream_proxy():
            assert frame == _JPEG
            break

    assert len(sleep_calls) == 1
    assert sleep_calls[0] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Buffer limits
# ---------------------------------------------------------------------------


async def test_grab_exceeds_max_buf_bytes_raises_error() -> None:
    # 11 MB of garbage bytes (1300 * 8192 bytes = 10.6 MB)
    chunks = [b"a" * 8192] * 1350
    resp = _make_stream_response(chunks)
    mock_client = _make_httpx_client(resp)

    with patch("sentinel.camera.mjpeg.httpx.AsyncClient", return_value=mock_client):
        grabber = MjpegGrabber(_SETTINGS)
        with pytest.raises(CameraReadError) as exc_info:
            await grabber.grab()
        assert "limit exceeded" in str(exc_info.value)


async def test_stream_proxy_exceeds_max_buf_bytes_raises_error() -> None:
    # 11 MB of garbage bytes
    chunks = [b"a" * 8192] * 1350
    resp = _make_stream_response(chunks)
    mock_client = _make_httpx_client(resp)

    with patch("sentinel.camera.mjpeg.httpx.AsyncClient", return_value=mock_client):
        grabber = MjpegGrabber(_SETTINGS)
        with pytest.raises(CameraReadError) as exc_info:
            async for _ in grabber.stream_proxy():
                pass
        assert "limit exceeded" in str(exc_info.value)
