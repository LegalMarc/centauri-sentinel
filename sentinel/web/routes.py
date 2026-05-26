"""Web route handlers for the status UI and camera proxies."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

_SNAPSHOT_ID_RE = re.compile(r"^[a-f0-9]{32}$")

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
        last_tick_utc = heartbeat.get("last_tick_utc") if heartbeat else None
        age = _age_seconds(last_tick_utc)
        detections = await db.get_recent_detections(limit=10)
        pauses = await db.get_recent_pauses(limit=10)

        # Map snapshot_path to snapshot_id for the Jinja template
        for d in detections:
            path_str = d.get("snapshot_path")
            d["snapshot_id"] = Path(str(path_str)).stem if path_str else None

        # Expose printer state and elapsed print time
        p_status = watcher.last_printer_status
        printer_state = "Idle"
        print_elapsed = "—"
        if p_status:
            printer_state = "Printing" if p_status.printing else "Idle"
            print_elapsed = f"{p_status.elapsed_seconds:.0f}s" if p_status.printing else "—"

        return templates.TemplateResponse(
            request,
            "status.html",
            {
                "watcher_state": watcher.state.name,
                "tick_age": f"{age:.0f}s ago" if age is not None else "never",
                "tick_age_stale": age is not None and age > stall_seconds,
                "detections": detections,
                "pauses": pauses,
                "printer_state": printer_state,
                "print_elapsed": print_elapsed,
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

    @router.get("/snapshot/{snapshot_id}")
    async def get_saved_snapshot(snapshot_id: str) -> Response:
        if db is None:
            raise HTTPException(status_code=503, detail="Service not initialised")
        # Strict validation: snapshot IDs are always uuid4().hex (32 lowercase hex chars).
        # Reject anything else to prevent path traversal attacks.
        if not _SNAPSHOT_ID_RE.fullmatch(snapshot_id):
            raise HTTPException(status_code=404, detail="Snapshot not found")
        snapshots_dir = Path(db._path).parent / "snapshots"
        p = (snapshots_dir / f"{snapshot_id}.jpg").resolve()
        # Second guard: resolved path must stay inside snapshots_dir
        if not str(p).startswith(str(snapshots_dir.resolve())):
            raise HTTPException(status_code=404, detail="Snapshot not found")
        if not p.exists():
            raise HTTPException(status_code=404, detail="Snapshot not found")
        try:
            jpeg = await asyncio.to_thread(p.read_bytes)
            return Response(content=jpeg, media_type="image/jpeg")
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Error reading snapshot") from exc

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
        last_tick = heartbeat.get("last_tick_utc") if heartbeat else None
        age = _age_seconds(last_tick)

        if age is None:
            reasons.append("no heartbeat recorded")
        elif age > stall_seconds:
            reasons.append(f"watcher stalled ({age:.0f}s since last tick)")

        if not await db.ping():
            reasons.append("db not reachable")

        if reasons:
            body = json.dumps({"status": "not ready", "reasons": reasons})
            return Response(content=body, status_code=503, media_type="application/json")
        return Response(content='{"status":"ready"}', media_type="application/json")

    return router
