"""Web route handlers for the status UI and camera proxies."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi.templating import Jinja2Templates

    from sentinel.db.repo import Database

logger = logging.getLogger(__name__)


def _age_seconds(heartbeat: str | None) -> float | None:
    """Return seconds since the last heartbeat, or None if no heartbeat recorded."""
    if not heartbeat:
        return None
    try:
        last = datetime.fromisoformat(heartbeat.replace("Z", "+00:00"))
        return (datetime.now(UTC) - last).total_seconds()
    except ValueError:
        return None


def make_router(
    db: Database | None,
    watcher: Any,
    camera: Any,
    templates: Jinja2Templates | None,
    *,
    stall_seconds: int = 60,
) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    async def status_page(request: Request) -> Response:
        if db is None or templates is None or watcher is None:
            raise HTTPException(status_code=503, detail="Service not initialised")
        heartbeat = await db.get_heartbeat()
        age = _age_seconds(heartbeat)
        detections = await db.get_recent_detections(limit=10)
        pauses = await db.get_recent_pauses(limit=10)
        return templates.TemplateResponse(
            request,
            "status.html",
            {
                "watcher_state": watcher.state.name,
                "tick_age": f"{age:.0f}s ago" if age is not None else "never",
                "tick_age_stale": age is not None and age > stall_seconds,
                "detections": detections,
                "pauses": pauses,
            },
        )

    @router.get("/snapshot")
    async def snapshot() -> Response:
        if camera is None:
            raise HTTPException(status_code=503, detail="Camera not available")
        try:
            jpeg = await camera.grab()
            return Response(content=jpeg, media_type="image/jpeg")
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Camera unavailable") from exc

    @router.get("/stream")
    async def stream() -> StreamingResponse:
        if camera is None:
            raise HTTPException(status_code=503, detail="Camera not available")

        async def _gen() -> AsyncIterator[bytes]:
            async for frame in camera.stream_proxy():
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"

        return StreamingResponse(
            _gen(),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    @router.get("/readyz")
    async def readyz() -> Response:
        reasons: list[str] = []

        if db is None:
            body = json.dumps({"status": "not ready", "reasons": ["service not initialised"]})
            return Response(content=body, status_code=503, media_type="application/json")

        heartbeat = await db.get_heartbeat()
        age = _age_seconds(heartbeat)
        if age is None:
            reasons.append("no heartbeat recorded")
        elif age > stall_seconds:
            reasons.append(f"watcher stalled ({age:.0f}s since last tick)")

        try:
            async with db._db.execute("SELECT 1") as cur:
                await cur.fetchone()
        except Exception:
            reasons.append("db not reachable")

        if reasons:
            body = json.dumps({"status": "not ready", "reasons": reasons})
            return Response(content=body, status_code=503, media_type="application/json")
        return Response(content='{"status":"ready"}', media_type="application/json")

    return router
