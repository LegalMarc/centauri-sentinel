"""FastAPI application factory — stub for ticket #1.

Full implementation in ticket #10.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

from sentinel.ml.nonce import get_nonce_store

if TYPE_CHECKING:
    from sentinel.config import Settings


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="centauri-sentinel", version="0.1.0")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/__internal_snapshot/{nonce}")
    async def internal_snapshot(nonce: str) -> Response:
        """Single-use JPEG endpoint for the Obico ML API URL-fetch flow."""
        jpeg = get_nonce_store().pop(nonce)
        if jpeg is None:
            raise HTTPException(status_code=404, detail="Snapshot not found or already consumed")
        return Response(content=jpeg, media_type="image/jpeg")

    return app
