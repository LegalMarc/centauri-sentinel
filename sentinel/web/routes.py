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
    from sentinel.printer.types import PrinterStatus

logger = logging.getLogger(__name__)


def _age_seconds(last: datetime | str | None) -> float | None:
    """Return seconds since `last` (a datetime or ISO string), or None if unknown."""
    if not last:
        return None
    if isinstance(last, str):
        try:
            last = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except ValueError:
            return None
    return (datetime.now(UTC) - last).total_seconds()


def _format_age(seconds: float | None) -> str:
    """Format an age in seconds as 'Ns ago', or 'never' if unknown."""
    return f"{seconds:.0f}s ago" if seconds is not None else "never"


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


def _derive_print_state(p_status: PrinterStatus | None) -> str:
    """Derive the raw/internal print_state string from a printer status snapshot.

    Shared by status_page() and printer_api() so the two views can never
    diverge on what the current print state is for the same underlying
    PrinterStatus — see docs/review_findings.md follow-up sweep (a previous
    fix added the `print_state or (...)` fallback to status_page() only,
    leaving printer_api() to report an empty string when the printer sends
    print_state="" while printing).
    """
    if not p_status:
        return "offline"
    if p_status.stale:
        return "offline (stale data)"
    return p_status.print_state or ("printing" if p_status.printing else "idle")


def _derive_printer_state(p_status: PrinterStatus | None, print_state: str) -> str:
    """Derive the human-readable, capitalized printer state for display.

    Must be called with the print_state returned by _derive_print_state() for
    the same p_status, so the raw and display values never diverge.
    """
    if not p_status:
        return "Offline"
    if p_status.stale:
        return "Offline (Unreachable)"
    return print_state.capitalize()


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
        (
            detections,
            pauses,
            recent_jobs,
            analytics,
            detection_enabled_str,
            printer_ip_val,
            ml_confirm_count_str,
            ml_score_threshold_str,
            ml_poll_interval_str,
            detection_warmup_str,
        ) = await asyncio.gather(
            db.get_recent_detections(limit=10),
            db.get_recent_pauses(limit=10),
            db.get_recent_jobs(limit=20),
            db.get_analytics_summary(),
            db.get_setting("detection_enabled", "true"),
            db.get_setting("printer_ip", settings.printer_ip),
            db.get_setting("ml_confirm_count", str(settings.ml_confirm_count)),
            db.get_setting("ml_score_threshold", str(settings.ml_score_threshold)),
            db.get_setting("ml_poll_interval_seconds", str(settings.ml_poll_interval_seconds)),
            db.get_setting("detection_warmup_seconds", str(settings.detection_warmup_seconds)),
        )

        detection_enabled = detection_enabled_str == "true"
        printer_ip = printer_ip_val or settings.printer_ip
        ml_confirm_count = int(
            ml_confirm_count_str if ml_confirm_count_str is not None else settings.ml_confirm_count  # type: ignore[arg-type]
        )
        ml_score_threshold = float(
            ml_score_threshold_str  # type: ignore[arg-type]
            if ml_score_threshold_str is not None
            else settings.ml_score_threshold
        )
        ml_poll_interval_seconds = int(
            ml_poll_interval_str  # type: ignore[arg-type]
            if ml_poll_interval_str is not None
            else settings.ml_poll_interval_seconds
        )
        detection_warmup_seconds = int(
            detection_warmup_str  # type: ignore[arg-type]
            if detection_warmup_str is not None
            else settings.detection_warmup_seconds
        )

        from sentinel import __version__

        # Map snapshot_path to snapshot_id for the Jinja template
        for item in detections:  # type: ignore
            if isinstance(item, dict):
                path_str = item.get("snapshot_path")
                item["snapshot_id"] = Path(str(path_str)).stem if path_str else None

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
        # Sentinel's own MJPEG probe (same signal /readyz uses), not the printer's
        # self-reported MQTT flag — the two connections are independent, so the
        # printer can under-report camera availability while Sentinel's own stream
        # proxy is perfectly reachable (and vice versa).
        camera_connected = bool(camera.is_connected) if camera is not None else False
        filename = "—"
        current_layer = 0
        total_layers = 0
        thumbnail_base64 = None
        is_printing = False
        is_paused = False

        if p_status:
            print_state = _derive_print_state(p_status)
            printer_state = _derive_printer_state(p_status, print_state)
            is_printing = print_state == "printing"
            is_paused = print_state == "paused"
            is_active = p_status.printing or is_paused or print_state == "completed"
            print_elapsed = format_duration(p_status.elapsed_seconds) if is_active else "—"
            extruder_temp = p_status.extruder_temp
            extruder_target = p_status.extruder_target
            bed_temp = p_status.bed_temp
            bed_target = p_status.bed_target
            progress = 100.0 if print_state in ("completed", "complete") else p_status.progress
            remaining_seconds = p_status.remaining_seconds
            filename = p_status.filename or "—"
            current_layer = p_status.current_layer
            total_layers = p_status.total_layers
            thumbnail_base64 = p_status.thumbnail_base64

        observation = watcher.last_ml_observation
        last_ml_score = observation.score if observation else None
        last_ml_score_age_s = _age_seconds(observation.ts if observation else None)
        last_ml_score_age = _format_age(last_ml_score_age_s)
        last_ml_score_stale = (
            last_ml_score_age_s is not None and last_ml_score_age_s > stall_seconds
        )

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
                "tick_age": _format_age(age),
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
                "last_ml_score": last_ml_score,
                "last_ml_score_age": last_ml_score_age,
                "last_ml_score_stale": last_ml_score_stale,
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

        observation = watcher.last_ml_observation
        last_ml_score_age_s = _age_seconds(observation.ts if observation else None)

        data: dict[str, Any] = {
            "watcher_state": watcher.state.name,
            "tick_age": _format_age(age),
            "tick_age_stale": age is not None and age > stall_seconds,
            "last_ml_score": observation.score if observation else None,
            "last_ml_score_age": _format_age(last_ml_score_age_s),
            "last_ml_score_stale": last_ml_score_age_s is not None
            and last_ml_score_age_s > stall_seconds,
        }

        # Sentinel's own MJPEG probe (same signal /readyz uses), not the printer's
        # self-reported MQTT flag — see status_page() above for why these differ.
        camera_connected = bool(camera.is_connected) if camera is not None else False

        p_status = await watcher.get_fresh_status()
        # Derived via the same helpers status_page() uses, so the two views can
        # never diverge on print_state/printer_state for the same status.
        print_state = _derive_print_state(p_status)
        data["print_state"] = print_state
        data["printer_state"] = _derive_printer_state(p_status, print_state)
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
                    "camera_connected": camera_connected,
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
                    "camera_connected": camera_connected,
                    "thumbnail_base64": None,
                }
            )

        return Response(content=json.dumps(data), media_type="application/json")

    @router.post("/api/control/pause")
    async def control_pause() -> Response:
        if db is None or watcher is None or watcher.printer is None:
            raise HTTPException(status_code=503, detail="Service not initialised")
        from sentinel.printer.errors import PauseDebouncedError

        try:
            await watcher.printer.pause()
        except PauseDebouncedError:
            # Debounce fired — check whether the printer is genuinely paused already.
            try:
                live = await watcher.printer.status()
                if live.print_state == "paused":
                    await db.record_pause(source="web", result="ok")
                    await watcher.get_fresh_status(force=True)
                    return Response(
                        content='{"status": "ok", "message": "Print paused"}',
                        media_type="application/json",
                    )
            except Exception:
                pass
            await db.record_pause(
                source="web",
                result="error",
                error_message="Pause suppressed by debounce; printer status unclear",
            )
            return Response(
                content='{"status": "error", "message": "Pause suppressed — debounce active; retrying next tick"}',
                status_code=429,
                media_type="application/json",
            )
        except Exception as exc:
            logger.exception("Pause failed via Web API")
            await db.record_pause(source="web", result="error", error_message=str(exc))
            raise HTTPException(status_code=500, detail="Pause failed — check server logs") from exc
        await db.record_pause(source="web", result="ok")
        await watcher.get_fresh_status(force=True)
        return Response(
            content='{"status": "ok", "message": "Print paused"}', media_type="application/json"
        )

    @router.post("/api/control/resume")
    async def control_resume() -> Response:
        if watcher is None or watcher.printer is None:
            raise HTTPException(status_code=503, detail="Service not initialised")
        try:
            await watcher.printer.resume()
            from sentinel.watcher.state import WatcherState

            # Atomic check-and-set: avoids racing a concurrent watchdog/tick
            # write that could otherwise silently clobber this resume action.
            await watcher.external_transition(
                WatcherState.ARMED, from_states=(WatcherState.PAUSED, WatcherState.STALLED)
            )
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
        # Check for an empty/absent body BEFORE attempting to parse JSON: a
        # missing body is the documented "use the 600s default" case, but
        # json.JSONDecodeError (raised by parsing b"") is itself a ValueError,
        # so it would otherwise be caught by the except (ValueError, TypeError)
        # clause below and turned into a hard 400 instead of the default.
        raw_body = await request.body()
        if raw_body:
            try:
                body = json.loads(raw_body)
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
            except (ValueError, TypeError) as exc:
                raise HTTPException(
                    status_code=400, detail=f"Invalid snooze duration: {exc}"
                ) from exc
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
                    from sentinel.network import format_host_for_url, validate_printer_ip

                    printer_ip = await asyncio.to_thread(validate_printer_ip, printer_ip)
                except ValueError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid printer IP: {exc}",
                    ) from exc

                await db.set_setting("printer_ip", printer_ip)
                # Bracket IPv6 literals exactly as MjpegGrabber does at startup;
                # a bare "fd00::10:8080" netloc is unparseable by urlparse.
                new_camera_url = (
                    f"http://{format_host_for_url(printer_ip)}:"
                    f"{settings.printer_mjpeg_port}{settings.printer_mjpeg_path}"
                )
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

            def _delete_files() -> int:
                removed = 0
                if snapshots_dir.exists():
                    for f in snapshots_dir.iterdir():
                        if f.suffix == ".jpg":
                            try:
                                f.unlink()
                                removed += 1
                            except OSError:
                                logger.warning("Failed to remove snapshot file: %s", f)
                return removed

            files_removed = await asyncio.to_thread(_delete_files)

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
            from sentinel.camera.errors import (
                CameraClosedError,
                CameraOfflineError,
                CameraReadError,
            )

            try:
                async for frame in camera.stream_proxy():
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            except CameraClosedError:
                logger.info("Camera stream closed cleanly")
            except (CameraReadError, CameraOfflineError) as exc:
                # e.g. "Max concurrent stream proxies reached", or the camera
                # going offline mid-stream. The 200/multipart headers are
                # already flushed by this point, so just end the response
                # cleanly instead of letting an unhandled exception propagate.
                logger.warning("Camera stream ended: %s", exc)

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
