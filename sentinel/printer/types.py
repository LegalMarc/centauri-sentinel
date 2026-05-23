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

    # Raw decoded payload for diagnostics
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


# MQTT method codes (verified from spike, method 6000 = status push)
METHOD_STATUS_PUSH = 6000
