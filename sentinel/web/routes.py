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

_web_background_tasks: set[asyncio.Task[None]] = set()


async def _re_enable_after(db: Any, delay: float) -> None:
    await asyncio.sleep(delay)
    await db.set_setting("detection_enabled", "true")
    logger.info("Detection re-enabled after %.0fs snooze", delay)


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
        recent_jobs = await db.get_recent_jobs(limit=20)
        analytics = await db.get_analytics_summary()
        detection_enabled = (await db.get_setting("detection_enabled", "true") == "true")

        # Map snapshot_path to snapshot_id for the Jinja template
        for d in detections:
            path_str = d.get("snapshot_path")
            d["snapshot_id"] = Path(str(path_str)).stem if path_str else None

        # Expose printer state and elapsed print time
        p_status = watcher.last_printer_status
        printer_state = "Idle"
        print_elapsed = "—"
        extruder_temp = 0.0
        extruder_target = 0.0
        bed_temp = 0.0
        bed_target = 0.0
        progress = 0.0
        remaining_seconds = 0.0
        print_state = "idle"
        camera_connected = False
        filename = "—"
        current_layer = 0
        total_layers = 0
        thumbnail_base64 = None

        if p_status:
            print_state = p_status.print_state or (
                "printing" if p_status.printing else "idle"
            )
            printer_state = print_state.capitalize()
            is_active = p_status.printing or print_state == "paused"
            print_elapsed = (
                f"{p_status.elapsed_seconds:.0f}s" if is_active else "—"
            )
            extruder_temp = p_status.extruder_temp
            extruder_target = p_status.extruder_target
            bed_temp = p_status.bed_temp
            bed_target = p_status.bed_target
            progress = p_status.progress
            remaining_seconds = p_status.remaining_seconds
            camera_connected = p_status.camera_connected
            filename = p_status.filename or "—"
            current_layer = p_status.current_layer
            total_layers = p_status.total_layers
            thumbnail_base64 = p_status.thumbnail_base64

        return templates.TemplateResponse(
            request,
            "status.html",
            {
                "watcher_state": watcher.state.name,
                "tick_age": f"{age:.0f}s ago" if age is not None else "never",
                "tick_age_stale": age is not None and age > stall_seconds,
                "detections": detections,
                "pauses": pauses,
                "recent_jobs": recent_jobs,
                "analytics": analytics,
                "detection_enabled": detection_enabled,
                "printer_state": printer_state,
                "print_elapsed": print_elapsed,
                "extruder_temp": extruder_temp,
                "extruder_target": extruder_target,
                "bed_temp": bed_temp,
                "bed_target": bed_target,
                "progress": progress,
                "remaining_seconds": remaining_seconds,
                "print_state": print_state,
                "camera_connected": camera_connected,
                "filename": filename,
                "current_layer": current_layer,
                "total_layers": total_layers,
                "thumbnail_base64": thumbnail_base64,
            },
        )

    @router.get("/api/printer")
    async def printer_api() -> Response:
        if watcher is None:
            raise HTTPException(status_code=503, detail="Service not initialised")
        p_status = watcher.last_printer_status
        if not p_status:
            return Response(content='{"status": "unknown"}', media_type="application/json")

        data = {
            "printing": p_status.printing,
            "elapsed_seconds": p_status.elapsed_seconds,
            "current_layer": p_status.current_layer,
            "total_layers": p_status.total_layers,
            "filename": p_status.filename,
            "extruder_temp": p_status.extruder_temp,
            "extruder_target": p_status.extruder_target,
            "bed_temp": p_status.bed_temp,
            "bed_target": p_status.bed_target,
            "progress": p_status.progress,
            "remaining_seconds": p_status.remaining_seconds,
            "print_state": p_status.print_state,
            "camera_connected": p_status.camera_connected,
            "thumbnail_base64": p_status.thumbnail_base64,
        }
        return Response(content=json.dumps(data), media_type="application/json")

    @router.post("/api/control/pause")
    async def control_pause() -> Response:
        if db is None or watcher is None or watcher.printer is None:
            raise HTTPException(status_code=503, detail="Service not initialised")
        try:
            sent = await watcher.printer.pause()
        except Exception as exc:
            logger.exception("Pause failed via Web API")
            await db.record_pause(source="web", result="error", error_message=str(exc))
            raise HTTPException(status_code=500, detail=f"Pause failed: {exc}")
        if sent:
            await db.record_pause(source="web", result="ok")
            return Response(content='{"status": "ok", "message": "Print paused"}', media_type="application/json")
        else:
            await db.record_pause(source="web", result="error", error_message="Printer already paused")
            return Response(
                content='{"status": "error", "message": "Printer already paused"}',
                status_code=400,
                media_type="application/json"
            )

    @router.post("/api/control/resume")
    async def control_resume() -> Response:
        if watcher is None or watcher.printer is None:
            raise HTTPException(status_code=503, detail="Service not initialised")
        try:
            await watcher.printer.resume()
            from sentinel.watcher.state import WatcherState
            if watcher.state == WatcherState.PAUSED:
                watcher.state = WatcherState.ARMED
            return Response(content='{"status": "ok", "message": "Print resumed"}', media_type="application/json")
        except Exception as exc:
            logger.exception("Resume failed via Web API")
            raise HTTPException(status_code=500, detail=f"Resume failed: {exc}")

    @router.post("/api/control/stop")
    async def control_stop() -> Response:
        if watcher is None or watcher.printer is None:
            raise HTTPException(status_code=503, detail="Service not initialised")
        try:
            await watcher.printer.stop()
            return Response(content='{"status": "ok", "message": "Print cancelled"}', media_type="application/json")
        except Exception as exc:
            logger.exception("Stop failed via Web API")
            raise HTTPException(status_code=500, detail=f"Stop failed: {exc}")

    @router.post("/api/control/snooze")
    async def control_snooze(request: Request) -> Response:
        if db is None:
            raise HTTPException(status_code=503, detail="Service not initialised")
        
        seconds = 600
        try:
            body = await request.json()
            if isinstance(body, dict) and "seconds" in body:
                seconds = int(body["seconds"])
        except Exception:
            pass
            
        await db.set_setting("detection_enabled", "false")
        
        task = asyncio.create_task(_re_enable_after(db, float(seconds)))
        _web_background_tasks.add(task)
        task.add_done_callback(_web_background_tasks.discard)
        
        return Response(
            content=json.dumps({"status": "ok", "message": f"Detection snoozed for {seconds} seconds"}),
            media_type="application/json"
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
