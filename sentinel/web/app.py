"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from sentinel.ml.nonce import get_nonce_store
from sentinel.web.auth import AuthMiddleware
from sentinel.web.routes import make_router

if TYPE_CHECKING:
    from sentinel.config import Settings
    from sentinel.db.repo import Database

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(
    settings: Settings,
    *,
    db: Database | None = None,
    watcher: Any = None,
    camera: Any = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="centauri-sentinel", version="0.1.0")

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    router = make_router(
        db,
        watcher,
        camera,
        templates,
        stall_seconds=settings.watcher_stall_seconds,
    )
    app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/__internal_snapshot/{nonce}")
    async def internal_snapshot(nonce: str) -> Response:
        """Single-use JPEG endpoint for the Obico ML API URL-fetch flow."""
        jpeg = get_nonce_store().pop(nonce)
        if jpeg is None:
            raise HTTPException(
                status_code=404, detail="Snapshot not found or already consumed"
            )
        return Response(content=jpeg, media_type="image/jpeg")

    app.add_middleware(AuthMiddleware, settings=settings)

    return app
