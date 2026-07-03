"""ML result types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True)
class MlResult:
    """Inference result from the Obico ML API.

    score=0.0 is returned on any error (fail-open).
    """

    score: float
    """Spaghetti/failure confidence in [0.0, 1.0]."""

    error: bool = False
    """True if the detection failed due to a network or parsing error."""


@dataclass(frozen=True)
class LastMlObservation:
    """The most recent ML score the watcher observed, independent of threshold outcome."""

    score: float
    ts: datetime
