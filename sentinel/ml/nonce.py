"""Single-use in-memory nonce store for JPEG snapshots.

The ML API fetches snapshots by URL; the sentinel exposes them via
/__internal_snapshot/<nonce>. Each nonce can only be read once.

Entries expire after _TTL_S seconds regardless of consumption so a
crashed or unreachable ML API cannot cause unbounded memory growth.
"""

from __future__ import annotations

import secrets
import threading
import time

_TTL_S = 60.0  # 2x the default poll interval; long enough for any ML round-trip
_MAX_SIZE = 50  # hard cap; oldest entry evicted when reached


class NonceStore:
    """Thread-safe single-use JPEG store with TTL expiry."""

    def __init__(self, ttl: float = _TTL_S) -> None:
        self._store: dict[str, tuple[bytes, float]] = {}  # nonce → (jpeg, expires_at)
        self._lock = threading.Lock()
        self._ttl = ttl

    def put(self, jpeg: bytes) -> str:
        """Store *jpeg* under a new nonce; returns the nonce."""
        nonce = secrets.token_hex(16)
        with self._lock:
            self._sweep_locked()
            if len(self._store) >= _MAX_SIZE:
                oldest = min(self._store, key=lambda k: self._store[k][1])
                del self._store[oldest]
            self._store[nonce] = (jpeg, time.monotonic() + self._ttl)
        return nonce

    def pop(self, nonce: str) -> bytes | None:
        """Return and remove the JPEG for *nonce*, or None if absent/expired."""
        with self._lock:
            entry = self._store.pop(nonce, None)
            if entry is None:
                return None
            jpeg, expires_at = entry
            if time.monotonic() > expires_at:
                return None
            return jpeg

    def remove(self, nonce: str) -> None:
        """Remove *nonce* without returning the value (cleanup on error)."""
        with self._lock:
            self._store.pop(nonce, None)

    def _sweep_locked(self) -> None:
        """Evict expired entries — must be called with self._lock held."""
        now = time.monotonic()
        expired = [k for k, (_, exp) in self._store.items() if now > exp]
        for k in expired:
            del self._store[k]


# Process-global store shared between MlClient and the FastAPI endpoint
_global_store = NonceStore()


def get_nonce_store() -> NonceStore:
    return _global_store
