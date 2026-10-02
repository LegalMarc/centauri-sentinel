"""Elegoo Centauri Carbon 2 printer client over MQTT.

Protocol notes:
- Broker: printer_ip:1883, username "elegoo", password = printer_access_code
- Status topic: elegoo/<serial>/api_status
- Request topic: elegoo/<serial>/<client_id>/api_request
- Firmware 1.x provides print state through method 6000 status pushes.
- Firmware 02.x may omit print-state fields from those pushes, so the client
  keeps a persistent registered MQTT session and periodically requests
  GET_STATUS (1002).
- GET_FILE_DETAIL (1046) provides active-file metadata such as material,
  filament usage, file size, and total layer count.

Both the earlier status-push behaviour and firmware 02.x are supported.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
import uuid
from typing import TYPE_CHECKING, Any, TypeVar

import aiomqtt
import tenacity

from sentinel.network import resolve_and_validate_printer_ip
from sentinel.printer.errors import (
    PauseDebouncedError,
    PrinterCommandError,
    PrinterProtocolError,
    PrinterRegistrationError,
    PrinterTimeoutError,
)
from sentinel.printer.types import (
    CC2_ACK_OK,
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

        print_state = print_status.get("state")
        if not print_state:
            _duration = float(print_status.get("print_duration", 0) or 0)
            _remaining = float(print_status.get("remaining_time_sec", 0) or 0)
            _layer = int(print_status.get("current_layer", 0) or 0)

            if _duration > 0 and (_remaining > 0 or _layer > 0):
                print_state = "printing"
            else:
                print_state = "idle"

        printing = print_state in ("printing", "paused") or print_status.get("enable") is True

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

        # FW2 GET_FILE_DETAIL (1046) metadata
        file_metadata = result.get("file_metadata", {})
        if not isinstance(file_metadata, dict):
            file_metadata = {}

        def _float_or_none(value):
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        def _int_or_none(value):
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        filament_used_g = _float_or_none(
            file_metadata.get("filament_used_g")
        )
        file_print_time = _float_or_none(
            file_metadata.get("print_time")
        )
        file_size_bytes = _int_or_none(
            file_metadata.get("size")
        )

        material = None
        filament_color = None
        color_map = file_metadata.get("color_map", [])

        if isinstance(color_map, list) and color_map:
            entry = color_map[0]
            if isinstance(entry, dict):
                material = entry.get("name")
                filament_color = entry.get("color")

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
            filament_used_g=filament_used_g,
            material=material,
            filament_color=filament_color,
            file_print_time=file_print_time,
            file_size_bytes=file_size_bytes,
            raw=payload,
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise PrinterProtocolError(f"Cannot parse status payload: {exc}") from exc


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
        return (time.monotonic() - self._last_update_time) <= 15.0

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

        _last_pause_at is only updated on a successful publish so a failed
        attempt does not block the next retry.
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
            await self._with_retry(lambda: self._send_command(CC2_CMD_PAUSE_PRINT))
        except BaseException:
            # BaseException (not Exception) so a mid-publish asyncio.CancelledError
            # also rolls back the anchor — it's a BaseException in Python 3.8+, and
            # skipping the rollback here would leave _last_pause_at set as though a
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
        await self._with_retry(lambda: self._send_command(CC2_CMD_RESUME_PRINT))
        self.clear_pause_debounce()

    async def stop(self) -> None:
        """Send stop command.

        CRITICAL: This terminates the active print permanently. Only call
        when the user has given explicit approval. See project safety rules.
        """
        self._stop_pending = True
        try:
            await self._with_retry(lambda: self._send_command(CC2_CMD_STOP_PRINT))
        except Exception:
            logger.exception("Stop command failed, flag remains pending")
            raise

    async def reconfigure(self, host: str) -> None:
        """Update the printer IP address and restart the connection."""
        self._host = host
        await self.close()

    async def close(self) -> None:
        """Clean up the persistent background listener and reset state."""
        if self._listener_task is not None:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
            self._listener_task = None
        self._serial_number = None
        self._accumulated_data = {}
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
            self._accumulated_data = {}
            self._last_update_time = 0.0
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
            if self._listener_task is not None:
                self._listener_task.cancel()
                self._listener_task = None
            raise PrinterTimeoutError("Status request timed out")

        async with self._state_lock:
            return _parse_status(self._accumulated_data, self._file_layers_cache)

    async def _listen_loop(self) -> None:
        """Background loop that maintains a persistent registered MQTT connection."""
        delay = 0.5

        while True:
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

                    # Listen for normal status pushes first. This also lets us
                    # discover the printer serial number without another client.
                    await client.subscribe("elegoo/+/api_status")

                    messages_iter = aiter(client.messages)

                    # Wait for first status packet and discover SN.
                    async with asyncio.timeout(15.0):
                        first_message = await anext(messages_iter)

                    try:
                        first_payload: dict[str, Any] = json.loads(first_message.payload)
                    except json.JSONDecodeError as exc:
                        raise PrinterProtocolError(
                            "Malformed initial MQTT status packet"
                        ) from exc

                    parts = str(first_message.topic).split("/")
                    if len(parts) < 2:
                        raise PrinterProtocolError(
                            "Could not discover printer serial number"
                        )

                    self._serial_number = parts[1]
                    sn = self._serial_number

                    # Register THIS SAME persistent client with FW 02.x.
                    request_id = uuid.uuid4().hex
                    register_response_topic = (
                        f"elegoo/{sn}/{request_id}/register_response"
                    )
                    response_topic = f"elegoo/{sn}/{client_id}/api_response"
                    request_topic = f"elegoo/{sn}/{client_id}/api_request"

                    await client.subscribe(register_response_topic)
                    await client.subscribe(response_topic)

                    await client.publish(
                        f"elegoo/{sn}/api_register",
                        json.dumps({
                            "client_id": client_id,
                            "request_id": request_id,
                        }),
                    )

                    registered = False

                    # Preserve the first status packet before waiting for
                    # registration response.
                    if (
                        isinstance(first_payload, dict)
                        and first_payload.get("method") == METHOD_STATUS_PUSH
                    ):
                        content = (
                            first_payload.get("result")
                            or first_payload.get("data")
                            or {}
                        )
                        async with self._state_lock:
                            self._accumulated_data.setdefault("result", {})
                            self._accumulated_data.setdefault("data", {})
                            self._accumulated_data["method"] = METHOD_STATUS_PUSH
                            _deep_merge(self._accumulated_data["result"], content)
                            _deep_merge(self._accumulated_data["data"], content)
                            self._last_update_time = time.monotonic()

                    async with asyncio.timeout(10.0):
                        while not registered:
                            message = await anext(messages_iter)

                            if str(message.topic) == register_response_topic:
                                response = json.loads(message.payload)

                                if response.get("error") == "ok":
                                    registered = True
                                    break

                                code = response.get("code")
                                raise PrinterProtocolError(
                                    f"FW2 registration rejected: "
                                    f"code={code}, response={response}"
                                )

                            # Don't lose status packets while registration
                            # handshake is in progress.
                            try:
                                payload = json.loads(message.payload)
                            except json.JSONDecodeError:
                                continue

                            if (
                                isinstance(payload, dict)
                                and payload.get("method") == METHOD_STATUS_PUSH
                            ):
                                content = (
                                    payload.get("result")
                                    or payload.get("data")
                                    or {}
                                )
                                async with self._state_lock:
                                    self._accumulated_data.setdefault("result", {})
                                    self._accumulated_data.setdefault("data", {})
                                    self._accumulated_data["method"] = METHOD_STATUS_PUSH
                                    _deep_merge(
                                        self._accumulated_data["result"], content
                                    )
                                    _deep_merge(
                                        self._accumulated_data["data"], content
                                    )
                                    self._last_update_time = time.monotonic()

                    logger.info(
                        "FW2 persistent MQTT client registered; "
                        "GET_STATUS polling enabled"
                    )

                    # Send first 1002 immediately.
                    command_id = self._next_request_id()
                    await client.publish(
                        request_topic,
                        json.dumps({
                            "id": command_id,
                            "method": 1002,
                            "params": {},
                        }),
                    )

                    last_poll = time.monotonic()
                    last_heartbeat = time.monotonic()
                    metadata_filename = None
                    metadata_pending = None
                    delay = 0.5

                    while True:
                        # Use short timeout so we can poll even when there
                        # are no delta pushes.
                        try:
                            async with asyncio.timeout(1.0):
                                message = await anext(messages_iter)
                        except StopAsyncIteration:
                            raise PrinterProtocolError(
                                "MQTT status stream ended"
                            )
                        except TimeoutError:
                            message = None

                        now = time.monotonic()

                        if now - last_poll >= 5.0:
                            command_id = self._next_request_id()
                            await client.publish(
                                request_topic,
                                json.dumps({
                                    "id": command_id,
                                    "method": 1002,
                                    "params": {},
                                }),
                            )
                            last_poll = now

                        # FW2 registration heartbeat, same persistent client.
                        if now - last_heartbeat >= 30.0:
                            await client.publish(
                                request_topic,
                                json.dumps({"type": "PING"}),
                            )
                            last_heartbeat = now

                        # Request file detail once when a new active filename
                        # appears. 1046 supplies metadata missing from 1002.
                        async with self._state_lock:
                            active_filename = (
                                self._accumulated_data
                                .get("result", {})
                                .get("print_status", {})
                                .get("filename")
                            )

                        if (
                            active_filename
                            and active_filename != metadata_filename
                            and active_filename != metadata_pending
                        ):
                            command_id = self._next_request_id()
                            await client.publish(
                                request_topic,
                                json.dumps({
                                    "id": command_id,
                                    "method": 1046,
                                    "params": {
                                        "storage_media": "local",
                                        "filename": active_filename,
                                    },
                                }),
                            )
                            metadata_pending = active_filename
                            logger.info(
                                "FW2 requesting file detail for %s",
                                active_filename,
                            )

                        if message is None:
                            continue

                        try:
                            payload: dict[str, Any] = json.loads(message.payload)
                        except json.JSONDecodeError as exc:
                            logger.warning(
                                "Skipping malformed MQTT message: %s", exc
                            )
                            self.malformed_messages_count += 1
                            continue

                        topic = str(message.topic)

                        # Full GET_STATUS response.
                        if topic == response_topic:
                            method = payload.get("method")

                            if method == 1002:
                                content = (
                                    payload.get("result")
                                    or payload.get("data")
                                    or {}
                                )

                                if isinstance(content, dict):
                                    async with self._state_lock:
                                        self._accumulated_data.setdefault(
                                            "result", {}
                                        )
                                        self._accumulated_data.setdefault(
                                            "data", {}
                                        )
                                        self._accumulated_data["method"] = (
                                            METHOD_STATUS_PUSH
                                        )

                                        _deep_merge(
                                            self._accumulated_data["result"],
                                            content,
                                        )
                                        _deep_merge(
                                            self._accumulated_data["data"],
                                            content,
                                        )

                                        self._last_update_time = (
                                            time.monotonic()
                                        )

                            elif method == 1046:
                                detail = (
                                    payload.get("result")
                                    or payload.get("data")
                                    or {}
                                )

                                if isinstance(detail, dict):
                                    detail_filename = (
                                        detail.get("filename")
                                        or detail.get("file_name")
                                        or metadata_pending
                                    )

                                    total_layers = (
                                        detail.get("TotalLayers")
                                        or detail.get("layer")
                                        or detail.get("total_layer")
                                        or 0
                                    )

                                    thumbnail = (
                                        detail.get("thumbnail")
                                        or detail.get("Thumbnail")
                                        or detail.get("thumbnail_base64")
                                    )

                                    try:
                                        total_layers = int(total_layers or 0)
                                    except (TypeError, ValueError):
                                        total_layers = 0

                                    async with self._state_lock:
                                        result = self._accumulated_data.setdefault(
                                            "result", {}
                                        )

                                        if detail_filename and total_layers > 0:
                                            file_list = result.setdefault(
                                                "file_list", []
                                            )

                                            existing = next(
                                                (
                                                    item for item in file_list
                                                    if item.get("filename")
                                                    == detail_filename
                                                ),
                                                None,
                                            )

                                            if existing is None:
                                                file_list.append({
                                                    "filename": detail_filename,
                                                    "layer": total_layers,
                                                })
                                            else:
                                                existing["layer"] = total_layers

                                        if thumbnail:
                                            result["thumbnail"] = thumbnail

                                        # Preserve FW2 file metadata in the
                                        # accumulated status for _parse_status().
                                        result["file_metadata"] = {
                                            "filename": detail_filename,
                                            "filament_used_g": detail.get(
                                                "total_filament_used"
                                            ),
                                            "print_time": detail.get(
                                                "print_time"
                                            ),
                                            "size": detail.get("size"),
                                            "color_map": detail.get(
                                                "color_map", []
                                            ),
                                        }

                                        self._last_update_time = (
                                            time.monotonic()
                                        )

                                    metadata_filename = detail_filename
                                    metadata_pending = None

                                    logger.info(
                                        "FW2 file detail received: "
                                        "filename=%s total_layers=%s "
                                        "thumbnail=%s",
                                        detail_filename,
                                        total_layers,
                                        bool(thumbnail),
                                    )
                                else:
                                    metadata_pending = None

                            continue

                        # Normal 6000 delta status push.
                        if payload.get("method") == METHOD_STATUS_PUSH:
                            content = (
                                payload.get("result")
                                or payload.get("data")
                                or {}
                            )

                            async with self._state_lock:
                                new_print_state = (
                                    content.get("print_status", {}).get("state")
                                )

                                if new_print_state is not None:
                                    prev_print_state = (
                                        self._accumulated_data
                                        .get("result", {})
                                        .get("print_status", {})
                                        .get("state")
                                    )

                                    was_active = prev_print_state in (
                                        "printing", "paused"
                                    )
                                    now_active = new_print_state in (
                                        "printing", "paused"
                                    )

                                    if was_active and not now_active:
                                        self._accumulated_data["result"] = {}
                                        self._accumulated_data["data"] = {}

                                self._accumulated_data.setdefault("result", {})
                                self._accumulated_data.setdefault("data", {})
                                self._accumulated_data["method"] = (
                                    METHOD_STATUS_PUSH
                                )

                                _deep_merge(
                                    self._accumulated_data["result"], content
                                )
                                _deep_merge(
                                    self._accumulated_data["data"], content
                                )

                                for k, v in payload.items():
                                    if k not in ("result", "data"):
                                        self._accumulated_data[k] = v

                                self._last_update_time = time.monotonic()

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
                code = getattr(exc, "rc", None)

                if (
                    isinstance(exc, aiomqtt.MqttCodeError)
                    and code in (4, 5)
                ):
                    logger.critical(
                        "MQTT permanent authentication failure: %s", exc
                    )
                    raise

                logger.warning(
                    "Printer MQTT status connection failed: %s. "
                    "Reconnecting...",
                    exc,
                )

                async with self._state_lock:
                    self._accumulated_data.clear()

                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

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
            PrinterTimeoutError: no registration or command ack in time.

        Returns the decoded command-response payload on success.
        """
        if self._serial_number is None:
            raise PrinterProtocolError(
                "serial unknown — no status received yet; cannot publish command"
            )
        resolved_ip = await resolve_and_validate_printer_ip(self._host)
        client_id, request_id = _generate_cc2_ids()
        cmd_id = self._next_request_id()
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

                # 2) The actual command, confirmed by its ack.
                await client.publish(
                    request_topic,
                    json.dumps({"id": cmd_id, "method": method, "params": params or {}}),
                )
                return await self._await_command_ack(messages, response_topic, cmd_id)
        except TimeoutError as exc:
            raise PrinterTimeoutError("Command timed out (registration or ack)") from exc

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
