"""Detection watcher loop.

State machine: IDLE → WARMUP → ARMED → PAUSED (and cross-cutting CAMERA_OFFLINE / STALLED).

The main loop fires every ML_POLL_INTERVAL_SECONDS. A separate watchdog
task monitors heartbeat freshness and raises a STALLED alert if the loop
stops responding.

SAFETY: The watcher never calls printer.pause() or printer.stop() without
transitioning to PAUSED state and going through _on_confirmed_detection().
Pause is logged and sent to notifiers; it is NOT called during IDLE/WARMUP.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from sentinel.camera.errors import CameraOfflineError
from sentinel.watcher.state import WatcherState

if TYPE_CHECKING:
    from sentinel.config import Settings
    from sentinel.db.repo import Database
    from sentinel.ml.types import MlResult
    from sentinel.printer.types import PrinterStatus

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    """Any object that can send alerts to users."""

    async def send_detection_alert(
        self,
        score: float,
        snapshot_id: str | None = None,
        jpeg: bytes | None = None,
    ) -> None: ...
    async def send_stall_alert(self) -> None: ...
    async def send_camera_offline_alert(self) -> None: ...


class Printer(Protocol):
    async def status(self) -> PrinterStatus: ...
    async def pause(self) -> bool: ...


class Camera(Protocol):
    async def grab(self) -> bytes: ...


class MLClient(Protocol):
    async def detect(self, jpeg: bytes) -> MlResult: ...


class WatcherLoop:
    """Core detection loop with injected dependencies (testable)."""

    def __init__(
        self,
        settings: Settings,
        printer: Printer,
        camera: Camera,
        ml: MLClient,
        db: Database,
        notifiers: list[Notifier],
    ) -> None:
        self._settings = settings
        self._printer = printer
        self._camera = camera
        self._ml = ml
        self._db = db
        self._notifiers = notifiers

        self._state = WatcherState.IDLE
        self._confirm_count = 0
        self._print_start: datetime | None = None
        self._running = False
        self.last_printer_status: PrinterStatus | None = None

    @property
    def state(self) -> WatcherState:
        return self._state

    @state.setter
    def state(self, value: WatcherState) -> None:
        self._state = value

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        """Start the main loop and heartbeat watchdog; runs until cancelled."""
        self._running = True
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._loop())
            tg.create_task(self._watchdog())

    async def tick(self) -> None:
        """Run one iteration of the detection loop (useful for tests)."""
        await self._tick()

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Watcher loop tick raised unexpectedly")
            await asyncio.sleep(self._settings.ml_poll_interval_seconds)

    async def _tick(self) -> None:
        ts = datetime.now(tz=UTC).isoformat()
        await self._db.update_heartbeat(ts, self.state.name)

        try:
            printer_status: PrinterStatus = await self._printer.status()
            self.last_printer_status = printer_status
        except Exception:
            logger.warning("Could not get printer status; staying in current state")
            return

        prev_state = self.state
        await self._update_state(printer_status)

        if self.state == WatcherState.ARMED:
            detection_enabled = await self._db.get_setting("detection_enabled", "true")
            if detection_enabled == "true":
                await self._check_frame(prev_state)
            else:
                # Reset the confirm counter so that re-enabling detection
                # does not carry stale consecutive hits into the new window.
                if self._confirm_count > 0:
                    logger.debug("Detection disabled — resetting confirm counter")
                    self._confirm_count = 0

    async def _update_state(self, status: PrinterStatus) -> None:
        if not status.printing:
            if self.state != WatcherState.IDLE:
                logger.info("Printer idle — transitioning IDLE")
            self.state = WatcherState.IDLE
            self._confirm_count = 0
            self._print_start = None
            return

        # Printer is printing
        if self._print_start is None:
            self._print_start = datetime.now(tz=UTC)

        elapsed = status.elapsed_seconds
        warmup = self._settings.detection_warmup_seconds

        if elapsed < warmup:
            self.state = WatcherState.WARMUP
        else:
            if self.state in (WatcherState.IDLE, WatcherState.WARMUP):
                logger.info("Printer armed for detection (elapsed=%.0fs)", elapsed)
            # Recover from CAMERA_OFFLINE once printer is still printing —
            # the next _check_frame call will attempt a fresh grab.
            if self.state == WatcherState.CAMERA_OFFLINE:
                logger.info("Camera offline — retrying grab on next tick")
                self.state = WatcherState.ARMED
            elif self.state != WatcherState.PAUSED:
                self.state = WatcherState.ARMED

    async def _check_frame(self, prev_state: WatcherState) -> None:
        try:
            jpeg = await self._camera.grab()
        except CameraOfflineError:
            self.state = WatcherState.CAMERA_OFFLINE
            logger.warning("Camera offline — suspending detection")
            if prev_state != WatcherState.CAMERA_OFFLINE:
                for n in self._notifiers:
                    try:
                        await n.send_camera_offline_alert()
                    except Exception:
                        logger.exception("Notifier camera_offline_alert failed")
            return
        except Exception:
            logger.warning("Camera grab failed; skipping this tick")
            return

        result: MlResult = await self._ml.detect(jpeg)

        if result.score >= self._settings.ml_score_threshold:
            self._confirm_count += 1
            logger.info(
                "Detection score=%.2f confirm=%d/%d",
                result.score,
                self._confirm_count,
                self._settings.ml_confirm_count,
            )
            if self._confirm_count >= self._settings.ml_confirm_count:
                await self._on_confirmed_detection(result, jpeg)
        else:
            if self._confirm_count > 0:
                logger.debug("Score below threshold — resetting confirm counter")
            self._confirm_count = 0

    async def _on_confirmed_detection(self, result: MlResult, jpeg: bytes) -> None:
        logger.warning("CONFIRMED DETECTION score=%.2f — pausing printer", result.score)
        consecutive_count = self._confirm_count
        self._confirm_count = 0
        snapshot_id: str | None = uuid.uuid4().hex

        snapshots_dir = Path(self._db._path).parent / "snapshots"
        snapshot_path = None
        try:
            await asyncio.to_thread(snapshots_dir.mkdir, parents=True, exist_ok=True)
            p = snapshots_dir / f"{snapshot_id}.jpg"
            await asyncio.to_thread(p.write_bytes, jpeg)
            snapshot_path = str(p)
        except Exception:
            logger.exception("Failed to save snapshot file to disk")
            snapshot_id = None
            snapshot_path = None

        # Shield the pause publish so that a task cancellation arriving mid-call
        # still lets the MQTT command complete before propagating CancelledError.
        # State is only set to PAUSED after a successful publish; on failure the
        # watcher stays ARMED and the next tick will retry.
        pause_ok = False
        pause_sent = False

        async def _do_pause() -> None:
            nonlocal pause_sent
            await self._printer.pause()
            pause_sent = True

        try:
            await asyncio.shield(_do_pause())
            pause_ok = True
            self.state = WatcherState.PAUSED
        except asyncio.CancelledError:
            if pause_sent:
                self.state = WatcherState.PAUSED
            else:
                logger.critical("Watcher cancelled before printer pause completed")
            raise
        except Exception:
            logger.exception("Printer pause failed — notifying anyway")

        pause_id = await self._db.record_pause(
            source="auto",
            result="ok" if pause_ok else "error",
            error_message=None if pause_ok else "Printer pause failed",
        )
        await self._db.record_detection(
            score=result.score,
            consecutive=consecutive_count,
            confirmed=1,
            snapshot_path=snapshot_path,
        )

        for n in self._notifiers:
            try:
                await n.send_detection_alert(result.score, snapshot_id, jpeg)
            except Exception:
                logger.exception("Notifier detection_alert failed")

        # Cleanup old snapshots (keep last 50)
        try:
            old_paths = await self._db.get_snapshots_for_cleanup(keep_limit=50)
            if old_paths:
                await self._db.delete_old_snapshots(old_paths)

                def _delete_files() -> None:
                    for path_str in old_paths:
                        p = Path(path_str)
                        if p.exists():
                            try:
                                p.unlink()
                            except OSError:
                                logger.exception("Failed to delete old snapshot file: %s", p)

                await asyncio.to_thread(_delete_files)
        except Exception:
            logger.exception("Failed to clean up old snapshots")

        # pause_id is available for future audit-log / resume-wiring use.
        del pause_id

    async def _watchdog(self) -> None:
        # Sleep for half the stall window so the maximum alert latency is
        # 1.5x stall_seconds rather than 2x (sleep-before-check pattern).
        half = max(15, self._settings.watcher_stall_seconds // 2)
        while self._running:
            await asyncio.sleep(half)
            await self._watchdog_tick(self._db)

    async def _watchdog_tick(self, db: Database) -> None:
        stall_s = self._settings.watcher_stall_seconds
        heartbeat = await db.get_heartbeat()
        if heartbeat is None:
            return
        last = heartbeat.get("last_tick_utc")
        if not last:
            return
        age = (datetime.now(tz=UTC) - datetime.fromisoformat(last)).total_seconds()
        if age > stall_s and self.state != WatcherState.STALLED:
            logger.error("Heartbeat stale (age=%.0fs) — watcher stalled", age)
            self.state = WatcherState.STALLED
            for n in self._notifiers:
                try:
                    await n.send_stall_alert()
                except Exception:
                    logger.exception("Notifier stall_alert failed")
