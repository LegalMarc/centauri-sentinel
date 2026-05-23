"""Shared notification types."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DetectionAlert:
    """Payload for a detection/failure alert."""

    score: float
    snapshot_id: str | None = None


@dataclass(frozen=True)
class StallAlert:
    """Payload for a watcher-stall alert."""


@dataclass(frozen=True)
class CameraOfflineAlert:
    """Payload for a camera-offline alert."""
