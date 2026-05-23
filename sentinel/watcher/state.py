"""Watcher state machine definitions."""

from __future__ import annotations

from enum import Enum, auto


class WatcherState(Enum):
    """States of the detection watcher."""

    IDLE = auto()
    """Printer is not printing."""

    WARMUP = auto()
    """Printer is printing but within the warmup window — detection suppressed."""

    ARMED = auto()
    """Printer is printing past warmup — actively checking for failures."""

    PAUSED = auto()
    """Printer has been paused due to a confirmed detection."""

    CAMERA_OFFLINE = auto()
    """Camera has been unreachable for too long."""

    STALLED = auto()
    """Heartbeat watchdog fired — watcher loop may have hung."""
