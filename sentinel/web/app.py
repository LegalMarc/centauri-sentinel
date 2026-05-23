"""FastAPI application factory — stub for ticket #1.

Full implementation in ticket #10.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI

if TYPE_CHECKING:
    from sentinel.config import Settings


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="centauri-sentinel", version="0.1.0")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
