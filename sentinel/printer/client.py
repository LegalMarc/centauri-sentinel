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


def _parse_status(payload: dict[str, Any]) -> PrinterStatus:
    """Extract PrinterStatus from a method-6000 payload."""
    try:
        data: dict[str, Any] = payload.get("data", payload)
        attrs: dict[str, Any] = data.get("Attributes", {})
        # CurrentStatus: 0=idle, 1=printing
        printing = int(attrs.get("CurrentStatus", 0)) == 1
        elapsed = float(attrs.get("PrintTime", 0))
        current_layer = int(attrs.get("CurrentLayer", 0))
        total_layers = int(attrs.get("TotalLayer", 0))
        filename: str | None = attrs.get("Filename") or None
        return PrinterStatus(
            printing=printing,
            elapsed_seconds=elapsed,
            current_layer=current_layer,
            total_layers=total_layers,
            filename=filename,
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_status(self) -> PrinterStatus:
        """Connect, wait for one status push, return parsed status."""
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
                async with asyncio.timeout(_TIMEOUT_S):
                    async for message in client.messages:
                        try:
                            payload: dict[str, Any] = json.loads(message.payload)
                        except json.JSONDecodeError as exc:
                            raise PrinterProtocolError(f"Bad JSON: {exc}") from exc
                        if payload.get("method") == METHOD_STATUS_PUSH:
                            return _parse_status(payload)
        except TimeoutError as exc:
            raise PrinterTimeoutError("Status request timed out") from exc

        raise PrinterProtocolError("MQTT stream ended without a status message")

    async def _send_command(self, msg: dict[str, Any]) -> None:
        """Publish a command and return; does not wait for an ack."""
        try:
            client_id = f"{self._client_id}-cmd-{uuid.uuid4().hex[:8]}"
            async with aiomqtt.Client(
                hostname=self._host,
                port=self._port,
                username="elegoo",
                password=self._access_code,
                identifier=client_id,
            ) as client:
                # TODO(L2): topic uses printer IP as serial; replace with actual
                # serial number once one full print cycle has been observed.
                topic = f"elegoo/{self._host}/{self._client_id}/api_request"
                async with asyncio.timeout(_TIMEOUT_S):
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
