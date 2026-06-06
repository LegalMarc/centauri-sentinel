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

    from sentinel.config import Settings
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


def format_duration(seconds: float) -> str:
    """Format duration in seconds to a human-readable string like '2h 5m 12s'."""
    if seconds <= 0:
        return "—"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    parts = []
    if h > 0:
        parts.append(f"{h}h")
    if m > 0 or h > 0:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def make_router(
    db: Database | None,
    watcher: Any,
    camera: Any,
    templates: Jinja2Templates | None,
    settings: Settings,
) -> APIRouter:
    router = APIRouter()
    stall_seconds = settings.watcher_stall_seconds

    @router.post("/logout")
    async def logout() -> Response:
        """Clear the session cookie."""
        response = Response(content=json.dumps({"status": "ok"}), media_type="application/json")
        response.set_cookie(
            key="sentinel_session",
            value="",
            path="/",
            httponly=True,
            samesite="strict",
            max_age=0,
        )
        return response

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
        detection_enabled = await db.get_setting("detection_enabled", "true") == "true"

        # Get settings from DB (with config defaults)
        printer_ip = await db.get_setting("printer_ip", settings.printer_ip) or settings.printer_ip

        ml_confirm_count_str = await db.get_setting(
            "ml_confirm_count", str(settings.ml_confirm_count)
        )
        ml_confirm_count = int(
            ml_confirm_count_str if ml_confirm_count_str is not None else settings.ml_confirm_count
        )

        ml_score_threshold_str = await db.get_setting(
            "ml_score_threshold", str(settings.ml_score_threshold)
        )
        ml_score_threshold = float(
            ml_score_threshold_str
            if ml_score_threshold_str is not None
            else settings.ml_score_threshold
        )

        ml_poll_interval_str = await db.get_setting(
            "ml_poll_interval_seconds", str(settings.ml_poll_interval_seconds)
        )
        ml_poll_interval_seconds = int(
            ml_poll_interval_str
            if ml_poll_interval_str is not None
            else settings.ml_poll_interval_seconds
        )

        detection_warmup_str = await db.get_setting(
            "detection_warmup_seconds", str(settings.detection_warmup_seconds)
        )
        detection_warmup_seconds = int(
            detection_warmup_str
            if detection_warmup_str is not None
            else settings.detection_warmup_seconds
        )

        from sentinel import __version__

        # Map snapshot_path to snapshot_id for the Jinja template
        for d in detections:
            path_str = d.get("snapshot_path")
            d["snapshot_id"] = Path(str(path_str)).stem if path_str else None

        # Expose printer state and elapsed print time
        p_status = await watcher.get_fresh_status()
        printer_state = "Offline"
        print_elapsed = "—"
        extruder_temp = None
        extruder_target = None
        bed_temp = None
        bed_target = None
        progress = 0.0
        remaining_seconds = 0.0
        print_state = "offline"
        camera_connected = False
        filename = "—"
        current_layer = 0
        total_layers = 0
        thumbnail_base64 = None
        is_printing = False
        is_paused = False

        if p_status:
            print_state = p_status.print_state or ("printing" if p_status.printing else "idle")
            if p_status.stale:
                print_state = "offline (stale data)"
                printer_state = "Offline (Unreachable)"
            else:
                printer_state = print_state.capitalize()
            is_printing = print_state == "printing"
            is_paused = print_state == "paused"
            is_active = p_status.printing or is_paused or print_state == "completed"
            print_elapsed = format_duration(p_status.elapsed_seconds) if is_active else "—"
            extruder_temp = p_status.extruder_temp
            extruder_target = p_status.extruder_target
            bed_temp = p_status.bed_temp
            bed_target = p_status.bed_target
            progress = 100.0 if print_state == "completed" else p_status.progress
            remaining_seconds = p_status.remaining_seconds
            camera_connected = p_status.camera_connected
            filename = p_status.filename or "—"
            current_layer = p_status.current_layer
            total_layers = p_status.total_layers
            thumbnail_base64 = p_status.thumbnail_base64

        notification_failures = None
        if watcher is not None and watcher.dispatcher is not None:
            failures = watcher.dispatcher.failed_channels
            if failures:
                notification_failures = [
                    {"channel": ch, "snapshot_id": snap} for ch, snap in failures.items()
                ]

        return templates.TemplateResponse(
            request,
            "status.html",
            {
                "watcher_state": watcher.state.name,
                "tick_age": f"{age:.0f}s ago" if age is not None else "never",
                "tick_age_stale": age is not None and age > stall_seconds,
                "notification_failures": notification_failures,
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
                "is_printing": is_printing,
                "is_paused": is_paused,
                "filename": filename,
                "current_layer": current_layer,
                "total_layers": total_layers,
                "thumbnail_base64": thumbnail_base64,
                "printer_ip": printer_ip,
                "ml_confirm_count": ml_confirm_count,
                "ml_score_threshold": ml_score_threshold,
                "ml_poll_interval_seconds": ml_poll_interval_seconds,
                "detection_warmup_seconds": detection_warmup_seconds,
                "version": __version__,
            },
        )

    @router.get("/api/printer")
    async def printer_api() -> Response:
        if watcher is None or db is None:
            raise HTTPException(status_code=503, detail="Service not initialised")

        heartbeat = await db.get_heartbeat()
        last_tick_utc = heartbeat.get("last_tick_utc") if heartbeat else None
        age = _age_seconds(last_tick_utc)

        data: dict[str, Any] = {
            "watcher_state": watcher.state.name,
            "tick_age": f"{age:.0f}s ago" if age is not None else "never",
            "tick_age_stale": age is not None and age > stall_seconds,
        }

        p_status = await watcher.get_fresh_status()
        if p_status:
            data.update(
                {
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
                    "print_state": (
                        "offline (stale data)" if p_status.stale else p_status.print_state
                    ),
                    "camera_connected": p_status.camera_connected,
                    "thumbnail_base64": p_status.thumbnail_base64,
                }
            )
        else:
            data.update(
                {
                    "printing": False,
                    "elapsed_seconds": 0.0,
                    "current_layer": 0,
                    "total_layers": 0,
                    "filename": None,
                    "extruder_temp": None,
                    "extruder_target": None,
                    "bed_temp": None,
                    "bed_target": None,
                    "progress": 0.0,
                    "remaining_seconds": 0.0,
                    "print_state": "offline",
                    "camera_connected": False,
                    "thumbnail_base64": None,
                }
            )

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
            raise HTTPException(status_code=500, detail="Pause failed — check server logs") from exc
        if sent:
            await db.record_pause(source="web", result="ok")
            await watcher.get_fresh_status(force=True)
            return Response(
                content='{"status": "ok", "message": "Print paused"}', media_type="application/json"
            )
        else:
            await db.record_pause(
                source="web", result="error", error_message="Printer already paused"
            )
            return Response(
                content='{"status": "error", "message": "Printer already paused"}',
                status_code=400,
                media_type="application/json",
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
            await watcher.get_fresh_status(force=True)
            return Response(
                content='{"status": "ok", "message": "Print resumed"}',
                media_type="application/json",
            )
        except Exception as exc:
            logger.exception("Resume failed via Web API")
            raise HTTPException(
                status_code=500, detail="Resume failed — check server logs"
            ) from exc

    @router.post("/api/control/stop")
    async def control_stop() -> Response:
        if watcher is None or watcher.printer is None:
            raise HTTPException(status_code=503, detail="Service not initialised")
        try:
            await watcher.printer.stop()
            await watcher.get_fresh_status(force=True)
            return Response(
                content='{"status": "ok", "message": "Print cancelled"}',
                media_type="application/json",
            )
        except Exception as exc:
            logger.exception("Stop failed via Web API")
            raise HTTPException(status_code=500, detail="Stop failed — check server logs") from exc

    @router.post("/api/control/snooze")
    async def control_snooze(request: Request) -> Response:
        if watcher is None or db is None:
            raise HTTPException(status_code=503, detail="Service not initialised")

        seconds = 600
        try:
            body = await request.json()
            if isinstance(body, dict) and "seconds" in body:
                seconds = int(body["seconds"])
                if seconds < 0:
                    raise HTTPException(
                        status_code=400, detail="Snooze duration cannot be negative"
                    )
                if seconds > 3600:
                    raise HTTPException(
                        status_code=400,
                        detail="Snooze duration cannot exceed 3600 seconds (1 hour). "
                        "Use the disable endpoint for longer suppression.",
                    )
        except HTTPException:
            raise
        except Exception:
            pass

        await watcher.snooze(float(seconds))

        return Response(
            content=json.dumps(
                {"status": "ok", "message": f"Detection snoozed for {seconds} seconds"}
            ),
            media_type="application/json",
        )

    @router.post("/api/settings")
    async def update_settings(request: Request) -> Response:
        if db is None or watcher is None:
            raise HTTPException(status_code=503, detail="Service not initialised")
        try:
            body = await request.json()
            printer_ip = body.get("printer_ip")
            ml_confirm_count = body.get("ml_confirm_count")
            ml_score_threshold = body.get("ml_score_threshold")
            ml_poll_interval_seconds = body.get("ml_poll_interval_seconds")
            detection_warmup_seconds = body.get("detection_warmup_seconds")

            if printer_ip is not None:
                try:
                    from sentinel.network import validate_printer_ip

                    printer_ip = await asyncio.to_thread(validate_printer_ip, printer_ip)
                except ValueError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid printer IP: {exc}",
                    ) from exc

                await db.set_setting("printer_ip", printer_ip)
                new_camera_url = f"http://{printer_ip}:{settings.printer_mjpeg_port}{settings.printer_mjpeg_path}"
                if hasattr(camera, "reconfigure"):
                    await camera.reconfigure(new_camera_url)
                if hasattr(watcher.printer, "reconfigure"):
                    await watcher.printer.reconfigure(printer_ip)

            if ml_confirm_count is not None:
                ml_confirm_count_val = int(ml_confirm_count)
                if ml_confirm_count_val < 1:
                    raise HTTPException(status_code=400, detail="Confirm count must be at least 1")
                await db.set_setting("ml_confirm_count", str(ml_confirm_count_val))

            if ml_score_threshold is not None:
                ml_score_threshold_val = float(ml_score_threshold)
                if not (0.0 <= ml_score_threshold_val <= 1.0):
                    raise HTTPException(
                        status_code=400, detail="Score threshold must be between 0.0 and 1.0"
                    )
                await db.set_setting("ml_score_threshold", str(ml_score_threshold_val))

            if ml_poll_interval_seconds is not None:
                ml_poll_interval_seconds_val = int(ml_poll_interval_seconds)
                if ml_poll_interval_seconds_val < 1:
                    raise HTTPException(
                        status_code=400, detail="Poll interval must be at least 1 second"
                    )
                await db.set_setting("ml_poll_interval_seconds", str(ml_poll_interval_seconds_val))

            if detection_warmup_seconds is not None:
                detection_warmup_seconds_val = int(detection_warmup_seconds)
                if detection_warmup_seconds_val < 0:
                    raise HTTPException(
                        status_code=400, detail="Warmup duration cannot be negative"
                    )
                await db.set_setting("detection_warmup_seconds", str(detection_warmup_seconds_val))

            return Response(
                content=json.dumps({"status": "ok", "message": "Settings updated successfully"}),
                media_type="application/json",
            )
        except HTTPException:
            raise
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid parameter format: {exc}") from exc
        except Exception as exc:
            logger.exception("Failed to update settings")
            raise HTTPException(
                status_code=500, detail="Failed to update settings — check server logs"
            ) from exc

    @router.delete("/api/data/clear")
    async def clear_all_data() -> Response:
        """Delete all detection events, pause history, print jobs, and snapshot files."""
        if db is None:
            raise HTTPException(status_code=503, detail="Service not initialised")
        try:
            counts = await db.clear_all_data()

            # Also clean up snapshot files from disk
            snapshots_dir = Path(db._path).parent / "snapshots"
            files_removed = 0
            if snapshots_dir.exists():
                for f in snapshots_dir.iterdir():
                    if f.suffix == ".jpg":
                        try:
                            f.unlink()
                            files_removed += 1
                        except OSError:
                            logger.warning("Failed to remove snapshot file: %s", f)

            counts["snapshot_files"] = files_removed
            return Response(
                content=json.dumps({"status": "ok", "cleared": counts}),
                media_type="application/json",
            )
        except Exception as exc:
            logger.exception("Failed to clear data")
            raise HTTPException(
                status_code=500, detail="Failed to clear data — check server logs"
            ) from exc

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
            from sentinel.camera.errors import CameraClosedError

            try:
                async for frame in camera.stream_proxy():
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            except CameraClosedError:
                logger.info("Camera stream closed cleanly")

        return StreamingResponse(
            _gen(),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    @router.get("/readyz")
    async def readyz() -> Response:
        reasons: list[str] = []

        if db is None:
            body = json.dumps(
                {
                    "status": "not ready",
                    "reasons": ["service not initialised"],
                    "subsystems": {
                        "db": "unreachable",
                        "watcher": "no heartbeat",
                        "mqtt": "disconnected",
                        "camera": "unreachable",
                    },
                }
            )
            return Response(content=body, status_code=503, media_type="application/json")

        heartbeat = await db.get_heartbeat()
        last_tick = heartbeat.get("last_tick_utc") if heartbeat else None
        age = _age_seconds(last_tick)

        db_reachable = False
        try:
            if await db.ping():
                db_reachable = True
            else:
                reasons.append("db not reachable")
        except Exception:
            reasons.append("db not reachable")

        watcher_healthy = False
        if age is None:
            reasons.append("no heartbeat recorded")
        elif age > stall_seconds:
            reasons.append(f"watcher stalled ({age:.0f}s since last tick)")
        else:
            watcher_healthy = True

        mqtt_connected = False
        if watcher is not None and watcher.printer is not None:
            mqtt_connected = bool(watcher.printer.is_connected)
        if not mqtt_connected:
            reasons.append("mqtt printer disconnected")

        camera_connected = False
        if camera is not None:
            camera_connected = bool(camera.is_connected)
        if not camera_connected:
            reasons.append("camera unreachable")

        subsystems = {
            "db": "reachable" if db_reachable else "unreachable",
            "watcher": (
                "healthy" if watcher_healthy else ("stalled" if age is not None else "no heartbeat")
            ),
            "mqtt": "connected" if mqtt_connected else "disconnected",
            "camera": "reachable" if camera_connected else "unreachable",
        }

        if reasons:
            body = json.dumps(
                {
                    "status": "not ready",
                    "reasons": reasons,
                    "subsystems": subsystems,
                }
            )
            return Response(content=body, status_code=503, media_type="application/json")

        body = json.dumps(
            {
                "status": "ready",
                "subsystems": subsystems,
            }
        )
        return Response(content=body, media_type="application/json")

    return router
