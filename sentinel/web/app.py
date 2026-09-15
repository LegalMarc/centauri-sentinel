"""FastAPI application factory."""

from __future__ import annotations

import hmac
import json
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
    import asyncio

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
        _oversized = False
        _response_started = False

        async def bounded_receive() -> dict[str, Any]:
            nonlocal body_size, _oversized
            msg: dict[str, Any] = await receive()
            if msg["type"] == "http.request":
                body_size += len(msg.get("body", b""))
                if body_size > 1024 * 1024:
                    _oversized = True
                    # Signal end-of-body so the app sees a clean EOF rather
                    # than an incomplete stream; it will attempt to respond
                    # with whatever it parsed from the truncated body.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return msg

        async def guarded_send(message: dict[str, Any]) -> None:
            nonlocal _response_started
            if _oversized:
                # Swallow the downstream response entirely; we will send 413.
                if message["type"] == "http.response.start":
                    _response_started = True
                return
            await send(message)

        await self.app(scope, bounded_receive, guarded_send)

        if _oversized:
            response = Response(status_code=413, content="Payload Too Large")
            await response(scope, receive, send)


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
    async def healthz(request: Request) -> Response:
        res: dict[str, Any] = {"status": "ok"}
        bot = getattr(request.app.state, "bot", None)
        if bot is not None:
            res["telegram_bot_crash_count"] = getattr(bot, "crash_count", 0)

        watcher_task: asyncio.Task[None] | None = getattr(request.app.state, "watcher_task", None)
        if watcher_task is not None and watcher_task.done():
            res["status"] = "degraded"
            res["watcher"] = "dead"
            return Response(
                content=json.dumps(res),
                status_code=503,
                media_type="application/json",
            )

        return Response(content=json.dumps(res), media_type="application/json")

    @app.get("/__internal_snapshot/{nonce}")
    async def internal_snapshot(nonce: str, request: Request) -> Response:
        """Single-use JPEG endpoint for the Obico ML API URL-fetch flow."""
        if internal_token is not None:
            t = request.query_params.get("t") or ""
            # Use constant-time comparison to prevent timing side-channel attack
            if not t or not hmac.compare_digest(t, internal_token):
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

    # Registered LAST so it ends up OUTERMOST: Starlette's add_middleware()
    # prepends each new registration (user_middleware.insert(0, ...)), and
    # build_middleware_stack() wraps them outside-in from that list, so the
    # most-recently-registered middleware wraps everything registered before
    # it. Adding CSP after AuthMiddleware/LimitUploadSizeMiddleware means
    # every response — including ones AuthMiddleware or
    # LimitUploadSizeMiddleware short-circuit without calling further into
    # the app (the /login page, 401/403/429s, redirects, 413s) — still
    # passes back out through this middleware on the way to the client and
    # gets a Content-Security-Policy header. Registering it earlier (as an
    # inner layer, closer to the router) would let those short-circuited
    # responses skip it entirely.
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

    return app
