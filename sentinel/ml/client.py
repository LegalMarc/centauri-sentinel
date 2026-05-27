"""Obico ML API client — URL-fetch mode only (confirmed by spike).

Spike finding (docs/verified-assumptions.md): the ML API supports
GET /p/?img=<url> only. POST multipart is not available.

Flow: sentinel stores the JPEG in the in-memory NonceStore under a
single-use nonce, then calls the ML API with the nonce URL. The API
fetches the image itself. Any error returns MlResult(score=0.0).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from sentinel.ml.nonce import NonceStore, get_nonce_store
from sentinel.ml.types import MlResult

if TYPE_CHECKING:
    from sentinel.config import Settings

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0
_FAIL_OPEN = MlResult(score=0.0)


class MlClient:
    """Client for the Obico ML spaghetti-detection API."""

    def __init__(
        self,
        settings: Settings,
        nonce_store: NonceStore | None = None,
    ) -> None:
        self._api_url = settings.ml_api_url.rstrip("/")
        self._token_file = Path(settings.ml_api_token_file)
        self._bind_host = settings.bind_host
        self._bind_port = settings.bind_port
        self._store = nonce_store if nonce_store is not None else get_nonce_store()
        self._token: str | None = None
        self._token_mtime: float = 0.0
        if not self._token_file.exists():
            logger.warning(
                "ML API token file not found: %s — "
                "requests will be sent without authentication. "
                "Ensure token-init has run before the first detection.",
                self._token_file,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def detect(self, jpeg: bytes) -> MlResult:
        """Run spaghetti detection on *jpeg*. Never raises; fails open."""
        try:
            return await self._detect(jpeg)
        except Exception:
            logger.exception("ML detect failed — returning score=0.0 (fail-open)")
            return _FAIL_OPEN

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _detect(self, jpeg: bytes) -> MlResult:
        nonce = self._store.put(jpeg)
        try:
            host = self._bind_host
            if host == "0.0.0.0":
                host = "sentinel" if Path("/.dockerenv").exists() else "127.0.0.1"
            snapshot_url = f"http://{host}:{self._bind_port}/__internal_snapshot/{nonce}"
            token = await asyncio.to_thread(self._load_token)
            headers: dict[str, str] = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"

            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    f"{self._api_url}/p/",
                    params={"img": snapshot_url},
                    headers=headers,
                )
                resp.raise_for_status()
                return self._parse(resp.json())
        finally:
            self._store.remove(nonce)

    def _load_token(self) -> str | None:
        """Read token from file; reloads if mtime changed since last read."""
        if not self._token_file.exists():
            return None
        try:
            mtime = os.path.getmtime(self._token_file)
            if mtime != self._token_mtime:
                self._token = self._token_file.read_text().strip()
                self._token_mtime = mtime
        except OSError:
            return None
        return self._token

    @staticmethod
    def _parse(data: Any) -> MlResult:
        """Extract the spaghetti score from the API response."""
        try:
            # Obico response: {"results": [{"score": 0.73}]} or {"score": 0.73}
            if isinstance(data, dict):
                if "results" in data and isinstance(data["results"], list):
                    results = data["results"]
                    if results:
                        score = float(results[0].get("score", 0.0))
                        return MlResult(score=score)
                if "score" in data:
                    return MlResult(score=float(data["score"]))
            return _FAIL_OPEN
        except (TypeError, ValueError, KeyError):
            keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
            logger.warning("Cannot parse ML response (shape: %s)", keys)
            return _FAIL_OPEN
