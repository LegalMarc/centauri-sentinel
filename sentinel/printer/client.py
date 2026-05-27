"""Elegoo Centauri Carbon 2 printer client over MQTT.

Protocol notes (from spike, docs/verified-assumptions.md):
- Broker: printer_ip:1883, username "elegoo", password = printer_access_code
- Status topic:  elegoo/<serial>/api_status  (subscribe)
- Request topic: elegoo/<serial>/<client_id>/api_request  (publish)
- Method 6000 = periodic status push (contains all state needed)

The client subscribes and waits for the next status push rather than
issuing a dedicated request, because the printer pushes status ~1 Hz.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, TypeVar

import aiomqtt
import tenacity

from sentinel.printer.errors import PrinterProtocolError, PrinterTimeoutError
from sentinel.printer.types import METHOD_STATUS_PUSH, PrinterStatus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sentinel.config import Settings

_T = TypeVar("_T")

logger = logging.getLogger(__name__)

_TIMEOUT_S = 5.0
_RETRY_ATTEMPTS = 3
_RETRY_WAIT = tenacity.wait_exponential(multiplier=0.5, min=0.5, max=4)
_PAUSE_DEBOUNCE_S = 30.0  # minimum seconds between successive pause() publishes


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Recursively merge dictionary source into target."""
    for key, value in source.items():
        if isinstance(value, dict) and key in target and isinstance(target[key], dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def _parse_status(payload: dict[str, Any]) -> PrinterStatus:
    """Extract PrinterStatus from a method-6000 payload.

    Supports both legacy Attributes and Carbon 2 formats.
    """
    try:
        # 1. Legacy / Mock Attributes format
        data: dict[str, Any] = payload.get("data", payload)
        attrs: dict[str, Any] = data.get("Attributes", {})
        if attrs:
            printing = int(attrs.get("CurrentStatus", 0)) == 1
            elapsed = float(attrs.get("PrintTime", 0))
            current_layer = int(attrs.get("CurrentLayer", 0))
            total_layers = int(attrs.get("TotalLayer", 0))
            filename = attrs.get("Filename") or None
            thumbnail_base64 = attrs.get("Thumbnail") or payload.get("thumbnail") or None
            return PrinterStatus(
                printing=printing,
                elapsed_seconds=elapsed,
                current_layer=current_layer,
                total_layers=total_layers,
                filename=filename,
                thumbnail_base64=thumbnail_base64,
                raw=payload,
            )

        # 2. Modern Elegoo Carbon 2 format
        result: dict[str, Any] = payload.get("result", {})
        print_status = result.get("print_status", {})
        machine_status = result.get("machine_status", {})
        extruder = result.get("extruder", {})
        heater_bed = result.get("heater_bed", {})
        external_device = result.get("external_device", {})

        print_state = print_status.get("state", "idle")
        printing = print_state in ("printing", "paused") or print_status.get("enable") is True

        elapsed_seconds = float(print_status.get("print_duration", 0.0))
        current_layer = int(print_status.get("current_layer", 0))

        # Parse total layers from file_list if present
        filename = print_status.get("filename") or None
        total_layers = 0
        if filename:
            for file_info in result.get("file_list", []):
                if file_info.get("filename") == filename:
                    total_layers = int(file_info.get("layer", 0))
                    break

        extruder_temp = float(extruder.get("temperature", 0.0))
        extruder_target = float(extruder.get("target", 0.0))
        bed_temp = float(heater_bed.get("temperature", 0.0))
        bed_target = float(heater_bed.get("target", 0.0))
        progress = float(machine_status.get("progress", 0.0))
        remaining_seconds = float(print_status.get("remaining_time_sec", 0.0))
        camera_connected = bool(external_device.get("camera", False))

        thumbnail_base64 = payload.get("thumbnail") or result.get("thumbnail") or None

        return PrinterStatus(
            printing=printing,
            elapsed_seconds=elapsed_seconds,
            current_layer=current_layer,
            total_layers=total_layers,
            filename=filename,
            extruder_temp=extruder_temp,
            extruder_target=extruder_target,
            bed_temp=bed_temp,
            bed_target=bed_target,
            progress=progress,
            remaining_seconds=remaining_seconds,
            print_state=print_state,
            camera_connected=camera_connected,
            thumbnail_base64=thumbnail_base64,
            raw=payload,
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise PrinterProtocolError(f"Cannot parse status payload: {exc}") from exc


class PrinterClient:
    """Async client for the Elegoo Centauri Carbon 2 over MQTT."""

    def __init__(self, settings: Settings) -> None:
        self._host = settings.printer_ip
        self._port = settings.printer_mqtt_port
        self._access_code = settings.printer_access_code
        self._client_id = f"sentinel-{uuid.uuid4().hex[:8]}"
        self._last_pause_at: float = 0.0
        self._serial_number: str | None = None
        self._accumulated_data: dict[str, Any] = {}
        self._listener_task: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()
        self._last_update_time: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def status(self) -> PrinterStatus:
        """Return the current printer status.

        Raises PrinterTimeoutError after _TIMEOUT_S seconds.
        Retries up to _RETRY_ATTEMPTS times with exponential backoff.
        """
        return await self._with_retry(self._fetch_status)

    async def is_printing(self) -> bool:
        return (await self.status()).printing

    async def print_elapsed_seconds(self) -> float:
        return (await self.status()).elapsed_seconds

    async def pause(self) -> bool:
        """Send pause command.

        Returns True if the command was published, False if the debounce window
        was active (a pause was already sent within _PAUSE_DEBOUNCE_S seconds).
        _last_pause_at is only updated on a successful publish so a failed
        attempt does not block the next retry.
        """
        now = time.monotonic()
        if now - self._last_pause_at < _PAUSE_DEBOUNCE_S:
            logger.debug("pause() called within debounce window — skipping duplicate publish")
            return False
        await self._with_retry(lambda: self._send_command({"method": 1001}))
        self._last_pause_at = time.monotonic()
        return True

    async def resume(self) -> None:
        """Send resume command."""
        await self._with_retry(lambda: self._send_command({"method": 1002}))

    async def stop(self) -> None:
        """Send stop command.

        CRITICAL: This terminates the active print permanently. Only call
        when the user has given explicit approval. See project safety rules.
        """
        await self._with_retry(lambda: self._send_command({"method": 1003}))

    async def close(self) -> None:
        """Clean up the persistent background listener."""
        if self._listener_task is not None:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
            self._listener_task = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_status(self) -> PrinterStatus:
        """Fetch accumulated status from persistent background listener."""
        if self._listener_task is None or self._listener_task.done():
            if self._listener_task is not None and self._listener_task.done():
                exc = self._listener_task.exception()
                if exc:
                    self._listener_task = None
                    raise exc
            self._listener_task = asyncio.create_task(self._listen_loop())

        if not self._accumulated_data:
            try:
                async with asyncio.timeout(_TIMEOUT_S):
                    while not self._accumulated_data:
                        if self._listener_task.done():
                            exc = self._listener_task.exception()
                            if exc:
                                raise exc
                            break
                        await asyncio.sleep(0.1)
            except TimeoutError as exc:
                raise PrinterTimeoutError("Status request timed out") from exc

        now = time.monotonic()
        if now - self._last_update_time > 15.0:
            raise PrinterTimeoutError("Status request timed out")

        async with self._state_lock:
            return _parse_status(self._accumulated_data)

    async def _listen_loop(self) -> None:
        """Background loop that maintains a persistent connection to MQTT."""
        delay = 0.5
        has_received = False
        while True:
            try:
                client_id = f"{self._client_id}-status-{uuid.uuid4().hex[:8]}"
                async with aiomqtt.Client(
                    hostname=self._host,
                    port=self._port,
                    username="elegoo",
                    password=self._access_code,
                    identifier=client_id,
                ) as client:
                    await client.subscribe("elegoo/+/api_status")
                    delay = 0.5  # reset backoff on successful connect

                    stream_empty = True
                    async for message in client.messages:
                        stream_empty = False
                        try:
                            payload: dict[str, Any] = json.loads(message.payload)
                        except json.JSONDecodeError as exc:
                            raise PrinterProtocolError(f"Bad JSON: {exc}") from exc

                        if payload.get("method") == METHOD_STATUS_PUSH:
                            has_received = True
                            parts = str(message.topic).split("/")
                            if len(parts) >= 2:
                                self._serial_number = parts[1]

                            content = payload.get("result") or payload.get("data") or {}
                            async with self._state_lock:
                                if "result" not in self._accumulated_data:
                                    self._accumulated_data["result"] = {}
                                if "data" not in self._accumulated_data:
                                    self._accumulated_data["data"] = {}
                                if "method" not in self._accumulated_data:
                                    self._accumulated_data["method"] = METHOD_STATUS_PUSH

                                _deep_merge(self._accumulated_data["result"], content)
                                _deep_merge(self._accumulated_data["data"], content)

                                # Merge other keys
                                for k, v in payload.items():
                                    if k not in ("result", "data"):
                                        self._accumulated_data[k] = v

                                self._last_update_time = time.monotonic()

                    if stream_empty or not has_received:
                        raise PrinterProtocolError("MQTT stream ended without a status message")

                    logger.debug(
                        "Printer MQTT status stream ended cleanly. Reconnecting..."
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30.0)
            except asyncio.CancelledError:
                raise
            except (PrinterProtocolError, PrinterTimeoutError):
                raise
            except Exception as exc:
                logger.warning("Printer MQTT status connection failed: %s. Reconnecting...", exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    async def _send_command(self, msg: dict[str, Any]) -> None:
        """Publish a command and return; does not wait for an ack."""
        try:
            client_id = f"{self._client_id}-cmd-{uuid.uuid4().hex[:8]}"
            async with asyncio.timeout(_TIMEOUT_S):
                async with aiomqtt.Client(
                    hostname=self._host,
                    port=self._port,
                    username="elegoo",
                    password=self._access_code,
                    identifier=client_id,
                ) as client:
                    serial = self._serial_number or self._host
                    topic = f"elegoo/{serial}/{self._client_id}/api_request"
                    await client.publish(topic, json.dumps(msg))
        except TimeoutError as exc:
            raise PrinterTimeoutError("Command publish timed out") from exc

    async def _with_retry(self, fn: Callable[[], Awaitable[_T]]) -> _T:
        retryer = tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(_RETRY_ATTEMPTS),
            wait=_RETRY_WAIT,
            retry=tenacity.retry_if_exception_type(PrinterTimeoutError),
            reraise=True,
        )
        async for attempt in retryer:
            with attempt:
                return await fn()
        raise PrinterTimeoutError("Retries exhausted")  # unreachable, satisfies mypy
