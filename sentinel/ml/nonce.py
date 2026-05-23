"""Single-use in-memory nonce store for JPEG snapshots.

The ML API fetches snapshots by URL; the sentinel exposes them via
/__internal_snapshot/<nonce>. Each nonce can only be read once.
"""

from __future__ import annotations

import secrets
import threading


class NonceStore:
    """Thread-safe single-use JPEG store."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put(self, jpeg: bytes) -> str:
        """Store *jpeg* under a new nonce; returns the nonce."""
        nonce = secrets.token_hex(16)
        with self._lock:
            self._store[nonce] = jpeg
        return nonce

    def pop(self, nonce: str) -> bytes | None:
        """Return and remove the JPEG for *nonce*, or None if absent/expired."""
        with self._lock:
            return self._store.pop(nonce, None)

    def remove(self, nonce: str) -> None:
        """Remove *nonce* without returning the value (cleanup on error)."""
        with self._lock:
            self._store.pop(nonce, None)


# Process-global store shared between MlClient and the FastAPI endpoint
_global_store = NonceStore()


def get_nonce_store() -> NonceStore:
    return _global_store
