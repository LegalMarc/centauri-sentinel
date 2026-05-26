"""FastAPI application factory."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from sentinel.ml.nonce import get_nonce_store
from sentinel.web.auth import AuthMiddleware
from sentinel.web.routes import make_router

if TYPE_CHECKING:
    from sentinel.config import Settings
    from sentinel.db.repo import Database

_TEMPLATES_DIR = Path(__file__).parent / "templates"

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings,
    *,
    db: Database | None = None,
    watcher: Any = None,
    camera: Any = None,
    auth_secret: bytes | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    from sentinel import __version__

    app = FastAPI(title="centauri-sentinel", version=__version__)

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
    async def internal_snapshot(nonce: str, request: Request) -> Response:
        """Single-use JPEG endpoint for the Obico ML API URL-fetch flow."""
        jpeg = get_nonce_store().pop(nonce)
        if jpeg is None:
            client = request.client.host if request.client else "unknown"
            logger.warning(
                "Snapshot nonce not found or expired (prefix=%s, requester=%s) — "
                "check that obico-ml can reach the sentinel bind_host:bind_port",
                nonce[:8],
                client,
            )
            raise HTTPException(status_code=404, detail="Snapshot not found or already consumed")
        return Response(content=jpeg, media_type="image/jpeg")

    app.add_middleware(AuthMiddleware, settings=settings, secret=auth_secret)

    return app
