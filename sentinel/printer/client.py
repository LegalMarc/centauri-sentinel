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
import copy
import json
import logging
import secrets
import time
import uuid
from collections import deque
from typing import TYPE_CHECKING, Any, TypeVar

import aiomqtt
import tenacity

from sentinel.network import HostResolutionError, resolve_and_validate_printer_ip
from sentinel.printer.errors import (
    PauseDebouncedError,
    PrinterCommandError,
    PrinterCommandUnacknowledgedError,
    PrinterProtocolError,
    PrinterRegistrationError,
    PrinterTimeoutError,
)
from sentinel.printer.types import (
    CC2_ACK_OK,
    CC2_CMD_GET_STATUS,
    CC2_CMD_PAUSE_PRINT,
    CC2_CMD_RESUME_PRINT,
    CC2_CMD_STOP_PRINT,
    CC2_REG_OK,
    CC2_REG_TOO_MANY_CLIENTS,
    CC2_REGISTRATION_TIMEOUT_S,
    METHOD_STATUS_PUSH,
    PrinterStatus,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sentinel.config import Settings

_T = TypeVar("_T")

logger = logging.getLogger(__name__)

_TIMEOUT_S = 5.0
# Wait for a command ack. Matches the reference implementation's
# CC2_COMMAND_TIMEOUT (danielcherubini/elegoo-homeassistant cc2/const.py); a
# pause on a busy printer can take longer than the 5 s connect/status timeout.
_COMMAND_ACK_TIMEOUT_S = 10.0
_RETRY_ATTEMPTS = 3
_RETRY_WAIT = tenacity.wait_exponential(multiplier=0.5, min=0.5, max=4)
_PAUSE_DEBOUNCE_S = 30.0  # minimum seconds between successive pause() publishes
# Treat the status link as dead (reconnect, report stale) after this much push silence.
# An idle printer pushes only small deltas, with live gaps of up to 12 s; the print-time
# cadence is not captured yet. Pause/stop use their own sessions, and MQTT keepalive
# notices a dead TCP link, so a short limit would only add reconnects (and snapshots).
_STATUS_SILENCE_S = 45.0
# After a reconnect, the previous connection's state may still answer "printing" (never
# "idle") until the new snapshot loads, if its last push is at most this old.
_PROVISIONAL_MAX_S = 60.0
# GET_STATUS snapshots each register one command session, and the printer's client
# slots are shared with pause/stop. Retry a failed one after this long, and never
# register more often than the minimum interval or the hourly cap, across reconnects.
_SNAPSHOT_RETRY_S = 30.0
_SNAPSHOT_MIN_INTERVAL_S = 5.0
_SNAPSHOT_MAX_PER_HOUR = 30
# While a pause/resume/stop is in flight, wait this long before trying a snapshot.
_SNAPSHOT_CONTROL_BACKOFF_S = 2.0
# machine_status.status reported by an idle printer (live probe 2026-10-21).
_MACHINE_STATUS_IDLE = 1


def _generate_cc2_ids() -> tuple[str, str]:
    """Generate a (client_id, request_id) pair for the CC2 registration handshake.

    Formats mirror the Elegoo web interface / reference implementation
    (danielcherubini/elegoo-homeassistant) because the firmware is known to be
    picky about the shape of these identifiers:

    - client_id:  ``0cli`` + last 5 hex of the ms timestamp + a few random hex,
      truncated to 10 chars.
    - request_id: a 16-char UUID-like hex string + the ms timestamp in hex.
    """
    ms = int(time.time() * 1000)
    client_id = f"0cli{format(ms, 'x')[-5:]}{format(secrets.randbelow(4096), 'x')}"[:10]
    uuid_part = "".join(
        format(secrets.randbelow(16) if c == "x" else secrets.randbelow(4) + 8, "x")
        for c in "xxxxxxxxxxxxxxxx"
    )
    request_id = f"{uuid_part}{format(ms, 'x')}"
    return client_id, request_id


def _deep_merge(target: dict[str, Any], source: dict[str, Any], max_keys: int = 1000) -> None:
    """Recursively merge dictionary source into target."""
    for key, value in source.items():
        if isinstance(value, dict) and key in target and isinstance(target[key], dict):
            if len(target[key]) > max_keys:
                target[key].clear()
            _deep_merge(target[key], value, max_keys)
        else:
            if len(target) > max_keys and key not in target:
                continue
            target[key] = value


def _fill_missing(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Copy keys from ``source`` that ``target`` lacks, recursively. Never overwrites."""
    for key, value in source.items():
        if key not in target:
            target[key] = value
        elif isinstance(target[key], dict) and isinstance(value, dict):
            _fill_missing(target[key], value)


def _has_print_state(payload: dict[str, Any]) -> bool:
    """True once the merged status says what the printer is doing.

    Pushes are deltas, so after a (re)connect the merged state can hold temperatures
    but no ``print_status.state``. Parsing that as "idle" mid-print would end the job
    and disarm detection, so callers must wait for a state (from a push that carries
    one, or the GET_STATUS snapshot) instead.
    """
    data = payload.get("data", payload)
    if isinstance(data, dict) and data.get("Attributes"):
        return True  # legacy format: CurrentStatus is always present
    result = payload.get("result", {})
    print_status = result.get("print_status")
    if not isinstance(print_status, dict) or "state" not in print_status:
        return False
    # An empty state is read through machine_status.status (see _parse_status).
    return print_status["state"] != "" or isinstance(result.get("machine_status"), dict)


def _parse_status(
    payload: dict[str, Any], layers_cache: dict[str, int] | None = None
) -> PrinterStatus:
    """Extract PrinterStatus from a method-6000 payload.

    Supports both legacy Attributes and Carbon 2 formats.
    """
    try:
        # 1. Legacy / Mock Attributes format
        data: dict[str, Any] = payload.get("data", payload)
        attrs: dict[str, Any] = data.get("Attributes", {})
        if attrs:
            missing_keys = []
            for k in ["CurrentStatus", "PrintTime", "CurrentLayer", "TotalLayer"]:
                if k not in attrs:
                    missing_keys.append(k)
            if missing_keys:
                logger.warning(
                    "Missing key fields in legacy MQTT payload attributes: %s", missing_keys
                )

            printing = int(attrs.get("CurrentStatus", 0)) == 1
            elapsed = float(attrs.get("PrintTime", 0))
            current_layer = int(attrs.get("CurrentLayer", 0))
            total_layers = int(attrs.get("TotalLayer", 0))
            filename = attrs.get("Filename") or None
            thumbnail_base64 = attrs.get("Thumbnail") or payload.get("thumbnail") or None
            # Derive print_state from the boolean: the Attributes format only documents
            # CurrentStatus==1 as "printing" (docs/verified-assumptions.md); no paused or
            # completed codes have been verified, so we use the boolean as the sole mapping.
            print_state = "printing" if printing else "idle"
            return PrinterStatus(
                printing=printing,
                elapsed_seconds=elapsed,
                current_layer=current_layer,
                total_layers=total_layers,
                filename=filename,
                thumbnail_base64=thumbnail_base64,
                print_state=print_state,
                raw=payload,
            )

        # 2. Modern Elegoo Carbon 2 format
        if "result" not in payload:
            logger.warning("MQTT payload missing 'result' block for modern format")

        result: dict[str, Any] = payload.get("result", {})

        # Check for key blocks in result
        missing_blocks = []
        for block in [
            "print_status",
            "machine_status",
            "extruder",
            "heater_bed",
            "external_device",
        ]:
            if block not in result:
                missing_blocks.append(block)
        if missing_blocks:
            logger.warning(
                "Missing key status blocks in modern MQTT payload result: %s", missing_blocks
            )

        print_status = result.get("print_status", {})
        machine_status = result.get("machine_status", {})
        extruder = result.get("extruder", {})
        heater_bed = result.get("heater_bed", {})
        external_device = result.get("external_device", {})

        # Check key fields in blocks
        missing_fields = []
        if "state" not in print_status:
            missing_fields.append("print_status.state")
        if "print_duration" not in print_status:
            missing_fields.append("print_status.print_duration")
        if "current_layer" not in print_status:
            missing_fields.append("print_status.current_layer")
        if "remaining_time_sec" not in print_status:
            missing_fields.append("print_status.remaining_time_sec")
        if "progress" not in machine_status:
            missing_fields.append("machine_status.progress")
        if "temperature" not in extruder:
            missing_fields.append("extruder.temperature")
        if "target" not in extruder:
            missing_fields.append("extruder.target")
        if "temperature" not in heater_bed:
            missing_fields.append("heater_bed.temperature")
        if "target" not in heater_bed:
            missing_fields.append("heater_bed.target")
        if "camera" not in external_device:
            missing_fields.append("external_device.camera")

        if missing_fields:
            logger.warning("Missing key fields in modern MQTT payload: %s", missing_fields)

        # Callers only parse once a state is known (_has_print_state). A named state is
        # authoritative. An empty state is ambiguous: firmware 02.01 reports idle as ""
        # with machine_status.status 1 (and print_status.enable true, so "enable" does
        # not mean printing). Until "" is captured during a print, read it as printing
        # unless the idle status code says otherwise, so detection stays on.
        raw_state = print_status.get("state")
        if raw_state == "":
            printing = machine_status.get("status") != _MACHINE_STATUS_IDLE
            print_state = "printing" if printing else "standby"
        else:
            print_state = raw_state or "standby"
            printing = print_state in ("printing", "paused")

        elapsed_seconds = float(print_status.get("print_duration", 0.0))
        current_layer = int(print_status.get("current_layer", 0))

        # Parse total layers from file_list if present
        filename = print_status.get("filename") or None
        total_layers = 0
        if filename:
            if layers_cache is not None and filename in layers_cache:
                total_layers = layers_cache.pop(filename)
                layers_cache[filename] = total_layers  # move to end (LRU)
            else:
                for file_info in result.get("file_list", []):
                    if file_info.get("filename") == filename:
                        total_layers = int(file_info.get("layer", 0))
                        if layers_cache is not None:
                            layers_cache[filename] = total_layers
                            if len(layers_cache) > 100:
                                oldest = next(iter(layers_cache))
                                del layers_cache[oldest]
                        break

        extruder_temp = (
            float(extruder["temperature"]) if extruder.get("temperature") is not None else None
        )
        extruder_target = float(extruder["target"]) if extruder.get("target") is not None else None
        bed_temp = (
            float(heater_bed["temperature"]) if heater_bed.get("temperature") is not None else None
        )
        bed_target = float(heater_bed["target"]) if heater_bed.get("target") is not None else None
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


# CONNACK refusal codes for "bad username or password" / "not authorised". MQTT
# 3.1.1 reports them as 4/5; paho 2.x hands aiomqtt a ReasonCode that maps them to
# the MQTT 5 values 134/135, and a ReasonCode never compares equal to 4/5.
_AUTH_FAILURE_CODES = frozenset({4, 5, 134, 135})


def _is_auth_failure(exc: BaseException) -> bool:
    """Return True if ``exc`` is a permanent MQTT credential/authorisation refusal."""
    if not isinstance(exc, aiomqtt.MqttCodeError):
        return False
    rc = exc.rc
    value = getattr(rc, "value", rc)  # ReasonCode -> int; plain int passes through
    return isinstance(value, int) and value in _AUTH_FAILURE_CODES


class PrinterClient:
    """Async client for the Elegoo Centauri Carbon 2 over MQTT."""

    def __init__(self, settings: Settings) -> None:
        self._host = settings.printer_ip
        self._port = settings.printer_mqtt_port
        self._access_code = settings.printer_access_code.get_secret_value()
        self._client_id = f"sentinel-{uuid.uuid4().hex[:8]}"
        self._last_pause_at: float = 0.0
        self._serial_number: str | None = None
        self._accumulated_data: dict[str, Any] = {}
        self._listener_task: asyncio.Task[None] | None = None
        self._snapshot_task: asyncio.Task[None] | None = None
        # Each listener connection is a generation. The state is known only once that
        # generation's GET_STATUS snapshot is merged: pushes are deltas, so a push alone
        # cannot say which job is running (its filename, for one).
        self._conn_gen = 0
        self._baseline_gen = -1
        self._snapshot_times: deque[float] = deque()
        self._control_in_flight = 0
        # The last usable state of an earlier connection, and when its last push came.
        self._provisional: dict[str, Any] | None = None
        self._provisional_at = 0.0
        self._state_lock = asyncio.Lock()
        self._last_update_time: float = 0.0
        self.malformed_messages_count: int = 0
        self._stop_pending: bool = False
        self._file_layers_cache: dict[str, int] = {}
        self._status_client_id = f"{self._client_id}-status"
        self._cmd_client_id = f"{self._client_id}-cmd"
        # Monotonic per-command id used to match api_response acks to requests.
        self._request_counter: int = 0

    def _next_request_id(self) -> int:
        """Return the next command id (monotonic, for ack matching)."""
        self._request_counter += 1
        return self._request_counter

    @property
    def is_connected(self) -> bool:
        """Return True if the background listener is running and status updates are fresh."""
        if self._listener_task is None or self._listener_task.done():
            return False
        return (time.monotonic() - self._last_update_time) <= _STATUS_SILENCE_S

    def _current_view(self) -> dict[str, Any] | None:
        """The merged state that is safe to parse now, or None if it is not known.

        Known means this connection's GET_STATUS snapshot is merged. Until then, a
        recent earlier connection's state, with this connection's pushes on top, may
        answer only "printing": it keeps detection on through a reconnect mid-print
        without ever reporting idle from old data.
        """
        data = self._accumulated_data.get("data", self._accumulated_data)
        if isinstance(data, dict) and data.get("Attributes"):
            return self._accumulated_data  # legacy format: every push is complete
        if self._baseline_gen == self._conn_gen:
            return self._accumulated_data if _has_print_state(self._accumulated_data) else None
        if (
            self._provisional is not None
            and time.monotonic() - self._provisional_at <= _PROVISIONAL_MAX_S
        ):
            view = copy.deepcopy(self._provisional)
            for key in ("result", "data"):
                block = self._accumulated_data.get(key)
                if isinstance(block, dict):
                    _deep_merge(view.setdefault(key, {}), block)
            if _has_print_state(view) and _parse_status(view).printing:
                return view
        return None

    def _state_known(self) -> bool:
        return self._current_view() is not None

    def _retire_connection(self) -> None:
        """Keep the ending connection's usable state as provisional, then clear it.

        Call with _state_lock held, before _last_update_time is reset.
        """
        if self._accumulated_data:
            view = self._current_view()
            if view is not None:
                self._provisional = copy.deepcopy(view)
                self._provisional_at = self._last_update_time
        self._accumulated_data = {}

    @property
    def stop_pending(self) -> bool:
        """Return True if a stop command was sent but has not yet taken effect."""
        return self._stop_pending

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def status(self) -> PrinterStatus:
        """Return the current printer status.

        Raises PrinterTimeoutError after _TIMEOUT_S seconds.
        Retries up to _RETRY_ATTEMPTS times with exponential backoff.
        """
        s = await self._with_retry(self._fetch_status)
        if s and not s.printing:
            self._stop_pending = False
        return s

    async def is_printing(self) -> bool:
        return (await self.status()).printing

    async def print_elapsed_seconds(self) -> float:
        return (await self.status()).elapsed_seconds

    async def pause(self) -> None:
        """Send pause command.

        Raises PauseDebouncedError if a pause was already published within
        _PAUSE_DEBOUNCE_S seconds of this call.  Callers that catch
        PauseDebouncedError must query printer status directly to determine
        whether the printer is actually paused.

        _last_pause_at is only kept when a pause may have reached the printer, so
        a failed attempt does not block the next retry. A pause that was published
        but never acked (PrinterCommandUnacknowledgedError) or was cancelled
        mid-flight keeps the anchor: the printer may have paused, so the next
        pause() is debounced and callers check the live status instead of
        re-publishing immediately.
        """
        now = time.monotonic()
        if now - self._last_pause_at < _PAUSE_DEBOUNCE_S:
            logger.debug("pause() called within debounce window — raising PauseDebouncedError")
            raise PauseDebouncedError(
                "pause suppressed: already sent within the last "
                f"{_PAUSE_DEBOUNCE_S:.0f}s debounce window"
            )
        self._last_pause_at = now
        try:
            await self._control(CC2_CMD_PAUSE_PRINT)
        except (PrinterCommandUnacknowledgedError, asyncio.CancelledError):
            # Restart the window from now: the ack wait itself can take most of it.
            # A cancelled publish is also an unknown outcome (the command may have
            # reached the printer), so it keeps the debounce like an unacked one.
            self._last_pause_at = time.monotonic()
            raise
        except BaseException:
            # BaseException (not Exception) so any other failure rolls back the
            # anchor too: leaving it set would debounce the next retry as though a
            # pause were successfully published when nothing was actually sent.
            self._last_pause_at = 0.0
            raise

    def clear_pause_debounce(self) -> None:
        """Reset the debounce anchor so the next pause() call publishes immediately.

        Call this after a confirmed resume so that a re-detection within the
        30-second window is not silently dropped.
        """
        self._last_pause_at = 0.0
        logger.debug("Pause debounce anchor cleared")

    async def resume(self) -> None:
        """Send resume command and clear the pause debounce anchor."""
        await self._control(CC2_CMD_RESUME_PRINT)
        self.clear_pause_debounce()

    async def stop(self) -> None:
        """Send stop command.

        CRITICAL: This terminates the active print permanently. Only call
        when the user has given explicit approval. See project safety rules.
        """
        self._stop_pending = True
        try:
            await self._control(CC2_CMD_STOP_PRINT)
        except Exception:
            logger.exception("Stop command failed, flag remains pending")
            raise

    async def reconfigure(self, host: str) -> None:
        """Update the printer IP address and restart the connection."""
        # Stop the listener first (it can start snapshots), then any snapshot, so
        # nothing registers on the new host with the old serial.
        await self._stop_listener()
        await self._cancel_snapshot()
        self._host = host
        await self.close()

    async def close(self) -> None:
        """Clean up the persistent background listener and reset state."""
        await self._stop_listener()  # first: it can start snapshots
        await self._cancel_snapshot()
        self._serial_number = None
        self._accumulated_data = {}
        self._provisional = None
        self._last_update_time = 0.0
        # A pending stop is an intent aimed at whatever printer we were just
        # talking to. close()/reconfigure() means we're now talking to a
        # (possibly different) printer at a (possibly different) address, so
        # a stale stop intent must not carry over without fresh approval.
        self._stop_pending = False

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
            # Clear state on listener restart to prevent infinite timeout loops from stale data.
            # Seed _last_update_time to 0.0 (not time.monotonic()) so is_connected returns
            # False until a real MQTT status push arrives — a permanent auth failure must not
            # flap as "connected" for the first 15 s of each reconnect attempt.
            await self._cancel_snapshot()
            self._accumulated_data = {}
            self._provisional = None
            self._last_update_time = 0.0
            self._listener_task = asyncio.create_task(self._listen_loop())

        if not self._state_known():
            try:
                async with asyncio.timeout(_TIMEOUT_S):
                    while not self._state_known():
                        # close()/reconfigure() can null the task while we wait.
                        task = self._listener_task
                        if task is None:
                            raise PrinterTimeoutError("Status request aborted: listener closed")
                        if task.done():
                            exc = task.exception()
                            if exc:
                                raise exc
                            break
                        await asyncio.sleep(0.1)
            except TimeoutError as exc:
                raise PrinterTimeoutError(
                    "Status request timed out (print state not known yet)"
                ) from exc
            if not self._state_known():
                # The listener ended without an error before a state arrived.
                raise PrinterTimeoutError("Status request ended before the print state was known")

        now = time.monotonic()
        if now - self._last_update_time > _STATUS_SILENCE_S:
            # Await the old listener so the next one does not reuse its MQTT client id
            # while the old session is still open (the broker would kick one of them).
            await self._stop_listener()
            await self._cancel_snapshot()
            raise PrinterTimeoutError("Status request timed out")

        async with self._state_lock:
            view = self._current_view()
            if view is None:
                raise PrinterTimeoutError("Print state not known")
            return _parse_status(view, self._file_layers_cache)

    async def _stop_listener(self) -> None:
        task, self._listener_task = self._listener_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _listen_loop(self) -> None:
        """Background loop that maintains a persistent connection to MQTT."""
        delay = 0.5
        while True:
            has_received = False
            try:
                resolved_ip = await resolve_and_validate_printer_ip(self._host)
                client_id = self._status_client_id
                async with aiomqtt.Client(
                    hostname=resolved_ip,
                    port=self._port,
                    username="elegoo",
                    password=self._access_code,
                    identifier=client_id,
                    timeout=_TIMEOUT_S,
                    keepalive=60,
                ) as client:
                    await client.subscribe("elegoo/+/api_status")
                    delay = 0.5  # reset backoff on successful connect
                    async with self._state_lock:
                        # A new connection needs its own snapshot: pushes are deltas
                        # and the printer may have changed while we were away.
                        self._retire_connection()
                        self._conn_gen += 1
                    if self._serial_number is not None:
                        self._ensure_snapshot()  # no need to wait for the first push

                    stream_empty = True
                    messages_iter = aiter(client.messages)
                    while True:
                        try:
                            async with asyncio.timeout(_STATUS_SILENCE_S):
                                message = await anext(messages_iter)
                        except StopAsyncIteration:
                            break
                        except TimeoutError as exc:
                            raise PrinterTimeoutError(
                                f"MQTT read timeout (no status push for {_STATUS_SILENCE_S:.0f}s)"
                            ) from exc
                        stream_empty = False
                        try:
                            payload: dict[str, Any] = json.loads(message.payload)
                        except json.JSONDecodeError as exc:
                            logger.warning("Skipping malformed MQTT message: %s", exc)
                            self.malformed_messages_count += 1
                            continue

                        if not isinstance(payload, dict) or "method" not in payload:
                            logger.warning(
                                "MQTT message protocol mismatch: "
                                "payload lacks expected structure or method key"
                            )
                            self.malformed_messages_count += 1
                            continue

                        if payload.get("method") == METHOD_STATUS_PUSH:
                            has_received = True
                            parts = str(message.topic).split("/")
                            if len(parts) >= 2:
                                self._serial_number = parts[1]

                            content = payload.get("result") or payload.get("data") or {}
                            async with self._state_lock:
                                # _deep_merge only adds/overwrites keys a push carries; it
                                # never deletes keys a later push omits. When the print
                                # transitions from active to inactive, fields like
                                # filename/thumbnail from the finished job would otherwise
                                # survive indefinitely into the new idle/next-job state.
                                # Detect that transition via the modern-format
                                # print_status.state and reset accumulation so it restarts
                                # cleanly. Only trigger on an explicit new state (never on
                                # a push that omits print_status.state) to keep this
                                # narrowly scoped — not a clear on every push.
                                new_print_state = content.get("print_status", {}).get("state")
                                if new_print_state is not None:
                                    prev_print_state = (
                                        self._accumulated_data.get("result", {})
                                        .get("print_status", {})
                                        .get("state")
                                    )
                                    was_active = prev_print_state in ("printing", "paused")
                                    now_active = new_print_state in ("printing", "paused")
                                    if was_active and not now_active:
                                        # Drop only the finished job's fields. Other blocks
                                        # (bed, machine) arrive only when they change, so
                                        # clearing them would leave them unknown until the
                                        # next snapshot.
                                        for key in ("result", "data"):
                                            block = self._accumulated_data.get(key)
                                            if isinstance(block, dict):
                                                block.pop("print_status", None)
                                                block.pop("thumbnail", None)
                                        self._accumulated_data.pop("thumbnail", None)

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
                            if (
                                self._serial_number is not None
                                and self._baseline_gen != self._conn_gen
                                and not self._state_known()
                            ):
                                self._ensure_snapshot()

                    if stream_empty or not has_received:
                        raise PrinterProtocolError("MQTT stream ended without a status message")

                    logger.debug("Printer MQTT status stream ended cleanly. Reconnecting...")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30.0)
            except asyncio.CancelledError:
                raise
            except (
                aiomqtt.MqttError,
                OSError,
                TimeoutError,
                ConnectionError,
                PrinterProtocolError,
                PrinterTimeoutError,
                ValueError,
            ) as exc:
                if _is_auth_failure(exc):
                    logger.critical("MQTT permanent authentication failure: %s", exc)
                    raise
                logger.warning("Printer MQTT status connection failed: %s. Reconnecting...", exc)
                # An in-flight snapshot for this connection is dropped by its
                # generation check when the next connection starts.
                async with self._state_lock:
                    self._retire_connection()
                    self._last_update_time = 0.0
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    def _ensure_snapshot(self) -> None:
        """Start the GET_STATUS snapshot task unless one is already running."""
        if self._snapshot_task is None or self._snapshot_task.done():
            self._snapshot_task = asyncio.create_task(self._snapshot_loop())

    async def _cancel_snapshot(self) -> None:
        task, self._snapshot_task = self._snapshot_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _snapshot_wait(self) -> float:
        """Seconds until the rate limit allows another snapshot registration."""
        now = time.monotonic()
        while self._snapshot_times and now - self._snapshot_times[0] >= 3600.0:
            self._snapshot_times.popleft()
        wait = 0.0
        if len(self._snapshot_times) >= _SNAPSHOT_MAX_PER_HOUR:
            wait = self._snapshot_times[0] + 3600.0 - now
        if self._snapshot_times:
            wait = max(wait, self._snapshot_times[-1] + _SNAPSHOT_MIN_INTERVAL_S - now)
        return max(0.0, wait)

    async def _request_snapshot(self) -> dict[str, Any]:
        return await self._send_command(CC2_CMD_GET_STATUS)

    async def _snapshot_loop(self) -> None:
        """Merge a GET_STATUS snapshot for the current connection.

        Each attempt registers one short-lived command session, so attempts are rate
        limited across reconnects, wait out an in-flight pause/stop, and back off after
        a failure. A snapshot answered after the connection changed is dropped. It only
        fills keys the merged state lacks: every key already present came from a push on
        this connection, which is at least as new as the snapshot. It does not touch
        _last_update_time, which only status pushes keep alive.
        """
        while True:
            gen = self._conn_gen
            if self._baseline_gen == gen:
                return
            wait = self._snapshot_wait()
            if wait > 0:
                await asyncio.sleep(wait)
                continue
            if self._control_in_flight:
                await asyncio.sleep(_SNAPSHOT_CONTROL_BACKOFF_S)
                continue
            self._snapshot_times.append(time.monotonic())
            try:
                response = await self._request_snapshot()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Printer status snapshot failed: %s. Retrying in %.0fs", exc, _SNAPSHOT_RETRY_S
                )
                await asyncio.sleep(_SNAPSHOT_RETRY_S)
                continue
            snapshot = response.get("result")
            if not isinstance(snapshot, dict) or not _has_print_state({"result": snapshot}):
                logger.warning(
                    "Printer status snapshot had no usable print state. Retrying in %.0fs",
                    _SNAPSHOT_RETRY_S,
                )
                await asyncio.sleep(_SNAPSHOT_RETRY_S)
                continue
            snapshot = {k: v for k, v in snapshot.items() if k != "error_code"}
            async with self._state_lock:
                if gen != self._conn_gen:
                    continue  # the connection changed while we waited: take a new one
                for key in ("result", "data"):
                    block = self._accumulated_data.setdefault(key, {})
                    _fill_missing(block, snapshot)
                self._accumulated_data.setdefault("method", METHOD_STATUS_PUSH)
                self._baseline_gen = gen
                self._provisional = None
            logger.info("Printer status snapshot loaded")
            return

    async def _control(self, method: int) -> None:
        """Send pause/resume/stop with retries, marked in flight for the whole
        sequence (backoffs included) so status snapshots do not compete with it
        for the printer's client slots."""
        self._control_in_flight += 1
        try:
            await self._with_retry(lambda: self._send_command(method))
        finally:
            self._control_in_flight -= 1

    async def _send_command(
        self, method: int, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Register, publish a control command, and confirm the printer's ack.

        Firmware 02.x silently drops ``api_request`` commands from a client that
        has not completed the ``api_register`` handshake, so every command opens
        a short-lived MQTT session that (1) registers, (2) publishes the command
        with the correct ``{"id", "method", "params"}`` envelope, and (3) waits
        for the matching ``api_response`` ack. A missing ack or a non-zero
        ``error_code`` raises rather than returning silently — a command the
        printer ignores must surface as a real failure, not a false success.

        Fresh per-command ``client_id``/``request_id`` are used so a closed
        session never leaves a stale registration occupying a client slot.

        Raises:
            PrinterProtocolError: serial unknown / malformed response.
            PrinterRegistrationError: registration rejected or timed out.
            PrinterCommandError: command acknowledged with a non-zero error_code.
            PrinterTimeoutError: no registration in time (nothing published yet,
                so safe to retry).
            PrinterCommandUnacknowledgedError: the command was published but no
                ack arrived / the session died waiting. NOT retryable: the
                printer may have acted, so re-publishing could duplicate it.

        Returns the decoded command-response payload on success.
        """
        if self._serial_number is None:
            raise PrinterProtocolError(
                "serial unknown — no status received yet; cannot publish command"
            )
        try:
            resolved_ip = await resolve_and_validate_printer_ip(self._host)
        except HostResolutionError as exc:
            # A momentary DNS/mDNS failure is transient, so make it retryable. An
            # SSRF rejection (plain ValueError) is a config problem and stays fatal.
            raise PrinterTimeoutError(f"Cannot resolve printer host: {exc}") from exc
        client_id, request_id = _generate_cc2_ids()
        cmd_id = self._next_request_id()
        command_published = False
        try:
            async with aiomqtt.Client(
                hostname=resolved_ip,
                port=self._port,
                username="elegoo",
                password=self._access_code,
                identifier=client_id,
                keepalive=60,
                timeout=_TIMEOUT_S,
            ) as client:
                sn = self._serial_number
                if sn is None:
                    raise PrinterProtocolError(
                        "serial became unknown mid-connect (printer was "
                        "closed/reconfigured); aborting command publish"
                    )
                register_topic = f"elegoo/{sn}/api_register"
                register_resp_topic = f"elegoo/{sn}/{request_id}/register_response"
                request_topic = f"elegoo/{sn}/{client_id}/api_request"
                response_topic = f"elegoo/{sn}/{client_id}/api_response"

                # Subscribe to both reply topics BEFORE publishing anything so no
                # fast response can race ahead of the subscription.
                await client.subscribe(register_resp_topic)
                await client.subscribe(response_topic)
                messages = aiter(client.messages)

                # 1) Registration handshake (required on firmware 02.x).
                await client.publish(
                    register_topic,
                    json.dumps({"client_id": client_id, "request_id": request_id}),
                )
                await self._await_registration(messages, register_resp_topic)

                # 2) The actual command, confirmed by its ack. Flag it as published
                # BEFORE awaiting: aiomqtt's publish() hands the packet to paho and
                # then waits for on_publish, so a timeout or dropped link there
                # means "maybe sent" and must never be retried.
                command_published = True
                try:
                    await client.publish(
                        request_topic,
                        json.dumps({"id": cmd_id, "method": method, "params": params or {}}),
                    )
                except aiomqtt.MqttCodeError:
                    # aiomqtt raises this only when paho refuses the publish
                    # synchronously (no socket, queue full): nothing was queued
                    # or sent, so the retry layer may safely try again.
                    command_published = False
                    raise
                return await self._await_command_ack(messages, response_topic, cmd_id)
        except PrinterCommandError:
            raise  # explicit rejection by the printer: not a lost ack
        except (
            TimeoutError,
            PrinterTimeoutError,
            PrinterProtocolError,
            aiomqtt.MqttError,
            OSError,
        ) as exc:
            if command_published:
                # The command is already on the wire; any retry would publish it
                # again. Surface "sent, outcome unknown" instead (see class docs).
                raise PrinterCommandUnacknowledgedError(
                    f"command id={cmd_id} was published but not acknowledged: {exc}"
                ) from exc
            if isinstance(exc, TimeoutError):
                raise PrinterTimeoutError("Command timed out (registration)") from exc
            raise

    async def _read_json_on_topic(
        self, messages: Any, topic: str, timeout_s: float, what: str
    ) -> dict[str, Any]:
        """Read from the shared message iterator until a dict arrives on ``topic``.

        Messages on other topics are skipped. Raises PrinterTimeoutError on
        timeout and PrinterProtocolError if the connection closes first or the
        payload is not a JSON object.
        """
        try:
            async with asyncio.timeout(timeout_s):
                while True:
                    message = await anext(messages)
                    if str(message.topic) != topic:
                        continue
                    try:
                        payload = json.loads(message.payload)
                    except json.JSONDecodeError as exc:
                        raise PrinterProtocolError(f"malformed {what} response: {exc}") from exc
                    if not isinstance(payload, dict):
                        raise PrinterProtocolError(
                            f"unexpected {what} response type: {type(payload).__name__}"
                        )
                    return payload
        except TimeoutError as exc:
            raise PrinterTimeoutError(f"timed out waiting for {what}") from exc
        except StopAsyncIteration as exc:
            raise PrinterProtocolError(f"connection closed while waiting for {what}") from exc

    async def _await_registration(self, messages: Any, register_resp_topic: str) -> None:
        """Wait for a successful registration response or raise."""
        payload = await self._read_json_on_topic(
            messages, register_resp_topic, CC2_REGISTRATION_TIMEOUT_S, "registration"
        )
        error = payload.get("error", "")
        if error == CC2_REG_OK:
            logger.debug("CC2 registration accepted (client_id in topic %s)", register_resp_topic)
            return
        if error == CC2_REG_TOO_MANY_CLIENTS:
            raise PrinterRegistrationError(
                "registration rejected: too many clients connected to the printer"
            )
        raise PrinterRegistrationError(f"registration rejected by printer: error={error!r}")

    async def _await_command_ack(
        self, messages: Any, response_topic: str, cmd_id: int
    ) -> dict[str, Any]:
        """Wait for the ack matching ``cmd_id`` and verify error_code == 0."""
        while True:
            payload = await self._read_json_on_topic(
                messages, response_topic, _COMMAND_ACK_TIMEOUT_S, "command ack"
            )
            # Ignore acks for a different in-flight id (shouldn't happen on a
            # dedicated per-command session, but be defensive).
            if payload.get("id") != cmd_id:
                continue
            # Receiving the matching-id ack is itself the success signal (this is
            # what the reference client relies on). Only an *explicit* non-zero
            # error_code is a rejection — a missing field is not treated as one,
            # so we never turn a genuinely-accepted command into a false failure.
            result = payload.get("result")
            error_code = result.get("error_code") if isinstance(result, dict) else None
            if error_code in (None, CC2_ACK_OK):
                return payload
            raise PrinterCommandError(
                f"printer rejected command id={cmd_id}: error_code={error_code!r}"
            )

    async def _with_retry(self, fn: Callable[[], Awaitable[_T]]) -> _T:
        retryer = tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(_RETRY_ATTEMPTS),
            wait=_RETRY_WAIT,
            retry=tenacity.retry_if_exception_type(
                (
                    PrinterTimeoutError,
                    PrinterProtocolError,
                    # "too many clients" and other registration refusals: nothing was
                    # published yet, so retrying with backoff cannot duplicate a command.
                    PrinterRegistrationError,
                    aiomqtt.MqttError,
                    OSError,
                    ConnectionError,
                )
            ),
            reraise=True,
        )
        async for attempt in retryer:
            with attempt:
                return await fn()
        raise PrinterTimeoutError("Retries exhausted")  # unreachable, satisfies mypy
