"""Tests for sentinel/printer/client.py.

Uses unittest.mock to stub aiomqtt.Client so no real MQTT broker is needed.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sentinel.config import Settings
from sentinel.printer.client import PrinterClient, _parse_status
from sentinel.printer.errors import PrinterProtocolError, PrinterTimeoutError
from sentinel.printer.types import PrinterStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SETTINGS = Settings(printer_ip="10.0.0.1", printer_access_code="secret")


def _status_payload(
    *,
    printing: bool = True,
    elapsed: float = 120.0,
    current_layer: int = 10,
    total_layers: int = 100,
    filename: str | None = "benchy.gcode",
) -> dict[str, Any]:
    return {
        "method": 6000,
        "data": {
            "Attributes": {
                "CurrentStatus": 1 if printing else 0,
                "PrintTime": elapsed,
                "CurrentLayer": current_layer,
                "TotalLayer": total_layers,
                "Filename": filename or "",
            }
        },
    }


def _make_message(payload: dict[str, Any]) -> MagicMock:
    msg = MagicMock()
    msg.payload = json.dumps(payload).encode()
    return msg


def _make_mqtt_cm(messages: list[dict[str, Any]]) -> Any:
    """Return a mock that behaves like `async with aiomqtt.Client(...) as client:`."""

    async def _aiter_messages() -> Any:
        for m in messages:
            yield _make_message(m)

    client_mock = AsyncMock()
    client_mock.subscribe = AsyncMock()
    client_mock.publish = AsyncMock()
    client_mock.__aiter__ = lambda _: _aiter_messages()
    client_mock.messages.__aiter__ = lambda _: _aiter_messages()

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=client_mock)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm, client_mock


# ---------------------------------------------------------------------------
# _parse_status unit tests
# ---------------------------------------------------------------------------


def test_parse_status_printing() -> None:
    status = _parse_status(_status_payload(printing=True, elapsed=60.0))
    assert status.printing is True
    assert status.elapsed_seconds == 60.0


def test_parse_status_idle() -> None:
    status = _parse_status(_status_payload(printing=False))
    assert status.printing is False


def test_parse_status_filename_none_on_empty() -> None:
    status = _parse_status(_status_payload(filename=None))
    assert status.filename is None


def test_parse_status_filename_present() -> None:
    status = _parse_status(_status_payload(filename="test.gcode"))
    assert status.filename == "test.gcode"


def test_parse_status_protocol_error() -> None:
    with pytest.raises(PrinterProtocolError):
        _parse_status({"method": 6000, "data": {"Attributes": {"CurrentStatus": "bad"}}})


def test_parse_status_bad_current_status() -> None:
    bad = {"method": 6000, "data": {"Attributes": {"CurrentStatus": [1, 2, 3]}}}
    with pytest.raises(PrinterProtocolError):
        _parse_status(bad)


# ---------------------------------------------------------------------------
# PrinterClient.status() — success
# ---------------------------------------------------------------------------


async def test_status_success() -> None:
    payload = _status_payload(printing=True, elapsed=90.0, current_layer=5, total_layers=50)
    cm, _ = _make_mqtt_cm([payload])

    with patch("sentinel.printer.client.aiomqtt.Client", return_value=cm):
        client = PrinterClient(_SETTINGS)
        s = await client.status()

    assert isinstance(s, PrinterStatus)
    assert s.printing is True
    assert s.elapsed_seconds == 90.0
    assert s.current_layer == 5
    assert s.total_layers == 50


async def test_status_skips_non_6000_messages() -> None:
    non_status = {"method": 1234, "data": {}}
    status_msg = _status_payload(printing=False)
    cm, _ = _make_mqtt_cm([non_status, status_msg])

    with patch("sentinel.printer.client.aiomqtt.Client", return_value=cm):
        client = PrinterClient(_SETTINGS)
        s = await client.status()

    assert isinstance(s, PrinterStatus)


# ---------------------------------------------------------------------------
# PrinterClient.status() — timeout
# ---------------------------------------------------------------------------


async def test_status_timeout() -> None:
    # Simulate a TimeoutError raised by the MQTT connect step
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(side_effect=TimeoutError("connect timed out"))
    cm.__aexit__ = AsyncMock(return_value=False)

    with patch("sentinel.printer.client.aiomqtt.Client", return_value=cm):
        printer = PrinterClient(_SETTINGS)
        with pytest.raises(PrinterTimeoutError):
            await printer._fetch_status()


# ---------------------------------------------------------------------------
# PrinterClient.status() — retry-then-success
# ---------------------------------------------------------------------------


async def test_status_retry_then_success() -> None:
    call_count = 0
    payload = _status_payload()

    async def _fetch_with_first_timeout() -> PrinterStatus:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise PrinterTimeoutError("first attempt")
        return _parse_status(payload)

    client = PrinterClient(_SETTINGS)
    with patch.object(client, "_fetch_status", side_effect=_fetch_with_first_timeout):
        s = await client.status()

    assert call_count == 2
    assert s.printing is True


# ---------------------------------------------------------------------------
# PrinterClient.status() — retry-then-fail
# ---------------------------------------------------------------------------


async def test_status_retry_exhausted() -> None:
    client = PrinterClient(_SETTINGS)
    with (
        patch.object(client, "_fetch_status", side_effect=PrinterTimeoutError("always fails")),
        pytest.raises(PrinterTimeoutError),
    ):
        await client.status()


# ---------------------------------------------------------------------------
# PrinterClient.is_printing() / print_elapsed_seconds()
# ---------------------------------------------------------------------------


async def test_is_printing_true() -> None:
    client = PrinterClient(_SETTINGS)
    s = _parse_status(_status_payload(printing=True))
    with patch.object(client, "status", return_value=s):
        assert await client.is_printing() is True


async def test_is_printing_false() -> None:
    client = PrinterClient(_SETTINGS)
    s = _parse_status(_status_payload(printing=False))
    with patch.object(client, "status", return_value=s):
        assert await client.is_printing() is False


async def test_print_elapsed_seconds() -> None:
    client = PrinterClient(_SETTINGS)
    s = _parse_status(_status_payload(elapsed=300.0))
    with patch.object(client, "status", return_value=s):
        assert await client.print_elapsed_seconds() == 300.0


# ---------------------------------------------------------------------------
# PrinterClient.pause() / resume() / stop() — success
# ---------------------------------------------------------------------------


async def _make_publish_client() -> tuple[Any, Any]:
    client_mock = AsyncMock()
    client_mock.publish = AsyncMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=client_mock)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm, client_mock


async def test_pause_publishes_command() -> None:
    cm, mock_client = await _make_publish_client()
    with patch("sentinel.printer.client.aiomqtt.Client", return_value=cm):
        client = PrinterClient(_SETTINGS)
        await client.pause()
    mock_client.publish.assert_called_once()


async def test_resume_publishes_command() -> None:
    cm, mock_client = await _make_publish_client()
    with patch("sentinel.printer.client.aiomqtt.Client", return_value=cm):
        client = PrinterClient(_SETTINGS)
        await client.resume()
    mock_client.publish.assert_called_once()


async def test_stop_publishes_command() -> None:
    cm, mock_client = await _make_publish_client()
    with patch("sentinel.printer.client.aiomqtt.Client", return_value=cm):
        client = PrinterClient(_SETTINGS)
        await client.stop()
    mock_client.publish.assert_called_once()


# ---------------------------------------------------------------------------
# Protocol error
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PrinterClient.pause() — bool return and debounce
# ---------------------------------------------------------------------------


async def test_pause_returns_true_on_success() -> None:
    cm, _ = await _make_publish_client()
    with patch("sentinel.printer.client.aiomqtt.Client", return_value=cm):
        client = PrinterClient(_SETTINGS)
        result = await client.pause()
    assert result is True


async def test_pause_returns_false_within_debounce_window() -> None:
    cm, mock_client = await _make_publish_client()
    with patch("sentinel.printer.client.aiomqtt.Client", return_value=cm):
        client = PrinterClient(_SETTINGS)
        first = await client.pause()
        second = await client.pause()
    assert first is True
    assert second is False
    assert mock_client.publish.call_count == 1  # only one actual publish


async def test_pause_failure_does_not_lock_debounce() -> None:
    """A failed publish must not set _last_pause_at; next call should retry."""
    client = PrinterClient(_SETTINGS)

    async def _always_fail(msg: dict[str, Any]) -> None:
        raise PrinterTimeoutError("mqtt down")

    with (
        patch.object(client, "_send_command", side_effect=_always_fail),
        pytest.raises(PrinterTimeoutError),
    ):
        await client.pause()

    assert client._last_pause_at == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# PrinterClient._fetch_status — stream ends without a 6000 push
# ---------------------------------------------------------------------------


async def test_status_stream_ends_without_status_message() -> None:
    cm, _ = _make_mqtt_cm([])
    with (
        patch("sentinel.printer.client.aiomqtt.Client", return_value=cm),
        pytest.raises(PrinterProtocolError, match="stream ended"),
    ):
        await PrinterClient(_SETTINGS)._fetch_status()


# ---------------------------------------------------------------------------
# PrinterClient._send_command — timeout
# ---------------------------------------------------------------------------


async def test_send_command_timeout_raises_printer_timeout_error() -> None:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(side_effect=TimeoutError("publish timed out"))
    cm.__aexit__ = AsyncMock(return_value=False)
    with (
        patch("sentinel.printer.client.aiomqtt.Client", return_value=cm),
        pytest.raises(PrinterTimeoutError),
    ):
        await PrinterClient(_SETTINGS)._send_command({"method": 1001})


# ---------------------------------------------------------------------------
# Protocol error (bad JSON)
# ---------------------------------------------------------------------------


async def test_bad_json_raises_protocol_error() -> None:
    bad_msg = MagicMock()
    bad_msg.payload = b"not json"

    client_mock = AsyncMock()
    client_mock.subscribe = AsyncMock()

    async def _messages() -> Any:
        yield bad_msg

    client_mock.messages.__aiter__ = lambda _: _messages()

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=client_mock)
    cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("sentinel.printer.client.aiomqtt.Client", return_value=cm),
        pytest.raises(PrinterProtocolError),
    ):
        await PrinterClient(_SETTINGS)._fetch_status()
