"""Camera-specific exceptions."""

from __future__ import annotations


class CameraError(Exception):
    """Base class for camera errors."""


class CameraOfflineError(CameraError):
    """Raised after consecutive grab failures exceed the threshold."""


class CameraReadError(CameraError):
    """Raised on a single grab failure (connection lost, timeout, bad data)."""
