"""FastAPI application factory."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

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


class LimitUploadSizeMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope["method"] not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return

        cl = dict(scope.get("headers", [])).get(b"content-length")
        if cl and int(cl) > 1024 * 1024:
            response = Response(status_code=413, content="Payload Too Large")
            await response(scope, receive, send)
            return

        body_size = 0

        async def bounded_receive() -> dict[str, Any]:
            nonlocal body_size
            msg: dict[str, Any] = await receive()
            if msg["type"] == "http.request":
                body_size += len(msg.get("body", b""))
                if body_size > 1024 * 1024:
                    raise HTTPException(status_code=413, detail="Payload Too Large")
            return msg

        await self.app(scope, bounded_receive, send)


def create_app(
    settings: Settings,
    *,
    db: Database | None = None,
    watcher: Any = None,
    camera: Any = None,
    auth_secret: bytes | None = None,
    internal_token: str | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    from sentinel import __version__

    app = FastAPI(title="centauri-sentinel", version=__version__)

    @app.middleware("http")
    async def add_csp_header(request: Request, call_next: Any) -> Response:
        import secrets

        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = cast("Response", await call_next(request))
        response.headers["Content-Security-Policy"] = (
            f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none';"
        )
        return response

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    router = make_router(
        db,
        watcher,
        camera,
        templates,
        settings,
    )
    app.include_router(router)
    app.add_middleware(LimitUploadSizeMiddleware)

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        res: dict[str, Any] = {"status": "ok"}
        bot = getattr(request.app.state, "bot", None)
        if bot is not None:
            res["telegram_bot_crash_count"] = getattr(bot, "crash_count", 0)
        return res

    @app.get("/__internal_snapshot/{nonce}")
    async def internal_snapshot(nonce: str, request: Request) -> Response:
        """Single-use JPEG endpoint for the Obico ML API URL-fetch flow."""
        if internal_token is not None:
            t = request.query_params.get("t")
            if not t or t != internal_token:
                raise HTTPException(status_code=403, detail="Forbidden: Invalid internal token")

        jpeg = get_nonce_store().get(nonce)

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
