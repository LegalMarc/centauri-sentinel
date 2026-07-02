"""Tests for sentinel/camera/mjpeg.py."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import httpx
import pytest

from sentinel.camera.errors import CameraOfflineError, CameraReadError
from sentinel.camera.mjpeg import MjpegGrabber, _extract_jpeg, _format_host_for_url
from sentinel.config import Settings

_SETTINGS = Settings(printer_ip="10.0.0.1", printer_access_code="test")

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
# _format_host_for_url unit tests
# ---------------------------------------------------------------------------


def test_format_host_for_url_brackets_ipv6() -> None:
    assert _format_host_for_url("fd00::1234") == "[fd00::1234]"


def test_format_host_for_url_leaves_ipv4_unchanged() -> None:
    assert _format_host_for_url("192.168.1.10") == "192.168.1.10"


def test_format_host_for_url_leaves_hostname_unchanged() -> None:
    assert _format_host_for_url("printer.local") == "printer.local"


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


# ---------------------------------------------------------------------------
# reconfigure() — new client is created, grab() works after reconfigure
# ---------------------------------------------------------------------------


def _make_httpx_client_with_aclose(resp: MagicMock) -> MagicMock:
    """Like _make_httpx_client but also mocks aclose() as a coroutine."""
    client = _make_httpx_client(resp)
    client.aclose = AsyncMock()
    return client


async def test_reconfigure_grab_reconnects() -> None:
    """After reconfigure(new_url), grab() must connect to the new URL
    and return frames without raising RuntimeError from a closed client."""
    import asyncio

    resp1 = _make_stream_response([_JPEG])
    mock_client1 = _make_httpx_client_with_aclose(resp1)

    resp2 = _make_stream_response([_JPEG])
    mock_client2 = _make_httpx_client_with_aclose(resp2)

    new_url = "http://10.0.0.2:8080/webcam/?action=stream"

    client_instances: list[object] = []

    def _client_factory(**_kwargs: object) -> object:
        if not client_instances:
            client_instances.append(mock_client1)
            return mock_client1
        client_instances.append(mock_client2)
        return mock_client2

    with patch("sentinel.camera.mjpeg.httpx.AsyncClient", side_effect=_client_factory):
        grabber = MjpegGrabber(_SETTINGS)

        # First grab — uses original URL / client1
        frame1 = await grabber.grab()
        assert frame1 == _JPEG

        # Cancel broadcaster so reconfigure can close cleanly
        if grabber._broadcaster_task and not grabber._broadcaster_task.done():
            grabber._broadcaster_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await grabber._broadcaster_task

        # Reconfigure to a new URL — must create a fresh client
        await grabber.reconfigure(new_url)
        assert grabber._url == new_url

        # Second grab — must not raise RuntimeError("client has been closed")
        frame2 = await grabber.grab()
        assert frame2 == _JPEG

    # Two AsyncClient instances must have been created
    assert len(client_instances) == 2


# ---------------------------------------------------------------------------
# close() — full queue: sentinel still delivered (CameraClosedError not dropped)
# ---------------------------------------------------------------------------


async def test_close_full_queue_delivers_sentinel() -> None:
    """close() must guarantee CameraClosedError even when the listener queue is full."""
    import asyncio

    from sentinel.camera.errors import CameraClosedError

    grabber = MjpegGrabber(_SETTINGS)
    q: asyncio.Queue[object] = asyncio.Queue(maxsize=2)
    # Fill the queue to capacity with dummy frames
    q.put_nowait(b"frame1")
    q.put_nowait(b"frame2")
    assert q.full()

    grabber._listeners.add(q)
    await grabber.close()

    # The sentinel must be present in the queue
    items = []
    while not q.empty():
        items.append(q.get_nowait())

    assert any(isinstance(item, CameraClosedError) for item in items), (
        f"CameraClosedError not found in queue items: {items}"
    )


# ---------------------------------------------------------------------------
# Regression: buffer-overflow CameraReadError must count toward the offline
# threshold (previously it bypassed the except clause that bumps
# _consecutive_failures, so is_connected stayed True forever).
# ---------------------------------------------------------------------------


async def test_grab_buffer_overflow_increments_consecutive_failures() -> None:
    chunks = [b"a" * 8192] * 1350
    resp = _make_stream_response(chunks)
    mock_client = _make_httpx_client(resp)

    with patch("sentinel.camera.mjpeg.httpx.AsyncClient", return_value=mock_client):
        grabber = MjpegGrabber(_SETTINGS)
        with pytest.raises(CameraReadError) as exc_info:
            await grabber.grab()
        assert "limit exceeded" in str(exc_info.value)

    assert grabber._consecutive_failures == 1


async def test_grab_buffer_overflow_reaches_offline_after_threshold() -> None:
    chunks = [b"a" * 8192] * 1350
    resp = _make_stream_response(chunks)
    mock_client = _make_httpx_client(resp)

    with patch("sentinel.camera.mjpeg.httpx.AsyncClient", return_value=mock_client):
        grabber = MjpegGrabber(_SETTINGS)
        grabber._consecutive_failures = 2  # one more buffer-overflow hits the threshold

        with pytest.raises(CameraOfflineError):
            await grabber.grab()

    assert grabber.is_connected is False


# ---------------------------------------------------------------------------
# Regression: stream_proxy() restarting a dead broadcaster must clear stale
# _latest_frame/_latest_exception, mirroring grab() — otherwise a concurrent
# grab() could observe a stale exception left over from the dead task.
# ---------------------------------------------------------------------------


async def test_stream_proxy_clears_stale_state_before_restarting_broadcaster() -> None:
    import asyncio

    resp = _make_stream_response([])  # no frames; idle-but-connected, like a live stream
    mock_client = _make_httpx_client(resp)

    with patch("sentinel.camera.mjpeg.httpx.AsyncClient", return_value=mock_client):
        grabber = MjpegGrabber(_SETTINGS)

        async def _dead() -> None:
            return None

        dead_task = asyncio.create_task(_dead())
        await dead_task
        assert dead_task.done()

        grabber._broadcaster_task = dead_task
        grabber._latest_exception = CameraOfflineError("stale from previous dead task")
        grabber._latest_frame = b"stale frame"

        agen = grabber.stream_proxy()
        anext_task = asyncio.create_task(agen.__anext__())
        # Let stream_proxy() run its synchronous restart-and-clear logic and
        # suspend on its (empty) queue wait.
        await asyncio.sleep(0.1)

        assert grabber._latest_exception is None
        assert grabber._latest_frame is None

        anext_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await anext_task


# ---------------------------------------------------------------------------
# Regression: close() racing with grab()'s wait loop (setting
# self._broadcaster_task = None between polling iterations) must not raise an
# unhandled AttributeError.
# ---------------------------------------------------------------------------


async def test_grab_survives_concurrent_close_during_wait_loop() -> None:
    import asyncio

    resp = _make_stream_response([])  # never produces a frame within this test
    mock_client = _make_httpx_client_with_aclose(resp)

    with patch("sentinel.camera.mjpeg.httpx.AsyncClient", return_value=mock_client):
        grabber = MjpegGrabber(_SETTINGS)
        grab_task = asyncio.create_task(grabber.grab())
        await asyncio.sleep(0.15)  # let grab() start polling in its wait loop
        assert grabber._broadcaster_task is not None

        await grabber.close()  # concurrently clears self._broadcaster_task mid-poll

        # Must raise a typed camera exception, not AttributeError.
        with pytest.raises(CameraReadError):
            await grab_task


# ---------------------------------------------------------------------------
# Regression: several complete JPEG frames arriving back-to-back in a single
# chunk must all reach a keeping-up listener — _broadcast_loop must yield to
# the event loop between frames so the maxsize=2 queue's "drop oldest" logic
# doesn't evict frames purely due to scheduling starvation.
# ---------------------------------------------------------------------------


async def test_stream_proxy_burst_of_frames_all_delivered() -> None:
    import asyncio

    def _tagged_jpeg(tag: int) -> bytes:
        return b"\xff\xd8\xff\xe0" + bytes([tag]) + b"\xff\xd9"

    frames = [_tagged_jpeg(i) for i in range(5)]
    burst_chunk = b"".join(frames)

    resp = _make_stream_response([burst_chunk])
    mock_client = _make_httpx_client(resp)

    frames_captured: list[bytes] = []

    with patch("sentinel.camera.mjpeg.httpx.AsyncClient", return_value=mock_client):
        grabber = MjpegGrabber(_SETTINGS)

        async def _consume() -> None:
            async for frame in grabber.stream_proxy():
                frames_captured.append(frame)
                if len(frames_captured) == len(frames):
                    break

        await asyncio.wait_for(_consume(), timeout=2.0)

    assert frames_captured == frames


# ---------------------------------------------------------------------------
# Regression: IPv6 printer_ip must be bracketed everywhere a URL is built, so
# a literal IPv6 address doesn't get mis-parsed by urlparse.
# ---------------------------------------------------------------------------


def test_init_brackets_ipv6_printer_ip() -> None:
    settings = Settings(printer_ip="fd00::1234", printer_access_code="test")
    grabber = MjpegGrabber(settings)
    assert grabber._url == "http://[fd00::1234]:8080/mjpeg"


async def test_stream_proxy_internal_brackets_resolved_ipv6() -> None:
    """The netloc rebuilt after resolve_and_validate_printer_ip() must also be
    bracketed, or the fetch URL is unparseable/wrong for an IPv6 printer."""
    settings = Settings(printer_ip="fd00::1234", printer_access_code="test")
    resp = _make_stream_response([_JPEG])
    mock_client = _make_httpx_client(resp)

    with patch("sentinel.camera.mjpeg.httpx.AsyncClient", return_value=mock_client):
        grabber = MjpegGrabber(settings)
        frame = await grabber.grab()

    assert frame == _JPEG
    fetched_url = mock_client.stream.call_args.args[1]
    assert fetched_url == "http://[fd00::1234]:8080/mjpeg"
