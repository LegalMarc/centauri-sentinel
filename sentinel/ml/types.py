"""ML result types."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MlResult:
    """Inference result from the Obico ML API.

    score=0.0 is returned on any error (fail-open).
    """

    score: float
    """Spaghetti/failure confidence in [0.0, 1.0]."""

