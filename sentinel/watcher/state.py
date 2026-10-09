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

    OFFLINE = auto()
    """Printer is offline/unreachable."""


def heartbeat_interval_seconds(stall_s: float) -> float:
    """How often a healthy watcher writes its heartbeat row."""
    return max(10.0, stall_s / 2.0)


def effective_stall_seconds(stall_s: float, poll_interval_s: float) -> float:
    """Heartbeat age beyond which the watcher counts as stalled.

    A healthy loop writes the heartbeat at most every heartbeat_interval and
    ticks every poll interval, so its heartbeat can legitimately be up to
    one heartbeat interval plus about two poll intervals old. The watchdog,
    /readyz and the dashboard must all use this one threshold.
    """
    return max(stall_s, heartbeat_interval_seconds(stall_s) + 2.0 * poll_interval_s)
