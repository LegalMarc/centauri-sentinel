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
            import asyncio

            await asyncio.sleep(0)
            yield chunk
        # Block forever so the stream doesn't end prematurely, like a real MJPEG stream
        import asyncio

        await asyncio.Event().wait()

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
    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("refused"))
    stream_cm.__aexit__ = AsyncMock(return_value=False)
    mock_client.stream = MagicMock(return_value=stream_cm)

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
    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("refused"))
    stream_cm.__aexit__ = AsyncMock(return_value=False)
    mock_client.stream = MagicMock(return_value=stream_cm)

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
    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("refused"))
    stream_cm.__aexit__ = AsyncMock(return_value=False)
    mock_client.stream = MagicMock(return_value=stream_cm)

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
    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(side_effect=TimeoutError())
    stream_cm.__aexit__ = AsyncMock(return_value=False)
    mock_client.stream = MagicMock(return_value=stream_cm)

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
    stream_cm1 = MagicMock()
    stream_cm1.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("refused"))
    stream_cm1.__aexit__ = AsyncMock(return_value=False)
    mock_client1.stream = MagicMock(return_value=stream_cm1)

    resp2 = _make_stream_response([_JPEG])
    stream_cm2 = resp2

    mock_client = MagicMock()
    mock_client.stream = MagicMock(side_effect=[stream_cm1, stream_cm2, stream_cm2])

    import asyncio

    _real_sleep = asyncio.sleep
    sleep_calls: list[float] = []

    async def _mock_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        await _real_sleep(0)

    with (
        patch("sentinel.camera.mjpeg.httpx.AsyncClient", return_value=mock_client),
        patch("sentinel.camera.mjpeg.asyncio.sleep", side_effect=_mock_sleep),
    ):
        grabber = MjpegGrabber(_SETTINGS)
        async for frame in grabber.stream_proxy():
            assert frame == _JPEG
            break

    assert pytest.approx(0.5) in sleep_calls


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


async def test_grab_generic_exception_wrapped_in_camera_read_error() -> None:
    mock_client = MagicMock()
    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(side_effect=RuntimeError("something went wrong"))
    stream_cm.__aexit__ = AsyncMock(return_value=False)
    mock_client.stream = MagicMock(return_value=stream_cm)

    with patch("sentinel.camera.mjpeg.httpx.AsyncClient", return_value=mock_client):
        grabber = MjpegGrabber(_SETTINGS)
        with pytest.raises(CameraReadError) as exc_info:
            await grabber.grab()
        assert "Grab failed: something went wrong" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, RuntimeError)


async def test_grab_reuses_persistent_connection() -> None:
    resp = _make_stream_response([_JPEG, _JPEG])
    mock_client = _make_httpx_client(resp)

    with patch("sentinel.camera.mjpeg.httpx.AsyncClient", return_value=mock_client):
        grabber = MjpegGrabber(_SETTINGS)

        # Grab twice
        frame1 = await grabber.grab()
        frame2 = await grabber.grab()

    # Ensure only 1 connection stream was opened
    mock_client.stream.assert_called_once()
    assert frame1 == _JPEG
    assert frame2 == _JPEG


async def test_stream_proxy_limit_exceeded() -> None:
    grabber = MjpegGrabber(_SETTINGS)
    # Mock self._listeners with 3 items
    grabber._listeners = {MagicMock(), MagicMock(), MagicMock()}

    with pytest.raises(CameraReadError, match="Max concurrent stream proxies reached"):
        async for _ in grabber.stream_proxy():
            pass


async def test_camera_close_yields_camera_closed_error() -> None:
    import asyncio

    from sentinel.camera.errors import CameraClosedError

    grabber = MjpegGrabber(_SETTINGS)
    q: asyncio.Queue[object] = asyncio.Queue()
    grabber._listeners.add(q)
    await grabber.close()
    item = q.get_nowait()
    assert isinstance(item, CameraClosedError)


async def test_stream_proxy_cancellation_during_grab() -> None:
    import asyncio

    resp = _make_stream_response([])
    mock_client = _make_httpx_client(resp)

    with patch("sentinel.camera.mjpeg.httpx.AsyncClient", return_value=mock_client):
        grabber = MjpegGrabber(_SETTINGS)
        grab_task = asyncio.create_task(grabber.grab())
        await asyncio.sleep(0.1)
        assert grabber._broadcaster_task is not None
        grabber._broadcaster_task.cancel()
        with pytest.raises(CameraReadError) as exc_info:
            await grab_task
        assert "was cancelled" in str(exc_info.value)
