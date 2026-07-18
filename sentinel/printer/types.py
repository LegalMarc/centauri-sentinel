"""Data types for the printer client.

Field names are derived from live MQTT observations (method 6000 status push).
Exact keys need one-print-cycle verification — see docs/verified-assumptions.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PrinterStatus:
    """Parsed snapshot of the printer's current state."""

    # True while the printer is actively printing (CurrentStatus == 1)
    printing: bool

    # Seconds since print started; 0 when idle
    elapsed_seconds: float

    # Layer counters; both 0 when idle
    current_layer: int
    total_layers: int

    # Filename of the current / last job; None when none loaded
    filename: str | None

    # Extruder and bed temperatures
    extruder_temp: float | None = None
    extruder_target: float | None = None
    bed_temp: float | None = None
    bed_target: float | None = None

    # Print progress percentage (0-100)
    progress: float = 0.0

    # Remaining print time in seconds
    remaining_seconds: float = 0.0

    # Human-readable state (e.g. printing, paused, idle)
    print_state: str = "idle"

    # Camera connectivity as reported by the printer
    camera_connected: bool = False

    # Base64-encoded thumbnail PNG image from G-code metadata
    thumbnail_base64: str | None = None

    # Raw decoded payload for diagnostics
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    # Set to True if the status was preserved due to a network timeout
    stale: bool = False


# MQTT method codes (verified from spike, method 6000 = status push)
METHOD_STATUS_PUSH = 6000

# ---------------------------------------------------------------------------
# CC2 print-control command codes.
#
# Verified against the working reference implementation
# danielcherubini/elegoo-homeassistant (custom_components/elegoo_printer/cc2/
# const.py) and cross-checked against the community CC2_PROTOCOL.md. These are
# the codes the Centauri Carbon 2 firmware honours.
#
# NOTE: the previous values (pause=1001, resume=1002, stop=1003) were WRONG:
# 1001 is GET_ATTRIBUTES and 1002 is GET_STATUS — read-only queries — so the
# old "pause" command silently queried attributes and never paused. See
# docs/verified-assumptions.md.
# ---------------------------------------------------------------------------
CC2_CMD_PAUSE_PRINT = 1021
CC2_CMD_STOP_PRINT = 1022
CC2_CMD_RESUME_PRINT = 1023

# Command-response ack: result.error_code == 0 means the printer accepted it.
CC2_ACK_OK = 0

# Registration handshake (required on firmware 02.x before api_request commands
# are honoured — the printer silently ignores unregistered command clients).
CC2_REG_OK = "ok"
CC2_REG_TOO_MANY_CLIENTS = "too many clients"
CC2_REGISTRATION_TIMEOUT_S = 3.0
