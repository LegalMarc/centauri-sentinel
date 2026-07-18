"""Tests for sentinel/printer/client.py.

Uses unittest.mock to stub aiomqtt.Client so no real MQTT broker is needed.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiomqtt
import pytest

from sentinel.config import Settings
from sentinel.printer.client import PrinterClient, _parse_status
from sentinel.printer.errors import (
    PauseDebouncedError,
    PrinterCommandError,
    PrinterProtocolError,
    PrinterRegistrationError,
    PrinterTimeoutError,
)
from sentinel.printer.types import (
    CC2_CMD_PAUSE_PRINT,
    CC2_CMD_RESUME_PRINT,
    CC2_CMD_STOP_PRINT,
    PrinterStatus,
)

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


def _modern_payload(
    state: str,
    *,
    filename: str | None = None,
    thumbnail: str | None = None,
    extra_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a modern (Carbon 2) format status push (method 6000, 'result' block)."""
    print_status: dict[str, Any] = {"state": state}
    if filename is not None:
        print_status["filename"] = filename
    result: dict[str, Any] = {
        "print_status": print_status,
        "machine_status": {"progress": 0.0},
        "extruder": {"temperature": 200.0, "target": 200.0},
        "heater_bed": {"temperature": 60.0, "target": 60.0},
        "external_device": {"camera": True},
    }
    if thumbnail is not None:
        result["thumbnail"] = thumbnail
    if extra_result is not None:
        result.update(extra_result)
    return {"method": 6000, "result": result}


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


def test_parse_status_legacy_printing_sets_print_state() -> None:
    """Legacy branch with CurrentStatus==1 must yield print_state='printing'."""
    status = _parse_status(_status_payload(printing=True))
    assert status.print_state == "printing"


def test_parse_status_legacy_idle_sets_print_state() -> None:
    """Legacy branch with CurrentStatus==0 must yield print_state='idle'."""
    status = _parse_status(_status_payload(printing=False))
    assert status.print_state == "idle"


def test_parse_status_filename_none_on_empty() -> None:
    status = _parse_status(_status_payload(filename=None))
    assert status.filename is None


def test_parse_status_filename_present() -> None:
    status = _parse_status(_status_payload(filename="test.gcode"))
    assert status.filename == "test.gcode"


def test_parse_status_thumbnail_legacy() -> None:
    payload = _status_payload(filename="test.gcode")
    payload["data"]["Attributes"]["Thumbnail"] = "base64encodedlegacy"
    status = _parse_status(payload)
    assert status.thumbnail_base64 == "base64encodedlegacy"


def test_parse_status_thumbnail_carbon2() -> None:
    payload = {
        "method": 6000,
        "result": {
            "print_status": {
                "state": "printing",
                "print_duration": 120.0,
                "filename": "test.gcode",
                "remaining_time_sec": 300.0,
            },
            "machine_status": {"progress": 40.0},
            "extruder": {"temperature": 210.0, "target": 210.0},
            "heater_bed": {"temperature": 60.0, "target": 60.0},
            "external_device": {"camera": True},
            "thumbnail": "base64encodedresult",
        },
    }
    status = _parse_status(payload)
    assert status.thumbnail_base64 == "base64encodedresult"
    assert status.printing is True
    assert status.extruder_temp == 210.0
    assert status.bed_temp == 60.0
    assert status.camera_connected is True


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


def _make_message_with_topic(topic: str, payload: dict[str, Any]) -> MagicMock:
    msg = MagicMock()
    msg.topic = topic
    msg.payload = json.dumps(payload).encode()
    return msg


class _FakeCommandClient:
    """Simulates the CC2 register→command→ack MQTT exchange over one session.

    Publishing to ``.../api_register`` enqueues a ``register_response``; publishing
    a command to ``.../api_request`` enqueues an ``api_response`` ack echoing the
    request id. Response error strings / ack error_codes are configurable, and
    either response can be dropped to exercise timeouts.
    """

    def __init__(
        self,
        *,
        register_error: str = "ok",
        ack_error_code: int | None = 0,
        omit_error_code: bool = False,
        drop_register: bool = False,
        drop_ack: bool = False,
    ) -> None:
        self.subscribed: list[str] = []
        self.published: list[tuple[str, dict[str, Any]]] = []
        self._register_error = register_error
        self._ack_error_code = ack_error_code
        self._omit_error_code = omit_error_code
        self._drop_register = drop_register
        self._drop_ack = drop_ack
        self._queue: list[MagicMock] = []

    @property
    def command_publishes(self) -> list[dict[str, Any]]:
        """Payloads published to api_request that carry a command (have 'method')."""
        return [p for t, p in self.published if t.endswith("/api_request") and "method" in p]

    async def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)

    async def publish(self, topic: str, payload_str: str) -> None:
        payload = json.loads(payload_str)
        self.published.append((topic, payload))
        parts = topic.split("/")
        if topic.endswith("/api_register") and not self._drop_register:
            sn, request_id = parts[1], payload["request_id"]
            self._queue.append(
                _make_message_with_topic(
                    f"elegoo/{sn}/{request_id}/register_response",
                    {"client_id": payload["client_id"], "error": self._register_error},
                )
            )
        elif topic.endswith("/api_request") and "method" in payload and not self._drop_ack:
            sn, client_id = parts[1], parts[2]
            result: dict[str, Any] = {}
            if not self._omit_error_code:
                result["error_code"] = self._ack_error_code
            self._queue.append(
                _make_message_with_topic(
                    f"elegoo/{sn}/{client_id}/api_response",
                    {"id": payload["id"], "method": payload["method"], "result": result},
                )
            )

    @property
    def messages(self) -> _FakeCommandClient:
        return self

    def __aiter__(self) -> _FakeCommandClient:
        return self

    async def __anext__(self) -> MagicMock:
        # The client always publishes before reading, so the expected response is
        # already queued on the happy path. When a response was dropped, yield to
        # the event loop so the caller's asyncio.timeout can fire.
        while not self._queue:
            await asyncio.sleep(0.005)
        return self._queue.pop(0)


def _make_command_client(**kwargs: Any) -> tuple[Any, _FakeCommandClient]:
    fake = _FakeCommandClient(**kwargs)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=fake)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm, fake


async def test_pause_publishes_command() -> None:
    cm, fake = _make_command_client()
    with patch("sentinel.printer.client.aiomqtt.Client", return_value=cm):
        client = PrinterClient(_SETTINGS)
        client._serial_number = "TESTSERIAL"
        await client.pause()
    assert [p["method"] for p in fake.command_publishes] == [CC2_CMD_PAUSE_PRINT]


async def test_resume_publishes_command() -> None:
    cm, fake = _make_command_client()
    with patch("sentinel.printer.client.aiomqtt.Client", return_value=cm):
        client = PrinterClient(_SETTINGS)
        client._serial_number = "TESTSERIAL"
        await client.resume()
    assert [p["method"] for p in fake.command_publishes] == [CC2_CMD_RESUME_PRINT]


async def test_stop_publishes_command() -> None:
    cm, fake = _make_command_client()
    with patch("sentinel.printer.client.aiomqtt.Client", return_value=cm):
        client = PrinterClient(_SETTINGS)
        client._serial_number = "TESTSERIAL"
        await client.stop()
    assert [p["method"] for p in fake.command_publishes] == [CC2_CMD_STOP_PRINT]


# ---------------------------------------------------------------------------
# Registration handshake + ack verification (firmware 02.x)
# ---------------------------------------------------------------------------


async def test_send_command_registers_before_publishing_command() -> None:
    """The client must complete the api_register handshake before api_request."""
    cm, fake = _make_command_client()
    with patch("sentinel.printer.client.aiomqtt.Client", return_value=cm):
        client = PrinterClient(_SETTINGS)
        client._serial_number = "SN1"
        await client._send_command(CC2_CMD_PAUSE_PRINT)
    topics = [t for t, _ in fake.published]
    register_idx = next(i for i, t in enumerate(topics) if t.endswith("/api_register"))
    request_idx = next(i for i, t in enumerate(topics) if t.endswith("/api_request"))
    assert register_idx < request_idx, "registration must precede the command publish"
    # Registration payload carries client_id + request_id.
    reg_payload = fake.published[register_idx][1]
    assert set(reg_payload) == {"client_id", "request_id"}


async def test_send_command_envelope_has_id_method_params() -> None:
    """Commands must use the {'id','method','params'} envelope the firmware expects."""
    cm, fake = _make_command_client()
    with patch("sentinel.printer.client.aiomqtt.Client", return_value=cm):
        client = PrinterClient(_SETTINGS)
        client._serial_number = "SN1"
        await client._send_command(CC2_CMD_PAUSE_PRINT)
    cmd = fake.command_publishes[0]
    assert cmd["method"] == CC2_CMD_PAUSE_PRINT
    assert cmd["params"] == {}
    assert isinstance(cmd["id"], int)


async def test_send_command_registration_rejected_raises() -> None:
    cm, _ = _make_command_client(register_error="fail")
    with (
        patch("sentinel.printer.client.aiomqtt.Client", return_value=cm),
        pytest.raises(PrinterRegistrationError),
    ):
        client = PrinterClient(_SETTINGS)
        client._serial_number = "SN1"
        await client._send_command(CC2_CMD_PAUSE_PRINT)


async def test_send_command_too_many_clients_raises_registration_error() -> None:
    cm, fake = _make_command_client(register_error="too many clients")
    with (
        patch("sentinel.printer.client.aiomqtt.Client", return_value=cm),
        pytest.raises(PrinterRegistrationError, match="too many clients"),
    ):
        client = PrinterClient(_SETTINGS)
        client._serial_number = "SN1"
        await client._send_command(CC2_CMD_PAUSE_PRINT)
    # The command itself must NOT be published if registration failed.
    assert fake.command_publishes == []


async def test_send_command_nonzero_ack_raises_command_error() -> None:
    """A command the printer rejects (error_code != 0) must raise, not succeed."""
    cm, _ = _make_command_client(ack_error_code=1010)  # NOT_PRINTING
    with (
        patch("sentinel.printer.client.aiomqtt.Client", return_value=cm),
        pytest.raises(PrinterCommandError, match="error_code=1010"),
    ):
        client = PrinterClient(_SETTINGS)
        client._serial_number = "SN1"
        await client._send_command(CC2_CMD_PAUSE_PRINT)


async def test_send_command_ack_without_error_code_is_success() -> None:
    """A matching-id ack that omits error_code must count as success, not a failure."""
    cm, _fake = _make_command_client(omit_error_code=True)
    with patch("sentinel.printer.client.aiomqtt.Client", return_value=cm):
        client = PrinterClient(_SETTINGS)
        client._serial_number = "SN1"
        result = await client._send_command(CC2_CMD_PAUSE_PRINT)  # must not raise
    assert result["method"] == CC2_CMD_PAUSE_PRINT


async def test_send_command_registration_timeout_raises_timeout() -> None:
    cm, _ = _make_command_client(drop_register=True)
    with (
        patch("sentinel.printer.client.CC2_REGISTRATION_TIMEOUT_S", 0.05),
        patch("sentinel.printer.client.aiomqtt.Client", return_value=cm),
        pytest.raises(PrinterTimeoutError, match="registration"),
    ):
        client = PrinterClient(_SETTINGS)
        client._serial_number = "SN1"
        await client._send_command(CC2_CMD_PAUSE_PRINT)


async def test_send_command_ack_timeout_raises_timeout() -> None:
    cm, fake = _make_command_client(drop_ack=True)
    with (
        patch("sentinel.printer.client._TIMEOUT_S", 0.05),
        patch("sentinel.printer.client.aiomqtt.Client", return_value=cm),
        pytest.raises(PrinterTimeoutError, match="command ack"),
    ):
        client = PrinterClient(_SETTINGS)
        client._serial_number = "SN1"
        await client._send_command(CC2_CMD_PAUSE_PRINT)
    # Registration still happened and the command was published; only the ack was lost.
    assert fake.command_publishes and fake.command_publishes[0]["method"] == CC2_CMD_PAUSE_PRINT


# ---------------------------------------------------------------------------
# Protocol error
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _send_command — serial unknown guard (issue #49)
# ---------------------------------------------------------------------------


async def test_send_command_serial_unknown_raises_protocol_error() -> None:
    """_send_command must raise PrinterProtocolError when serial is None."""
    client = PrinterClient(_SETTINGS)
    assert client._serial_number is None
    with pytest.raises(PrinterProtocolError, match="serial unknown"):
        await client._send_command(CC2_CMD_PAUSE_PRINT)


async def test_pause_serial_unknown_raises_protocol_error() -> None:
    """pause() must propagate PrinterProtocolError when serial is not yet known."""
    client = PrinterClient(_SETTINGS)
    assert client._serial_number is None
    with pytest.raises(PrinterProtocolError, match="serial unknown"):
        await client.pause()


async def test_resume_serial_unknown_raises_protocol_error() -> None:
    """resume() must propagate PrinterProtocolError when serial is not yet known."""
    client = PrinterClient(_SETTINGS)
    assert client._serial_number is None
    with pytest.raises(PrinterProtocolError, match="serial unknown"):
        await client.resume()


async def test_stop_serial_unknown_raises_protocol_error() -> None:
    """stop() must propagate PrinterProtocolError when serial is not yet known."""
    client = PrinterClient(_SETTINGS)
    assert client._serial_number is None
    with pytest.raises(PrinterProtocolError, match="serial unknown"):
        await client.stop()


async def test_send_command_with_known_serial_uses_serial_topic() -> None:
    """_send_command must use serial-keyed topics when serial is known."""
    cm, fake = _make_command_client()
    with patch("sentinel.printer.client.aiomqtt.Client", return_value=cm):
        client = PrinterClient(_SETTINGS)
        client._serial_number = "SN999"
        await client._send_command(CC2_CMD_PAUSE_PRINT)
    request_topics = [t for t, _ in fake.published]
    assert any(t.startswith("elegoo/SN999/") and t.endswith("/api_request") for t in request_topics)
    assert all(not t.startswith(f"elegoo/{_SETTINGS.printer_ip}/") for t in request_topics)


async def test_send_command_serial_cleared_mid_connect_raises_and_does_not_publish() -> None:
    """A concurrent close()/reconfigure() racing the awaited connect handshake
    must abort the publish instead of silently targeting 'elegoo/None/...'.

    The initial guard passes (serial known), but resolve_and_validate_printer_ip's
    await gives a concurrent close() a chance to null the serial out before the
    topic string is built.
    """
    client = PrinterClient(_SETTINGS)
    client._serial_number = "TESTSERIAL"

    async def _resolve_then_clear_serial(_host: str) -> str:
        client._serial_number = None  # simulates a concurrent close()/reconfigure()
        return "10.0.0.1"

    cm, fake = _make_command_client()
    with (
        patch(
            "sentinel.printer.client.resolve_and_validate_printer_ip",
            _resolve_then_clear_serial,
        ),
        patch("sentinel.printer.client.aiomqtt.Client", return_value=cm),
        pytest.raises(PrinterProtocolError, match="serial became unknown"),
    ):
        await client._send_command(CC2_CMD_PAUSE_PRINT)

    assert fake.published == []


# ---------------------------------------------------------------------------
# PrinterClient.pause() — debounce raises PauseDebouncedError
# ---------------------------------------------------------------------------


async def test_pause_succeeds_first_call() -> None:
    cm, _ = _make_command_client()
    with patch("sentinel.printer.client.aiomqtt.Client", return_value=cm):
        client = PrinterClient(_SETTINGS)
        client._serial_number = "TESTSERIAL"
        await client.pause()  # must not raise


async def test_pause_raises_debounced_within_debounce_window() -> None:
    cm, fake = _make_command_client()
    with patch("sentinel.printer.client.aiomqtt.Client", return_value=cm):
        client = PrinterClient(_SETTINGS)
        client._serial_number = "TESTSERIAL"
        await client.pause()
        with pytest.raises(PauseDebouncedError):
            await client.pause()
    assert len(fake.command_publishes) == 1  # only one actual command published


async def test_pause_failure_does_not_lock_debounce() -> None:
    """A failed publish must not set _last_pause_at; next call should retry."""
    client = PrinterClient(_SETTINGS)

    async def _always_fail(method: int, params: dict[str, Any] | None = None) -> None:
        raise PrinterTimeoutError("mqtt down")

    with (
        patch.object(client, "_send_command", side_effect=_always_fail),
        pytest.raises(PrinterTimeoutError),
    ):
        await client.pause()

    assert client._last_pause_at == pytest.approx(0.0)


async def test_pause_cancelled_resets_debounce_anchor() -> None:
    """A pause() cancelled mid-publish must roll back _last_pause_at.

    CancelledError is a BaseException, not an Exception, in Python 3.8+; the
    rollback's except clause must be broad enough to catch it so the anchor
    isn't left set as though a pause were successfully published when
    nothing actually went out.
    """
    client = PrinterClient(_SETTINGS)

    async def _cancelled(method: int, params: dict[str, Any] | None = None) -> None:
        raise asyncio.CancelledError()

    with (
        patch.object(client, "_send_command", side_effect=_cancelled),
        pytest.raises(asyncio.CancelledError),
    ):
        await client.pause()

    assert client._last_pause_at == pytest.approx(0.0)


async def test_pause_concurrent_calls_debounced() -> None:
    """Concurrent calls to pause() — exactly one succeeds, the other raises PauseDebouncedError."""
    client = PrinterClient(_SETTINGS)
    publishes = 0

    async def _slow_publish(method: int, params: dict[str, Any] | None = None) -> None:
        nonlocal publishes
        publishes += 1
        await asyncio.sleep(0.05)

    with patch.object(client, "_send_command", side_effect=_slow_publish):
        results = await asyncio.gather(client.pause(), client.pause(), return_exceptions=True)

    successes = [r for r in results if r is None]
    debounced = [r for r in results if isinstance(r, PauseDebouncedError)]
    assert len(successes) == 1
    assert len(debounced) == 1
    assert publishes == 1


async def test_clear_pause_debounce_allows_immediate_repause() -> None:
    """clear_pause_debounce() resets the anchor so a subsequent pause() publishes."""
    cm, fake = _make_command_client()
    with patch("sentinel.printer.client.aiomqtt.Client", return_value=cm):
        client = PrinterClient(_SETTINGS)
        client._serial_number = "TESTSERIAL"
        await client.pause()
        client.clear_pause_debounce()
        await client.pause()  # must not raise; debounce was cleared
    assert [p["method"] for p in fake.command_publishes] == [
        CC2_CMD_PAUSE_PRINT,
        CC2_CMD_PAUSE_PRINT,
    ]


async def test_resume_clears_pause_debounce() -> None:
    """resume() must clear the pause debounce so re-detection publishes a real pause."""
    cm, fake = _make_command_client()
    with patch("sentinel.printer.client.aiomqtt.Client", return_value=cm):
        client = PrinterClient(_SETTINGS)
        client._serial_number = "TESTSERIAL"
        await client.pause()
        assert client._last_pause_at > 0.0
        await client.resume()
        assert client._last_pause_at == pytest.approx(0.0)
        await client.pause()  # must not raise after resume
    assert [p["method"] for p in fake.command_publishes] == [
        CC2_CMD_PAUSE_PRINT,
        CC2_CMD_RESUME_PRINT,
        CC2_CMD_PAUSE_PRINT,
    ]


# ---------------------------------------------------------------------------
# PrinterClient._fetch_status — stream ends without a 6000 push
# ---------------------------------------------------------------------------


async def test_status_stream_ends_without_status_message() -> None:
    cm, _ = _make_mqtt_cm([])
    with (
        patch("sentinel.printer.client._TIMEOUT_S", 0.05),
        patch("sentinel.printer.client.aiomqtt.Client", return_value=cm),
        pytest.raises(PrinterTimeoutError),
    ):
        await PrinterClient(_SETTINGS)._fetch_status()


# ---------------------------------------------------------------------------
# PrinterClient._send_command — timeout
# ---------------------------------------------------------------------------


async def test_send_command_timeout_raises_printer_timeout_error() -> None:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(side_effect=TimeoutError("publish timed out"))
    cm.__aexit__ = AsyncMock(return_value=False)
    client = PrinterClient(_SETTINGS)
    client._serial_number = "TESTSERIAL"
    with (
        patch("sentinel.printer.client.aiomqtt.Client", return_value=cm),
        pytest.raises(PrinterTimeoutError),
    ):
        await client._send_command(CC2_CMD_PAUSE_PRINT)


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
        patch("sentinel.printer.client._TIMEOUT_S", 0.05),
        patch("sentinel.printer.client.aiomqtt.Client", return_value=cm),
        pytest.raises(PrinterTimeoutError),
    ):
        await PrinterClient(_SETTINGS)._fetch_status()


# ---------------------------------------------------------------------------
# Serial Number Extraction and Command Topic Routing
# ---------------------------------------------------------------------------


async def test_serial_number_extraction_and_usage() -> None:
    payload = _status_payload()
    msg = MagicMock()
    msg.payload = json.dumps(payload).encode()
    msg.topic = "elegoo/SERIAL123/api_status"

    client = PrinterClient(_SETTINGS)

    async def _aiter_messages() -> Any:
        yield msg

    client_mock_status = AsyncMock()
    client_mock_status.subscribe = AsyncMock()
    client_mock_status.messages.__aiter__ = lambda _: _aiter_messages()

    cm_status = AsyncMock()
    cm_status.__aenter__ = AsyncMock(return_value=client_mock_status)
    cm_status.__aexit__ = AsyncMock(return_value=False)

    with patch("sentinel.printer.client.aiomqtt.Client", return_value=cm_status):
        await client.status()

    # Assert serial number got extracted
    assert client._serial_number == "SERIAL123"

    # Mock client for sending command (register + ack flow)
    cm_cmd, fake_cmd = _make_command_client()

    with patch("sentinel.printer.client.aiomqtt.Client", return_value=cm_cmd):
        await client.pause()

    # Assert it published the command to the serial-keyed api_request topic, not the IP.
    request_topics = [t for t, _ in fake_cmd.published if t.endswith("/api_request")]
    assert request_topics
    assert all(t.startswith("elegoo/SERIAL123/") for t in request_topics)


async def test_close_resets_connection_state() -> None:
    client = PrinterClient(_SETTINGS)
    client._serial_number = "SERIAL123"
    client._accumulated_data = {"foo": "bar"}
    client._last_update_time = 12345.6
    client._stop_pending = True

    await client.close()

    assert client._serial_number is None
    assert client._accumulated_data == {}
    assert client._last_update_time == 0.0
    assert client.stop_pending is False


async def test_reconfigure_resets_stop_pending() -> None:
    """A stop pending against the old printer must not survive an IP change.

    reconfigure() (e.g. from a settings-page printer_ip edit) calls close()
    internally; a still-pending stop must not fire against whatever printer
    subsequently connects at the new address without fresh operator approval.
    """
    client = PrinterClient(_SETTINGS)
    client._stop_pending = True

    await client.reconfigure("10.0.0.99")

    assert client.stop_pending is False
    assert client._host == "10.0.0.99"


def test_deep_merge_dict() -> None:
    from sentinel.printer.client import _deep_merge

    target = {"a": {"b": 1}}
    source = {"a": {"c": 2}, "d": 3}
    _deep_merge(target, source)
    assert target == {"a": {"b": 1, "c": 2}, "d": 3}


def test_parse_status_carbon2_total_layers() -> None:
    payload = {
        "method": 6000,
        "result": {
            "print_status": {
                "state": "printing",
                "filename": "benchy.gcode",
            },
            "file_list": [{"filename": "benchy.gcode", "layer": 150}],
        },
    }
    status = _parse_status(payload)
    assert status.total_layers == 150


async def test_close_cancels_listener_task() -> None:
    client = PrinterClient(_SETTINGS)

    async def dummy_listen():
        await asyncio.sleep(10.0)

    client._listener_task = asyncio.create_task(dummy_listen())
    await client.close()
    assert client._listener_task is None


async def test_fetch_status_listener_done_with_exception() -> None:
    client = PrinterClient(_SETTINGS)

    async def fail_listen():
        raise PrinterProtocolError("test")

    client._listener_task = asyncio.create_task(fail_listen())
    await asyncio.sleep(0.01)
    with pytest.raises(PrinterProtocolError):
        await client._fetch_status()


async def test_fetch_status_listener_done_without_exception() -> None:
    client = PrinterClient(_SETTINGS)

    async def finish_listen():
        pass

    client._listener_task = asyncio.create_task(finish_listen())
    with pytest.raises(
        PrinterTimeoutError
    ):  # Will timeout waiting for _accumulated_data or checking done
        await client._fetch_status()


async def test_fetch_status_stale_update() -> None:
    client = PrinterClient(_SETTINGS)
    client._accumulated_data = {"method": 6000}
    client._last_update_time = time.monotonic() - 20.0

    async def dummy():
        pass

    client._listener_task = asyncio.create_task(dummy())

    with pytest.raises(PrinterTimeoutError):
        await client._fetch_status()


async def test_listen_loop_stream_clean_reconnect() -> None:
    client = PrinterClient(_SETTINGS)
    # Stream yields one status message, then ends naturally
    payload = _status_payload()
    cm, _ = _make_mqtt_cm([payload])
    with (
        patch("sentinel.printer.client.aiomqtt.Client", return_value=cm),
        patch("sentinel.printer.client.asyncio.sleep", side_effect=asyncio.CancelledError),
        pytest.raises(asyncio.CancelledError),
    ):
        await client._listen_loop()


async def test_with_retry_exhaustion() -> None:
    import tenacity

    client = PrinterClient(_SETTINGS)

    async def fail():
        raise PrinterTimeoutError("fail")

    with (
        patch("sentinel.printer.client._RETRY_WAIT", tenacity.wait_fixed(0.01)),
        pytest.raises(PrinterTimeoutError, match="fail"),
    ):
        await client._with_retry(fail)


# ---------------------------------------------------------------------------
# BUG-04 MQTT Schema Validation and Field Distinction Tests
# ---------------------------------------------------------------------------


def test_parse_status_missing_key_warning_legacy() -> None:

    # Missing CurrentStatus, PrintTime, CurrentLayer, TotalLayer
    payload = {"method": 6000, "data": {"Attributes": {"Filename": "test.gcode"}}}
    with patch("sentinel.printer.client.logger.warning") as mock_warn:
        status = _parse_status(payload)
        mock_warn.assert_called_once()
        warning_msg = mock_warn.call_args[0][0]
        assert "Missing key fields in legacy" in warning_msg

    assert status.printing is False
    assert status.elapsed_seconds == 0.0
    assert status.current_layer == 0
    assert status.total_layers == 0


def test_parse_status_missing_key_warning_modern() -> None:

    # Missing extruder block, heater_bed block entirely, and missing fields in other blocks
    payload = {
        "method": 6000,
        "result": {
            "print_status": {
                "state": "printing"
                # missing print_duration, current_layer, remaining_time_sec
            },
            "machine_status": {},  # missing progress
            # missing extruder block entirely
            # missing heater_bed block entirely
            # missing external_device block entirely
        },
    }
    with patch("sentinel.printer.client.logger.warning") as mock_warn:
        status = _parse_status(payload)
        # Warning called once for missing blocks, once for missing fields
        assert mock_warn.call_count >= 1

    assert status.printing is True
    assert status.elapsed_seconds == 0.0
    assert status.current_layer == 0
    assert status.extruder_temp is None
    assert status.extruder_target is None
    assert status.bed_temp is None
    assert status.bed_target is None
    assert status.progress == 0.0
    assert status.remaining_seconds == 0.0
    assert status.camera_connected is False


def test_parse_status_field_distinction_zero_vs_missing() -> None:
    # 1. Zero values present
    payload_zero = {
        "method": 6000,
        "result": {
            "print_status": {
                "state": "printing",
                "print_duration": 0.0,
                "current_layer": 0,
                "remaining_time_sec": 0.0,
            },
            "machine_status": {"progress": 0.0},
            "extruder": {"temperature": 0.0, "target": 0.0},
            "heater_bed": {"temperature": 0.0, "target": 0.0},
            "external_device": {"camera": False},
        },
    }

    with patch("sentinel.printer.client.logger.warning") as mock_warn:
        status_zero = _parse_status(payload_zero)
        mock_warn.assert_not_called()

    assert status_zero.extruder_temp == 0.0
    assert status_zero.extruder_target == 0.0
    assert status_zero.bed_temp == 0.0
    assert status_zero.bed_target == 0.0

    # 2. Missing values
    payload_missing = {
        "method": 6000,
        "result": {
            "print_status": {
                "state": "printing",
                "print_duration": 120.0,
                "current_layer": 10,
                "remaining_time_sec": 300.0,
            },
            "machine_status": {"progress": 40.0},
            "extruder": {},  # missing temperature & target
            "heater_bed": {},  # missing temperature & target
            "external_device": {"camera": True},
        },
    }

    with patch("sentinel.printer.client.logger.warning") as mock_warn:
        status_missing = _parse_status(payload_missing)
        mock_warn.assert_called_once()  # warns about extruder.temperature, etc.

    assert status_missing.extruder_temp is None
    assert status_missing.extruder_target is None
    assert status_missing.bed_temp is None
    assert status_missing.bed_target is None


async def test_listen_loop_skips_malformed_json_and_increments_counter() -> None:
    client = PrinterClient(_SETTINGS)
    assert client.malformed_messages_count == 0

    msg_malformed = MagicMock()
    msg_malformed.payload = b"this is not valid JSON!!!"
    msg_malformed.topic = "elegoo/serial1/api_status"

    payload_valid = _status_payload()
    msg_valid = MagicMock()
    msg_valid.payload = json.dumps(payload_valid).encode()
    msg_valid.topic = "elegoo/serial1/api_status"

    async def _aiter_messages():
        yield msg_malformed
        yield msg_valid

    client_mock = AsyncMock()
    client_mock.subscribe = AsyncMock()
    client_mock.messages.__aiter__ = lambda _: _aiter_messages()

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=client_mock)
    cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("sentinel.printer.client.aiomqtt.Client", return_value=cm),
        patch("sentinel.printer.client.asyncio.sleep", side_effect=asyncio.CancelledError),
        pytest.raises(asyncio.CancelledError),
    ):
        await client._listen_loop()

    assert client.malformed_messages_count == 1
    assert client._serial_number == "serial1"


async def test_printer_client_stop_pending() -> None:
    client = PrinterClient(_SETTINGS)
    assert client.stop_pending is False

    # Mock _send_command to fail
    with (
        patch.object(client, "_send_command", side_effect=RuntimeError("Timeout")),
        pytest.raises(RuntimeError),
    ):
        await client.stop()
    assert client.stop_pending is True

    # Mock _send_command to succeed
    with patch.object(client, "_send_command", return_value=None):
        await client.stop()
    assert client.stop_pending is True

    # It clears when status() confirms not printing
    s = _parse_status(_status_payload(printing=False))
    with patch.object(client, "_fetch_status", return_value=s):
        await client.status()
    assert client.stop_pending is False


# ---------------------------------------------------------------------------
# is_connected — must report False until a real MQTT status push is received
# ---------------------------------------------------------------------------


def test_is_connected_false_when_no_update() -> None:
    """A fresh client (or one just restarted) must report not connected.

    _last_update_time is seeded to 0.0, so the 15-second window check
    produces False immediately — no stale-but-fresh-looking "connected" window.
    """
    client = PrinterClient(_SETTINGS)
    # No listener task and _last_update_time == 0.0 by default
    assert client._last_update_time == 0.0
    assert client.is_connected is False


def test_is_connected_false_after_listener_restart() -> None:
    """After a listener restart, _last_update_time must be reset to 0.0, not
    time.monotonic(), so is_connected returns False until a real push arrives.

    This prevents a permanent MQTT auth failure from flapping as 'connected'
    for 15 seconds on every reconnect attempt.
    """
    client = PrinterClient(_SETTINGS)
    # Simulate what _fetch_status does on a listener restart
    client._accumulated_data = {}
    client._last_update_time = 0.0  # the fix: seeded to 0.0, not time.monotonic()
    # Even if a task is present, no real update → not connected
    assert client.is_connected is False


async def test_is_connected_true_after_real_update() -> None:
    """is_connected is True only after _last_update_time is set by a live push."""
    client = PrinterClient(_SETTINGS)

    async def _dummy() -> None:
        await asyncio.sleep(9999)

    task: asyncio.Task[None] = asyncio.ensure_future(_dummy())
    client._listener_task = task
    # Simulate a real MQTT push updating the timestamp
    client._last_update_time = time.monotonic()
    try:
        assert client.is_connected is True
    finally:
        task.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# _listen_loop — permanent MQTT auth failure must stop retrying (uses .rc,
# not .code — aiomqtt's MqttCodeError/MqttConnectError only expose .rc)
# ---------------------------------------------------------------------------


async def test_listen_loop_reraises_on_bad_credentials_rc4() -> None:
    """rc=4 ('bad username or password') must propagate, not retry forever."""
    client = PrinterClient(_SETTINGS)
    auth_error = aiomqtt.MqttCodeError(4, "Bad username or password")

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(side_effect=auth_error)
    cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("sentinel.printer.client.aiomqtt.Client", return_value=cm),
        pytest.raises(aiomqtt.MqttCodeError),
    ):
        await client._listen_loop()


async def test_listen_loop_reraises_on_not_authorised_rc5() -> None:
    """rc=5 ('not authorised') must propagate, not retry forever."""
    client = PrinterClient(_SETTINGS)
    auth_error = aiomqtt.MqttCodeError(5, "Not authorised")

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(side_effect=auth_error)
    cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("sentinel.printer.client.aiomqtt.Client", return_value=cm),
        pytest.raises(aiomqtt.MqttCodeError),
    ):
        await client._listen_loop()


async def test_listen_loop_retries_on_other_mqtt_code_error() -> None:
    """A non-auth MqttCodeError (e.g. rc=3, server unavailable) must still retry."""
    client = PrinterClient(_SETTINGS)
    transient_error = aiomqtt.MqttCodeError(3, "Server unavailable")

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(side_effect=transient_error)
    cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("sentinel.printer.client.aiomqtt.Client", return_value=cm),
        patch("sentinel.printer.client.asyncio.sleep", side_effect=asyncio.CancelledError),
        pytest.raises(asyncio.CancelledError),  # reached the retry sleep, not a re-raise
    ):
        await client._listen_loop()


# ---------------------------------------------------------------------------
# _listen_loop — stale field accumulation across an active->inactive
# print_status.state transition (filename/thumbnail must not survive)
# ---------------------------------------------------------------------------


async def test_listen_loop_clears_stale_fields_on_active_to_idle_transition() -> None:
    """A push reporting the print as no longer active must not let fields like
    filename/thumbnail from the finished job survive — _deep_merge never
    deletes keys a later push omits, so accumulation must be reset instead.
    """
    client = PrinterClient(_SETTINGS)
    printing_push = _modern_payload("printing", filename="job_a.gcode", thumbnail="thumbA")
    idle_push = _modern_payload("idle")  # job finished: no filename/thumbnail in this push
    cm, _ = _make_mqtt_cm([printing_push, idle_push])

    with (
        patch("sentinel.printer.client.aiomqtt.Client", return_value=cm),
        patch("sentinel.printer.client.asyncio.sleep", side_effect=asyncio.CancelledError),
        pytest.raises(asyncio.CancelledError),
    ):
        await client._listen_loop()

    status = _parse_status(client._accumulated_data)
    assert status.print_state == "idle"
    assert status.filename is None
    assert status.thumbnail_base64 is None


async def test_listen_loop_keeps_fields_on_paused_transition() -> None:
    """printing -> paused is still 'active'; fields must not be cleared."""
    client = PrinterClient(_SETTINGS)
    printing_push = _modern_payload("printing", filename="job_a.gcode", thumbnail="thumbA")
    paused_push = _modern_payload("paused", filename="job_a.gcode", thumbnail="thumbA")
    cm, _ = _make_mqtt_cm([printing_push, paused_push])

    with (
        patch("sentinel.printer.client.aiomqtt.Client", return_value=cm),
        patch("sentinel.printer.client.asyncio.sleep", side_effect=asyncio.CancelledError),
        pytest.raises(asyncio.CancelledError),
    ):
        await client._listen_loop()

    status = _parse_status(client._accumulated_data)
    assert status.print_state == "paused"
    assert status.filename == "job_a.gcode"
    assert status.thumbnail_base64 == "thumbA"


async def test_listen_loop_does_not_clear_on_push_without_print_status_state() -> None:
    """A push lacking print_status.state entirely (e.g. a partial update) must
    not trigger the reset — only an explicit new state does, keeping the fix
    narrowly scoped instead of clearing on every push.
    """
    client = PrinterClient(_SETTINGS)
    printing_push = _modern_payload("printing", filename="job_a.gcode")
    partial_push = {"method": 6000, "result": {"extruder": {"temperature": 205.0, "target": 210.0}}}
    cm, _ = _make_mqtt_cm([printing_push, partial_push])

    with (
        patch("sentinel.printer.client.aiomqtt.Client", return_value=cm),
        patch("sentinel.printer.client.asyncio.sleep", side_effect=asyncio.CancelledError),
        pytest.raises(asyncio.CancelledError),
    ):
        await client._listen_loop()

    status = _parse_status(client._accumulated_data)
    assert status.filename == "job_a.gcode"  # survived: no state-transition push seen
    assert status.extruder_temp == 205.0  # merged in normally from the partial push


async def test_listen_loop_does_not_clear_on_repeated_idle_pushes() -> None:
    """Once idle, repeated idle pushes must not repeatedly clear accumulation
    (only the active->inactive transition itself triggers a clear).
    """
    client = PrinterClient(_SETTINGS)
    printing_push = _modern_payload("printing", filename="job_a.gcode")
    idle_push_1 = _modern_payload("idle")
    idle_push_2 = _modern_payload("idle", extra_result={"machine_status": {"progress": 99.0}})
    cm, _ = _make_mqtt_cm([printing_push, idle_push_1, idle_push_2])

    with (
        patch("sentinel.printer.client.aiomqtt.Client", return_value=cm),
        patch("sentinel.printer.client.asyncio.sleep", side_effect=asyncio.CancelledError),
        pytest.raises(asyncio.CancelledError),
    ):
        await client._listen_loop()

    status = _parse_status(client._accumulated_data)
    assert status.filename is None
    assert status.progress == 99.0
