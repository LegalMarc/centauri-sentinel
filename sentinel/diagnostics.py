"""Operator diagnostics that talk to the real printer: ``python -m sentinel.diagnostics``.

``mqtt-slots`` measures how many CC2 client registrations the printer accepts
before answering ``too many clients``, whether re-registering the same
client_id/request_id pair is accepted, and how long a rejection lasts. Every
printer command opens a short registered MQTT session with fresh ids
(``PrinterClient._send_command``); if the firmware keeps each registration for
minutes, those pile up and later pause/stop commands are refused. This tool
answers that from the printer itself.

It only ever registers (and reads the status topic to learn the serial and the
print state). It never publishes pause, resume, stop or any other print-affecting
command. Because filling the slots makes the printer refuse later pause/stop
commands, it refuses to run while a print is active (exit code 2) unless
``--force`` is given.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

import aiomqtt

from sentinel.network import resolve_and_validate_printer_ip
from sentinel.printer.client import (
    _TIMEOUT_S,
    PrinterClient,
    _deep_merge,
    _generate_cc2_ids,
)
from sentinel.printer.errors import PrinterProtocolError, PrinterTimeoutError
from sentinel.printer.types import (
    CC2_REG_OK,
    CC2_REG_TOO_MANY_CLIENTS,
    CC2_REGISTRATION_TIMEOUT_S,
    METHOD_STATUS_PUSH,
)

if TYPE_CHECKING:
    from sentinel.config import Settings

logger = logging.getLogger(__name__)

WARNING = (
    "WARNING: this test registers MQTT clients with the printer and may temporarily\n"
    "occupy its client slots until they expire. Run it while the printer is idle.\n"
    "It sends registrations only: no pause, resume, stop or any other print command.\n"
    "It refuses to run while a print is active unless --force is given."
)

# Seconds between registration retries while waiting for a slot to expire.
_EXPIRY_POLL_S = 30.0
# How long to wait for the first status push when learning the serial number.
_SERIAL_TIMEOUT_S = 15.0

# RegistrationResult.outcome values.
OUTCOME_OK = "ok"
OUTCOME_TOO_MANY = "too many clients"
OUTCOME_FAIL = "fail"  # any other `error` string; the raw value is in `detail`
OUTCOME_TIMEOUT = "timeout"  # no register_response within CC2_REGISTRATION_TIMEOUT_S
OUTCOME_CONNECT_ERROR = "connect_error"  # MQTT connect / socket failure
# register_response was malformed (not JSON, not an object) or the connection closed first
OUTCOME_PROTOCOL_ERROR = "protocol_error"

# Exit code when the run is refused because a print is active.
EXIT_REFUSED = 2


class PrinterBusyError(Exception):
    """The printer is printing or paused (or its state is unknown), so the run is refused."""

    def __init__(self, print_state: str, message: str) -> None:
        super().__init__(message)
        self.print_state = print_state


@dataclass
class RegistrationResult:
    """The printer's answer to one registration on its own short MQTT session."""

    outcome: str
    client_id: str
    request_id: str
    detail: str = ""
    elapsed_s: float = 0.0
    # Seconds since the run started, so expiry can be reasoned about afterwards.
    at_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.outcome == OUTCOME_OK


def _make_client(host: str, port: int, access_code: str, identifier: str) -> aiomqtt.Client:
    """An MQTT client configured exactly like the ones PrinterClient opens."""
    return aiomqtt.Client(
        hostname=host,
        port=port,
        username="elegoo",
        password=access_code,
        identifier=identifier,
        keepalive=60,
        timeout=_TIMEOUT_S,
    )


# Firmware print states in which no job is running (docs/verified-assumptions.md
# §3: standby | printing | paused | complete | cancelled | error). Anything else,
# including an empty or unknown state, counts as busy: the guard is fail-closed.
_IDLE_PRINT_STATES = frozenset(
    {"standby", "idle", "complete", "completed", "cancelled", "canceled", "error"}
)


@dataclass(frozen=True)
class PrintState:
    """The printer's job state as reported in a status push."""

    label: str
    idle: bool


def classify_print_state(content: dict[str, Any]) -> PrintState | None:
    """Read the job state straight from merged status-push content.

    Prefers the modern ``print_status.state`` field (with ``enable`` as a
    second busy signal); falls back to the legacy ``Attributes.CurrentStatus``,
    where only 0 is known to mean idle. Returns None when no push has carried
    a state yet (partial deltas often omit it).
    """
    attrs = content.get("Attributes")
    legacy: PrintState | None = None
    if isinstance(attrs, dict) and "CurrentStatus" in attrs:
        raw_code = attrs["CurrentStatus"]
        # Only a literal 0 is known to mean idle; False, 0.7 or "0.0" are not.
        legacy_idle = not isinstance(raw_code, bool) and str(raw_code).strip() == "0"
        legacy = PrintState(f"CurrentStatus={raw_code}", legacy_idle)

    print_status = content.get("print_status")
    if isinstance(print_status, dict) and "state" in print_status:
        raw = print_status.get("state")
        label = str(raw).strip().lower() if raw is not None else ""
        idle = (
            label in _IDLE_PRINT_STATES
            and not print_status.get("enable")
            # Both formats present: either one saying busy wins.
            and (legacy is None or legacy.idle)
        )
        return PrintState(label or "(empty)", idle)
    return legacy


def is_print_active(state: PrintState) -> bool:
    """Not known to be idle: printing, paused, or a state we cannot vouch for."""
    return not state.idle


async def discover_printer(host: str, port: int, access_code: str) -> tuple[str, PrintState | None]:
    """Learn the printer serial and print state from the status topic.

    The serial is the second segment of ``elegoo/<sn>/api_status`` on any
    method-6000 push. The state comes from ``classify_print_state`` over the
    pushes merged so far, once one of them carries a print state. It is
    ``None`` when the serial is known but no push carried a state before the
    timeout.
    """
    serial: str | None = None
    accumulated: dict[str, Any] = {}
    async with _make_client(
        host, port, access_code, f"sentinel-diag-{_generate_cc2_ids()[0]}"
    ) as c:
        await c.subscribe("elegoo/+/api_status")
        messages = aiter(c.messages)
        try:
            async with asyncio.timeout(_SERIAL_TIMEOUT_S):
                while True:
                    message = await anext(messages)
                    try:
                        payload = json.loads(message.payload)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict) or payload.get("method") != METHOD_STATUS_PUSH:
                        continue
                    parts = str(message.topic).split("/")
                    if serial is None and len(parts) >= 2 and parts[1]:
                        serial = parts[1]
                    content = payload.get("result") or payload.get("data") or {}
                    if not isinstance(content, dict):
                        continue
                    _deep_merge(accumulated, content)
                    state = classify_print_state(accumulated)
                    if serial is not None and state is not None:
                        return serial, state
        except TimeoutError as exc:
            if serial is not None:
                return serial, None
            raise PrinterTimeoutError(
                f"no status push within {_SERIAL_TIMEOUT_S:.0f}s; cannot learn the serial number"
            ) from exc
        except StopAsyncIteration as exc:
            if serial is not None:
                return serial, None
            raise PrinterProtocolError("connection closed before any status push") from exc
    raise PrinterProtocolError("unreachable")  # pragma: no cover - satisfies mypy


async def discover_serial(host: str, port: int, access_code: str) -> str:
    """Learn just the printer serial (see ``discover_printer``)."""
    return (await discover_printer(host, port, access_code))[0]


async def register_once(
    host: str,
    port: int,
    access_code: str,
    serial: str,
    client_id: str,
    request_id: str,
    *,
    reader: PrinterClient,
    t0: float,
) -> RegistrationResult:
    """Register ``(client_id, request_id)`` on its own session, then disconnect.

    Mirrors the registration half of ``PrinterClient._send_command``: connect
    with ``client_id`` as the MQTT identifier, subscribe to both reply topics
    first, publish ``{"client_id", "request_id"}`` to ``api_register`` and read
    the ``register_response``. Leaving the ``async with`` is the disconnect.
    """
    started = time.monotonic()

    def result(outcome: str, detail: str = "") -> RegistrationResult:
        now = time.monotonic()
        return RegistrationResult(
            outcome=outcome,
            client_id=client_id,
            request_id=request_id,
            detail=detail,
            elapsed_s=round(now - started, 3),
            at_s=round(started - t0, 3),
        )

    register_topic = f"elegoo/{serial}/api_register"
    register_resp_topic = f"elegoo/{serial}/{request_id}/register_response"
    response_topic = f"elegoo/{serial}/{client_id}/api_response"
    try:
        async with _make_client(host, port, access_code, client_id) as client:
            await client.subscribe(register_resp_topic)
            await client.subscribe(response_topic)
            messages = aiter(client.messages)
            await client.publish(
                register_topic, json.dumps({"client_id": client_id, "request_id": request_id})
            )
            payload = await reader._read_json_on_topic(
                messages, register_resp_topic, CC2_REGISTRATION_TIMEOUT_S, "registration"
            )
    except PrinterTimeoutError as exc:
        return result(OUTCOME_TIMEOUT, str(exc))
    except PrinterProtocolError as exc:
        return result(OUTCOME_PROTOCOL_ERROR, str(exc))
    except (aiomqtt.MqttError, OSError, TimeoutError) as exc:
        return result(OUTCOME_CONNECT_ERROR, f"{type(exc).__name__}: {exc}")

    error = payload.get("error", "")
    if error == CC2_REG_OK:
        return result(OUTCOME_OK)
    if error == CC2_REG_TOO_MANY_CLIENTS:
        return result(OUTCOME_TOO_MANY)
    return result(OUTCOME_FAIL, f"error={error!r}")


async def run_mqtt_slots(
    settings: Settings,
    *,
    max_registrations: int = 12,
    expiry_wait_s: float = 0.0,
    force: bool = False,
) -> dict[str, Any]:
    """Run the client-slot experiments and return the structured report.

    Raises ``PrinterBusyError`` before registering anything if a print is active
    (or its state cannot be determined), unless ``force``. The expiry wait
    re-reads the print state before each retry and stops early if a print has
    started, since every retry holds another slot.
    """
    host = await resolve_and_validate_printer_ip(settings.printer_ip)
    port = settings.printer_mqtt_port
    access_code = settings.printer_access_code.get_secret_value()
    reader = PrinterClient(settings)  # only for its stateless registration reader
    t0 = time.monotonic()

    def reg(serial: str, ids: tuple[str, str]) -> Any:
        return register_once(host, port, access_code, serial, ids[0], ids[1], reader=reader, t0=t0)

    # The client_id keeps only one random hex digit next to a millisecond stamp, so
    # two ids made in the same millisecond can collide. A collision would turn a
    # "fresh" registration into a reuse and skew the count, so never repeat one.
    used_client_ids: set[str] = set()

    def new_ids() -> tuple[str, str]:
        while (ids := _generate_cc2_ids())[0] in used_client_ids:
            pass
        used_client_ids.add(ids[0])
        return ids

    serial, status = await discover_printer(host, port, access_code)
    if not force:
        if status is None:
            raise PrinterBusyError(
                "unknown",
                "cannot tell whether a print is active (no status push carried a print state); "
                "refusing to occupy the printer's client slots. Use --force to run anyway.",
            )
        if is_print_active(status):
            raise PrinterBusyError(
                status.label,
                f"the printer is {status.label}: filling its client slots would make it "
                "refuse Sentinel's pause/stop commands. Run this while the printer is idle, "
                "or use --force to run anyway.",
            )
    start_state = status.label if status is not None else "unknown"

    # Reuse test first: it is only informative while slots are still free, and
    # it costs at most two of them.
    reuse_ids = new_ids()
    reuse_first = await reg(serial, reuse_ids)
    reuse_second = await reg(serial, reuse_ids)
    if not reuse_first.ok:
        reuse_verdict = "inconclusive: the first registration was already refused"
    elif reuse_second.ok:
        reuse_verdict = "accepted: a repeated id pair is allowed"
    else:
        reuse_verdict = f"rejected: {reuse_second.outcome}"

    # Fresh-id test: one new id pair per registration, as every command does.
    fresh: list[RegistrationResult] = []
    rejection: RegistrationResult | None = None
    for _ in range(max_registrations):
        r = await reg(serial, new_ids())
        fresh.append(r)
        if not r.ok:
            rejection = r
            break
    accepted = sum(1 for r in fresh if r.ok)

    # Expiry: keep trying fresh ids until a slot frees up or the wait runs out.
    expiry: dict[str, Any] | None = None
    first_rejection = rejection or (None if reuse_first.ok else reuse_first)
    if expiry_wait_s > 0 and first_rejection is not None:
        first_ok = next((r for r in [reuse_first, *fresh] if r.ok), None)
        attempts = 0
        recovered: RegistrationResult | None = None
        stopped_reason: str | None = None
        deadline = time.monotonic() + expiry_wait_s
        while time.monotonic() + _EXPIRY_POLL_S <= deadline:
            await asyncio.sleep(_EXPIRY_POLL_S)
            if not force:
                # Every retry that is accepted takes another slot, so do not add to
                # them once a print has started: Sentinel may need to pause it.
                try:
                    _, latest = await discover_printer(host, port, access_code)
                except Exception as exc:
                    stopped_reason = (
                        f"could not re-check the print state: {type(exc).__name__}: {exc}"
                    )
                    break
                if latest is None:
                    stopped_reason = "could not re-check the print state (no state in status push)"
                    break
                if is_print_active(latest):
                    stopped_reason = f"a print started (printer is {latest.label})"
                    break
            attempts += 1
            r = await reg(serial, new_ids())
            if r.ok:
                recovered = r
                break
        expiry = {
            "recovered": recovered is not None,
            "stopped_reason": stopped_reason,
            "attempts": attempts,
            "waited_s": round(time.monotonic() - (t0 + first_rejection.at_s), 1),
            "since_rejection_s": (
                round(recovered.at_s - first_rejection.at_s, 1) if recovered else None
            ),
            # The oldest registration is the first to expire, so this approximates
            # the firmware's registration lifetime (resolution: the poll interval).
            "since_first_registration_s": (
                round(recovered.at_s - first_ok.at_s, 1) if recovered and first_ok else None
            ),
        }

    return {
        "printer": settings.printer_ip,
        "serial": serial,
        "print_state": start_state,
        "forced": force,
        "reuse": {
            "first": asdict(reuse_first),
            "second": asdict(reuse_second),
            "verdict": reuse_verdict,
        },
        "fresh": {
            "max": max_registrations,
            "accepted_before_rejection": accepted,
            "rejected": rejection is not None,
            "rejection": asdict(rejection) if rejection else None,
            "results": [asdict(r) for r in fresh],
        },
        "expiry": expiry,
    }


def format_summary(report: dict[str, Any]) -> str:
    """Render the report for a human."""
    fresh = report["fresh"]
    reuse = report["reuse"]
    lines = [
        f"Printer {report['printer']}  serial {report['serial']}  "
        f"print state {report['print_state']}" + ("  (forced)" if report["forced"] else ""),
        "",
        f"Reuse one id pair on two sessions: {reuse['verdict']}",
    ]
    if fresh["rejected"]:
        lines.append(
            f"Fresh ids: {fresh['accepted_before_rejection']} accepted, then "
            f"{fresh['rejection']['outcome']!r}"
            + (f" ({fresh['rejection']['detail']})" if fresh["rejection"]["detail"] else "")
        )
    else:
        lines.append(
            f"Fresh ids: all {fresh['accepted_before_rejection']} accepted, no rejection "
            f"(raise --max to probe further)"
        )
    expiry = report["expiry"]
    if expiry is None:
        lines.append("Expiry: not measured (needs --expiry-wait and a rejection)")
    elif expiry["recovered"]:
        lines.append(
            f"Expiry: a registration was accepted again {expiry['since_rejection_s']}s after "
            f"the rejection, about {expiry['since_first_registration_s']}s after the first "
            f"registration (+/- {int(_EXPIRY_POLL_S)}s)"
        )
    elif expiry["stopped_reason"]:
        lines.append(
            f"Expiry: stopped early after {expiry['waited_s']}s and {expiry['attempts']} retries: "
            f"{expiry['stopped_reason']}"
        )
    else:
        lines.append(
            f"Expiry: still refused after {expiry['waited_s']}s and {expiry['attempts']} retries"
        )
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sentinel.diagnostics",
        description="Diagnostics that talk to the real printer (settings come from the env).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    slots = sub.add_parser(
        "mqtt-slots",
        help="Measure the printer's MQTT client-registration slots (registration only)",
    )
    slots.add_argument(
        "--max",
        type=int,
        default=12,
        dest="max_registrations",
        help="Stop after this many fresh-id registrations if none is refused (default: 12)",
    )
    slots.add_argument(
        "--expiry-wait",
        type=float,
        default=0.0,
        help=(
            "After a rejection, retry every 30 s for up to this many seconds to time how "
            "long a slot stays taken (default: 0 = off)"
        ),
    )
    slots.add_argument(
        "--force",
        action="store_true",
        help=(
            "Run even if a print is active or paused, or its state is unknown. The printer "
            "may then refuse Sentinel's pause/stop commands until the slots expire"
        ),
    )
    return parser


async def _mqtt_slots(args: argparse.Namespace) -> int:
    from sentinel.config import get_settings

    print(WARNING, flush=True)
    print(flush=True)
    settings = get_settings()
    try:
        report = await run_mqtt_slots(
            settings,
            max_registrations=max(1, args.max_registrations),
            expiry_wait_s=max(0.0, args.expiry_wait),
            force=args.force,
        )
    except PrinterBusyError as exc:
        print(f"REFUSED: {exc}")
        print(json.dumps({"error": str(exc), "refused": True, "print_state": exc.print_state}))
        return EXIT_REFUSED
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(format_summary(report))
    print(json.dumps(report))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        return asyncio.run(_mqtt_slots(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
