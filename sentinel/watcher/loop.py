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
import contextlib
import copy
import dataclasses
import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from sentinel.camera.errors import CameraOfflineError
from sentinel.ml.types import LastMlObservation
from sentinel.printer.errors import PauseDebouncedError
from sentinel.watcher.state import WatcherState

if TYPE_CHECKING:
    from sentinel.config import Settings
    from sentinel.db.repo import Database
    from sentinel.ml.types import MlResult
    from sentinel.notify.dispatcher import NotificationDispatcher
    from sentinel.printer.types import PrinterStatus

logger = logging.getLogger(__name__)

# Klipper print_status.state values the CC2 firmware publishes at the end of a
# job (docs/CC2_PROTOCOL.md in danielcherubini/elegoo-homeassistant lists
# standby | printing | paused | complete | cancelled | error). "completed" is kept
# for the legacy/mock format and older tests.
_COMPLETED_PRINT_STATES = frozenset({"complete", "completed"})
_FAILED_PRINT_STATES = frozenset({"cancelled", "canceled", "error", "stopped"})


class Camera(Protocol):
    async def grab(self) -> bytes: ...


class MLClient(Protocol):
    async def detect(self, jpeg: bytes) -> MlResult: ...


class Printer(Protocol):
    async def status(self) -> PrinterStatus: ...
    async def pause(self) -> None: ...
    async def stop(self) -> None: ...
    def clear_pause_debounce(self) -> None: ...


class WatcherLoop:
    """Core detection loop with injected dependencies (testable)."""

    def __init__(
        self,
        settings: Settings,
        printer: Printer,
        camera: Camera,
        ml: MLClient,
        db: Database,
        dispatcher: NotificationDispatcher,
    ) -> None:
        self._settings = settings
        self._printer = printer
        self._camera = camera
        self._ml = ml
        self._db = db
        self._dispatcher = dispatcher

        self._state = WatcherState.IDLE
        self._confirm_count = 0
        self._print_start: datetime | None = None
        self._running = False
        self.last_printer_status: PrinterStatus | None = None
        self.last_printed_status: PrinterStatus | None = None
        self._last_status_fetch_time: float = 0.0
        self._current_job_id: int | None = None
        self._prev_print_state: str | None = None
        self._current_filename: str | None = None
        self._paused_since: datetime | None = None
        self._paused_by_sentinel: bool = False
        self._auto_stop_notified: bool = False
        self._alerted_new_print: bool = False
        self._last_heartbeat_time = 0.0
        self._last_heartbeat_state: WatcherState | None = None
        self._snooze_task: asyncio.Task[None] | None = None
        self._tick_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._last_resume_time = 0.0
        self._ml_error_count = 0
        self.last_ml_observation: LastMlObservation | None = None
        # Alert-once guards for the two "pause keeps failing" retry loops. The
        # pause itself is retried on every tick (safety first); the operator
        # alert, snapshot file and detection row are emitted once per episode.
        self._pause_failure_alerted = False
        self._ml_failure_alerted = False

    @property
    def state(self) -> WatcherState:
        return self._state

    @state.setter
    def state(self, value: WatcherState) -> None:
        if self._state == WatcherState.PAUSED and value in (
            WatcherState.ARMED,
            WatcherState.WARMUP,
        ):
            self._last_resume_time = time.monotonic()
            logger.info("Printer resumed — setting resume cooldown anchor")
            # Clear the printer's pause debounce so that a re-detection within
            # the 30-second window publishes a real pause rather than being
            # silently dropped (external resume path — client.resume() handles
            # the command-driven path directly).
            getattr(self._printer, "clear_pause_debounce", lambda: None)()
        self._state = value

    @property
    def dispatcher(self) -> NotificationDispatcher:
        return self._dispatcher

    @property
    def printer(self) -> Printer:
        return self._printer

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        """Start the main loop and heartbeat watchdog; runs until cancelled."""
        self._running = True
        # Check if we were snoozed and it expired
        try:
            snooze_until_str = await self._db.get_setting("snooze_until_utc", "0")
            if snooze_until_str is not None:
                snooze_until = float(snooze_until_str)
                if snooze_until > 0:
                    import time

                    now = time.time()
                    if now > snooze_until:
                        await self._db.set_setting("detection_enabled", "true")
                        await self._db.set_setting("snooze_until_utc", "0")
                    else:
                        self._snooze_task = asyncio.create_task(
                            self._re_enable_after(snooze_until - now)
                        )
        except (ValueError, TypeError):
            pass

        # Restore the last observed ML score across restarts so the dashboard
        # doesn't falsely read "never" right after a deploy/crash-recovery.
        try:
            last_score_str = await self._db.get_setting("last_ml_score")
            last_score_ts_str = await self._db.get_setting("last_ml_score_ts")
            if last_score_str is not None and last_score_ts_str is not None:
                self.last_ml_observation = LastMlObservation(
                    score=float(last_score_str),
                    ts=datetime.fromisoformat(last_score_ts_str),
                )
        except (ValueError, TypeError):
            pass

        # Run cleanup once on startup to handle orphans from previous crashes
        try:
            await self.cleanup_old_snapshots()
        except Exception:
            logger.exception("Startup snapshot cleanup failed (non-fatal)")

        # Reconcile stale print-job rows from a previous crash / restart.
        # Any row still in status='printing' was never closed; mark it
        # 'interrupted' so it does not show as a phantom in-progress job and
        # so analytics totals are not silently understated.
        try:
            ended_at = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            closed = await self._db.close_stale_jobs(ended_at)
            if closed:
                logger.warning(
                    "Startup reconciliation: closed %d stale printing job(s) as 'interrupted'",
                    closed,
                )
        except Exception:
            logger.exception("Startup job reconciliation failed (non-fatal)")

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._loop())
                tg.create_task(self._watchdog())
                tg.create_task(self._periodic_cleanup())
        finally:
            self.cancel_snooze()

    async def tick(self) -> None:
        """Run one iteration of the detection loop (useful for tests)."""
        await self._tick()

    async def external_transition(
        self, new_state: WatcherState, *, from_states: tuple[WatcherState, ...]
    ) -> bool:
        """Safely transition state from an external caller (web/bot), atomically
        checking the current state against from_states before writing, to avoid
        racing a concurrent watchdog/tick write. Returns True if applied."""
        async with self._state_lock:
            if self.state in from_states:
                self.state = new_state
                return True
            return False

    async def snooze(self, seconds: float) -> None:
        """Snooze detection for the given number of seconds."""
        self.cancel_snooze()

        import time

        snooze_until = time.time() + seconds
        # Write the expiry timestamp FIRST so that a crash between the two writes
        # leaves a recoverable state: run_forever will see snooze_until > 0 and
        # either reschedule or re-enable, rather than leaving detection disabled
        # with no snooze to undo it.
        await self._db.set_setting("snooze_until_utc", str(snooze_until))
        await self._db.set_setting("detection_enabled", "false")

        self._snooze_task = asyncio.create_task(self._re_enable_after(seconds))

    def cancel_snooze(self) -> None:
        """Cancel any pending snooze task."""
        if self._snooze_task and not self._snooze_task.done():
            self._snooze_task.cancel()
        self._snooze_task = None

    async def get_fresh_status(self, force: bool = False) -> PrinterStatus | None:
        """Fetch fresh status from the printer, updating cache and timestamp.

        If force is True, bypasses the cache.
        """
        now = time.monotonic()
        if not force and self.last_printer_status and (now - self._last_status_fetch_time < 2.0):
            return self.last_printer_status

        try:
            status = await self._printer.status()
            self.last_printer_status = status
            self._last_status_fetch_time = now
        except Exception:
            logger.warning(
                "Could not get fresh printer status; using last known status if available"
            )
            if self.last_printer_status is not None:
                self.last_printer_status = dataclasses.replace(self.last_printer_status, stale=True)
            self._last_status_fetch_time = now

        return self.last_printer_status

    async def _re_enable_after(self, delay: float) -> None:
        current_task = asyncio.current_task()
        try:
            with contextlib.suppress(ValueError):
                # If delay is negative or invalid, sleep raises ValueError,
                # we just skip the wait and re-enable immediately.
                await asyncio.sleep(delay)
            if self._snooze_task is current_task:
                await self._db.set_setting("detection_enabled", "true")
                await self._db.set_setting("snooze_until_utc", "0")
                self._dispatcher.dispatch_text("Detection re-enabled after snooze.")
                logger.info("Detection re-enabled after %.0fs snooze", delay)
        except asyncio.CancelledError:
            pass
        finally:
            if self._snooze_task is current_task:
                self._snooze_task = None

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

            try:
                poll_interval_str = await self._db.get_setting(
                    "ml_poll_interval_seconds",
                    str(self._settings.ml_poll_interval_seconds),
                )
                if poll_interval_str is None:
                    poll_interval_str = str(self._settings.ml_poll_interval_seconds)
                try:
                    poll_interval = float(poll_interval_str)
                except (ValueError, TypeError):
                    poll_interval = float(self._settings.ml_poll_interval_seconds)
            except Exception:
                logger.exception(
                    "DB error reading ml_poll_interval_seconds — using default %.1f s",
                    float(self._settings.ml_poll_interval_seconds),
                )
                poll_interval = float(self._settings.ml_poll_interval_seconds)
            await asyncio.sleep(poll_interval)

    async def _tick(self) -> None:
        if self._tick_lock.locked():
            logger.warning("Watcher loop tick overlapping skipped.")
            return
        async with self._tick_lock:
            now_ts = time.monotonic()
            try:
                stall_s = int(self._settings.watcher_stall_seconds)
            except (ValueError, TypeError):
                stall_s = 60
            hb_interval = max(10.0, stall_s / 2.0)

            if (
                self.state != self._last_heartbeat_state
                or now_ts - self._last_heartbeat_time >= hb_interval
            ):
                ts = datetime.now(tz=UTC).isoformat()
                await self._db.update_heartbeat(ts, self.state.name)
                self._last_heartbeat_time = now_ts
                self._last_heartbeat_state = self.state

            if getattr(self._printer, "stop_pending", False) is True:
                logger.warning("Retrying pending print stop command")
                try:
                    await self._printer.stop()
                except Exception:
                    logger.warning("Pending print stop command retry failed")

            printer_status = await self.get_fresh_status(force=True)
            if printer_status is None:
                return

            prev_state = self.state
            await self._update_state(printer_status)

            if self.state == WatcherState.PAUSED:
                if self._paused_since is None:
                    self._paused_since = datetime.now(tz=UTC)
                elif self._paused_by_sentinel:
                    pause_duration = (datetime.now(tz=UTC) - self._paused_since).total_seconds()
                    try:
                        auto_stop_timeout = int(self._settings.auto_stop_timeout_seconds)
                    except (ValueError, TypeError):
                        auto_stop_timeout = 0

                    if auto_stop_timeout > 0 and pause_duration > auto_stop_timeout:
                        if not self._auto_stop_notified:
                            logger.warning(
                                "Auto-stop timeout reached (%.0f s) — "
                                "dispatching notification and stopping printer",
                                pause_duration,
                            )
                            text = (
                                f"⚠️ Printer has been paused for over {auto_stop_timeout // 60} "
                                "minutes. Initiating automatic stop."
                            )
                            self._dispatcher.dispatch_text(text)
                            self._auto_stop_notified = True

                        try:
                            # Actually call stop to halt the printer
                            await self._printer.stop()
                        except Exception:
                            logger.exception(
                                "Failed to automatically stop the printer after timeout"
                            )
                        else:
                            # Only clear once stop() actually succeeded — a failed
                            # attempt must leave these set so the escalation stays
                            # eligible to retry on a later tick instead of being
                            # permanently disabled for the rest of this pause episode.
                            self._paused_since = None  # reset to prevent spamming
                            self._paused_by_sentinel = False
                            self._auto_stop_notified = False
            elif self.state not in (
                WatcherState.OFFLINE,
                WatcherState.CAMERA_OFFLINE,
                WatcherState.STALLED,
            ):
                self._paused_since = None
                self._paused_by_sentinel = False
                self._auto_stop_notified = False

            if self.state in (WatcherState.ARMED, WatcherState.CAMERA_OFFLINE):
                detection_enabled = await self._db.get_setting("detection_enabled", "true")
                if detection_enabled == "true":
                    await self._check_frame(prev_state)
                else:
                    # Reset the confirm counter so that re-enabling detection
                    # does not carry stale consecutive hits into the new window.
                    if self._confirm_count > 0:
                        logger.debug("Detection disabled — resetting confirm counter")
                        self._confirm_count = 0

            self.last_printer_status = copy.copy(printer_status)

    def _final_job_status(self, terminal_print_state: str | None) -> str:
        """Map the state observed when a job ended to 'completed' or 'failed'.

        The watcher polls every ML_POLL_INTERVAL_SECONDS while the printer
        pushes status at ~1 Hz, so the terminal state ("complete", "cancelled")
        can be missed entirely and the first post-job status already reads
        "standby"/"idle". In that case fall back to the last progress observed
        while printing: a job seen at (or past) 100 % is treated as completed.
        """
        state = (terminal_print_state or "").lower()
        if state in _COMPLETED_PRINT_STATES:
            return "completed"
        if state in _FAILED_PRINT_STATES:
            return "failed"
        last = self.last_printed_status
        if last is not None and last.filename == self._current_filename and last.progress >= 100.0:
            return "completed"
        return "failed"

    async def _update_state(self, status: PrinterStatus) -> None:
        if getattr(status, "stale", False):
            if self.state not in (WatcherState.STALLED, WatcherState.OFFLINE, WatcherState.IDLE):
                logger.warning(
                    "Printer status is stale — transitioning OFFLINE and suspending detection"
                )
                self.state = WatcherState.OFFLINE
                self._dispatcher.dispatch_text("⚠️ Printer is unreachable — detection suspended.")
            return
        if not status.printing:
            if self.state != WatcherState.IDLE:
                logger.info("Printer idle — transitioning IDLE")
            self.state = WatcherState.IDLE
            self._confirm_count = 0
            self._ml_error_count = 0

            # Record print end if a job was active
            if self._current_job_id is not None:
                ended_at = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
                elapsed_duration = 0
                if (
                    status.filename == self._current_filename
                    and getattr(status, "elapsed_seconds", 0) > 0
                ):
                    elapsed_duration = int(status.elapsed_seconds)
                elif (
                    self.last_printed_status
                    and self.last_printed_status.filename == self._current_filename
                    and getattr(self.last_printed_status, "elapsed_seconds", 0) > 0
                ):
                    elapsed_duration = int(self.last_printed_status.elapsed_seconds)
                wall_duration = 0
                if self._print_start is not None:
                    wall_duration = int((datetime.now(tz=UTC) - self._print_start).total_seconds())
                duration = max(0, elapsed_duration, wall_duration)

                final_status = self._final_job_status(status.print_state)
                await self._db.record_print_end(
                    self._current_job_id,
                    ended_at,
                    duration,
                    0.0,
                    final_status,
                )
                if final_status == "completed" and self._settings.notify_on_print_completed:
                    jpeg = await self._safe_grab_jpeg()
                    self._dispatcher.dispatch_print_completed(
                        self._current_filename, float(duration), jpeg
                    )

                self._current_job_id = None

            self._print_start = None
            self._prev_print_state = None
            self._current_filename = None
            self._paused_since = None
            self._paused_by_sentinel = False
            self._auto_stop_notified = False
            self._alerted_new_print = False
            self._pause_failure_alerted = False
            self._ml_failure_alerted = False
            return

        # Printer is printing
        if self._print_start is None:
            self._print_start = datetime.now(tz=UTC)

        if not self._alerted_new_print:
            self._alerted_new_print = True
            await self._check_and_send_state_reminders()
            if getattr(self._settings, "notify_on_print_start", False):
                jpeg = await self._safe_grab_jpeg()
                self._dispatcher.dispatch_print_started(status.filename, jpeg)

        # Handle back-to-back print job transitions (filename changed while printing)
        if (
            self._current_job_id is not None
            and self._current_filename is not None
            and status.filename != self._current_filename
        ):
            ended_at = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
            elapsed_duration = 0
            if (
                self.last_printed_status
                and self.last_printed_status.filename == self._current_filename
                and getattr(self.last_printed_status, "elapsed_seconds", 0) > 0
            ):
                elapsed_duration = int(self.last_printed_status.elapsed_seconds)
            wall_duration = 0
            if self._print_start is not None:
                wall_duration = int((datetime.now(tz=UTC) - self._print_start).total_seconds())
            duration = max(0, elapsed_duration, wall_duration)
            final_status = self._final_job_status(self._prev_print_state)
            await self._db.record_print_end(
                self._current_job_id,
                ended_at,
                duration,
                0.0,
                final_status,
            )
            if final_status == "completed" and getattr(
                self._settings, "notify_on_print_completed", True
            ):
                jpeg = await self._safe_grab_jpeg()
                self._dispatcher.dispatch_print_completed(
                    self._current_filename, float(duration), jpeg
                )
            self._current_job_id = None
            self._print_start = datetime.now(tz=UTC)
            self._alerted_new_print = False
            # Reset detection counters so job A's streak does not carry into job B
            self._confirm_count = 0
            self._ml_error_count = 0

        # Start job tracking if not already active
        if self._current_job_id is None:
            started_at = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
            self._current_filename = status.filename
            self._current_job_id = await self._db.record_print_start(
                status.filename or "unknown.gcode",
                started_at,
            )

        # Record pauses
        if (
            status.print_state == "paused"
            and self._prev_print_state != "paused"
            and self._current_job_id is not None
        ):
            await self._db.increment_job_pauses(self._current_job_id)

        self._prev_print_state = status.print_state

        elapsed = status.elapsed_seconds
        warmup_str = await self._db.get_setting(
            "detection_warmup_seconds",
            str(self._settings.detection_warmup_seconds),
        )
        if warmup_str is None:
            warmup_str = str(self._settings.detection_warmup_seconds)
        try:
            warmup = int(warmup_str)
        except (ValueError, TypeError):
            try:
                warmup = int(self._settings.detection_warmup_seconds)
            except (ValueError, TypeError):
                warmup = 0

        if status.print_state == "paused":
            if self.state != WatcherState.PAUSED:
                try:
                    cooldown_s = float(self._settings.resume_cooldown_seconds)
                except (ValueError, TypeError, AttributeError):
                    cooldown_s = 5.0

                if time.monotonic() - self._last_resume_time < cooldown_s:
                    logger.debug(
                        "Printer status still says 'paused' during post-resume cooldown; ignoring"
                    )
                else:
                    prev_s = self.state
                    logger.info("Printer paused externally — transitioning PAUSED")
                    self.state = WatcherState.PAUSED
                    self._confirm_count = 0
                    if prev_s not in (WatcherState.OFFLINE, WatcherState.STALLED) and getattr(
                        self._settings, "notify_on_print_paused", True
                    ):
                        jpeg = await self._safe_grab_jpeg()
                        self._dispatcher.dispatch_external_pause(jpeg)
            if status.printing and not getattr(status, "stale", False):
                self.last_printed_status = copy.copy(status)
            return

        if elapsed < warmup:
            self.state = WatcherState.WARMUP
            self._confirm_count = 0
        else:
            if self.state in (WatcherState.IDLE, WatcherState.WARMUP):
                logger.info("Printer armed for detection (elapsed=%.0fs)", elapsed)
            # Recover from CAMERA_OFFLINE once printer is still printing —
            # the next _check_frame call will attempt a fresh grab and recover if successful.
            if self.state == WatcherState.CAMERA_OFFLINE:
                pass
            elif self.state == WatcherState.PAUSED and status.print_state == "printing":
                if (
                    self._paused_since is not None
                    and (datetime.now(tz=UTC) - self._paused_since).total_seconds() > 5.0
                ):
                    logger.info("Printer resumed externally — transitioning ARMED")
                    self.state = WatcherState.ARMED
            elif self.state != WatcherState.PAUSED:
                self.state = WatcherState.ARMED

        if status.printing and not getattr(status, "stale", False):
            self.last_printed_status = copy.copy(status)

    async def _check_frame(self, prev_state: WatcherState) -> None:
        try:
            cooldown_s = float(self._settings.resume_cooldown_seconds)
        except (ValueError, TypeError, AttributeError):
            cooldown_s = 5.0

        if time.monotonic() - self._last_resume_time < cooldown_s:
            logger.info("Skipping frame check: within post-resume cooldown window")
            return

        try:
            jpeg = await self._camera.grab()
        except CameraOfflineError:
            if self.state != WatcherState.CAMERA_OFFLINE:
                self.state = WatcherState.CAMERA_OFFLINE
                self._confirm_count = 0
                if prev_state != WatcherState.CAMERA_OFFLINE:
                    logger.warning("Camera offline — suspending detection")
                    self._dispatcher.dispatch_camera_offline()
            return
        except Exception:
            logger.warning("Camera grab failed; skipping this tick")
            self._confirm_count = 0
            return

        if self.state == WatcherState.CAMERA_OFFLINE:
            logger.info("Camera recovered — transitioning ARMED")
            self.state = WatcherState.ARMED

        result: MlResult = await self._ml.detect(jpeg)
        if result.error:
            self._ml_error_count += 1
            logger.warning("ML detection failed (%d consecutive times)", self._ml_error_count)
            if self._ml_error_count >= self._settings.ml_consecutive_failure_threshold:
                logger.error("Too many ML failures — failing CLOSED by pausing printer")
                if not self._ml_failure_alerted:
                    self._ml_failure_alerted = True
                    self._dispatcher.dispatch_text(
                        "🚨 Sentinel ML service is failing continuously. "
                        "Pausing printer for safety."
                    )
                printer_paused = False
                try:
                    await self._printer.pause()
                    printer_paused = True
                except PauseDebouncedError:
                    # No new pause was actually sent — the debounce window suppressed
                    # it. Check live status: if the printer is already paused (e.g.
                    # from a prior command), treat this as success; otherwise leave
                    # _ml_error_count untouched so the next tick retries without
                    # needing to rebuild the failure streak from scratch.
                    logger.warning(
                        "ML-failure pause suppressed by debounce — "
                        "printer may already be paused from a prior command"
                    )
                    try:
                        live_status = await self._printer.status()
                        printer_paused = live_status.print_state == "paused"
                    except Exception:
                        logger.warning(
                            "Could not fetch live printer status after debounced "
                            "ML-failure pause; will retry on next tick"
                        )
                except Exception as e:
                    logger.error("Failed to pause printer on ML failure: %s", e)
                    try:
                        live_status = await self._printer.status()
                        printer_paused = live_status.print_state == "paused"
                    except Exception:
                        logger.warning(
                            "Could not fetch live printer status after failed "
                            "ML-failure pause; will retry on next tick"
                        )

                if printer_paused:
                    self.state = WatcherState.PAUSED
                    self._paused_since = datetime.now(tz=UTC)
                    self._paused_by_sentinel = True
                    self._confirm_count = 0
                    self._ml_error_count = 0
                    self._ml_failure_alerted = False
            return

        self._ml_error_count = 0
        self._ml_failure_alerted = False
        self.last_ml_observation = LastMlObservation(score=result.score, ts=datetime.now(tz=UTC))
        await self._db.set_setting("last_ml_score", str(self.last_ml_observation.score))
        await self._db.set_setting("last_ml_score_ts", self.last_ml_observation.ts.isoformat())

        score_threshold_str = await self._db.get_setting(
            "ml_score_threshold",
            str(self._settings.ml_score_threshold),
        )
        if score_threshold_str is None:
            score_threshold_str = str(self._settings.ml_score_threshold)
        try:
            score_threshold = float(score_threshold_str)
        except (ValueError, TypeError):
            score_threshold = self._settings.ml_score_threshold

        confirm_count_str = await self._db.get_setting(
            "ml_confirm_count",
            str(self._settings.ml_confirm_count),
        )
        if confirm_count_str is None:
            confirm_count_str = str(self._settings.ml_confirm_count)
        try:
            confirm_count = int(confirm_count_str)
        except (ValueError, TypeError):
            confirm_count = self._settings.ml_confirm_count

        if result.score >= score_threshold:
            self._confirm_count += 1
            logger.info(
                "Detection score=%.2f confirm=%d/%d",
                result.score,
                self._confirm_count,
                confirm_count,
            )
            if self._confirm_count >= confirm_count:
                await self._on_confirmed_detection(result, jpeg)
        else:
            if self._confirm_count > 0:
                logger.debug("Score below threshold — resetting confirm counter")
            self._confirm_count = 0
            # Score dropped: a failed-pause retry episode (if any) is over.
            self._pause_failure_alerted = False

    async def _on_confirmed_detection(self, result: MlResult, jpeg: bytes) -> None:
        # A retry is a confirmed detection on the tick right after a pause
        # failure: _confirm_count was restored so the pause is re-attempted
        # immediately, but the operator has already been alerted (and a
        # snapshot + detection row recorded) for this episode. Retries publish
        # the pause and log; they do not re-alert, re-snapshot or re-record.
        is_retry = self._pause_failure_alerted
        if is_retry:
            logger.warning("CONFIRMED DETECTION score=%.2f — retrying printer pause", result.score)
        else:
            logger.warning("CONFIRMED DETECTION score=%.2f — pausing printer", result.score)
        consecutive_count = self._confirm_count
        self._confirm_count = 0
        snapshot_id: str | None = None
        snapshot_path: str | None = None

        if not is_retry:
            snapshot_id = uuid.uuid4().hex
            snapshots_dir = Path(self._db._path).parent / "snapshots"
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
            self._paused_since = datetime.now(tz=UTC)
            self._paused_by_sentinel = True
        except asyncio.CancelledError:
            if pause_sent:
                self.state = WatcherState.PAUSED
                self._paused_since = datetime.now(tz=UTC)
                self._paused_by_sentinel = True
            else:
                logger.critical("Watcher cancelled before printer pause completed")
            raise
        except PauseDebouncedError:
            # The debounce window is active — we did NOT send a new pause command.
            # Inspect the live printer status to determine the real outcome:
            # - if the printer is already paused, the earlier pause took effect → treat as success.
            # - if the printer is still printing, the pause was silently lost → stay ARMED and retry.
            logger.info(
                "Pause suppressed by debounce window — checking live printer status "
                "to determine whether printer is actually paused"
            )
            try:
                live_status = await self._printer.status()
                printer_is_paused = live_status.print_state == "paused"
            except Exception:
                logger.warning(
                    "Could not fetch live printer status after debounced pause; "
                    "staying ARMED to retry on next tick"
                )
                printer_is_paused = False

            if printer_is_paused:
                logger.info("Debounced pause: printer confirmed paused — transitioning PAUSED")
                pause_ok = True
                self.state = WatcherState.PAUSED
                self._paused_since = datetime.now(tz=UTC)
                self._paused_by_sentinel = True
            else:
                logger.warning(
                    "Debounced pause: printer still printing — staying ARMED, will retry next tick"
                )
                # Restore the streak so the next high-score tick retries the
                # pause immediately instead of waiting for N more frames.
                self._confirm_count = consecutive_count
                if not is_retry:
                    self._dispatcher.dispatch_text(
                        "⚠️ Pause suppressed by debounce window but printer is still printing. "
                        "Sentinel remains armed and will retry if failure is still detected."
                    )
        except Exception:
            logger.exception("Printer pause failed — notifying anyway")
            self._confirm_count = consecutive_count
            if not is_retry:
                self._dispatcher.dispatch_text(
                    "⚠️ Printer pause command failed during failure detection! "
                    "G-code is still running. The watcher remains armed and will retry "
                    "if failure is still detected."
                )

        if pause_ok:
            self._pause_failure_alerted = False
        else:
            self._pause_failure_alerted = True

        if not is_retry:
            self._dispatcher.dispatch_detection(result.score, snapshot_id, jpeg)

        try:
            pause_id = await self._db.record_pause(
                source="auto",
                result="ok" if pause_ok else "error",
                error_message=None if pause_ok else "Printer pause failed",
            )
            if not is_retry:
                await self._db.record_detection(
                    score=result.score,
                    consecutive=consecutive_count,
                    confirmed=1,
                    snapshot_path=snapshot_path,
                )
        except Exception:
            logger.exception("DB write failed after detection — notification already dispatched")
            pause_id = None

        if not is_retry:
            await self.cleanup_old_snapshots()

        # pause_id is available for future audit-log / resume-wiring use.
        del pause_id

    async def cleanup_old_snapshots(self) -> None:
        """Clean up old snapshot files from disk and database based on retention limit."""
        try:
            keep_limit = int(self._settings.snapshot_retention_limit)
        except (ValueError, TypeError):
            keep_limit = 50

        try:
            # Batch cleanup in chunks of 100 to bound memory consumption under large row counts
            chunk_size = 100
            while True:
                old_paths = await self._db.get_snapshots_for_cleanup(
                    keep_limit=keep_limit, limit=chunk_size
                )
                if not old_paths:
                    break

                def _delete_files(paths: list[str]) -> list[str]:
                    # We return all paths to clear them from the DB even if disk deletion
                    # fails. Orphaned files are retried by fallback_directory_cleanup().
                    for path_str in paths:
                        if not path_str:
                            continue
                        p = Path(path_str)
                        try:
                            p.unlink(missing_ok=True)
                        except OSError:
                            logger.exception("Failed to delete old snapshot file: %s", p)
                    return paths

                deleted_paths = await asyncio.to_thread(_delete_files, old_paths)
                if deleted_paths:
                    await self._db.delete_old_snapshots(deleted_paths)

                if len(old_paths) < chunk_size:
                    break

            # Fallback directory cleanup for orphaned snapshots on disk
            await self.fallback_directory_cleanup()
        except Exception:
            logger.exception("Failed to clean up old snapshots")

    async def fallback_directory_cleanup(self) -> None:
        """Scan snapshots directory and delete any files that are not referenced in the database."""
        snapshots_dir = Path(self._settings.db_path).parent / "snapshots"
        if not snapshots_dir.exists():
            return

        try:
            active_paths = await self._db.get_all_active_snapshot_paths()
            active_filenames = {Path(p).name for p in active_paths if p}
        except Exception:
            logger.exception("Failed to query active snapshot paths for fallback cleanup")
            return

        def _cleanup_disk() -> None:
            for p in snapshots_dir.glob("*.jpg"):
                if p.name not in active_filenames:
                    try:
                        # Exclude fresh snapshots (modified within 60s) to prevent deletion races
                        try:
                            mtime = p.stat().st_mtime
                            if time.time() - mtime < 60.0:
                                continue
                        except OSError:
                            # If stat fails, be safe and skip deletion this round
                            continue

                        p.unlink()
                        logger.info("Cleaned up orphaned snapshot file: %s", p)
                    except OSError as exc:
                        logger.warning("Failed to delete orphaned snapshot file %s: %s", p, exc)

        await asyncio.to_thread(_cleanup_disk)

    async def _periodic_cleanup(self) -> None:
        """Periodic background task that runs snapshot cleanup and event retention pruning."""
        while self._running:
            await self.cleanup_old_snapshots()

            # Prune old events if retention is configured
            retention_days = self._settings.event_retention_days
            if retention_days > 0:
                try:
                    await self._db.prune_old_events(retention_days)
                except Exception:
                    logger.exception("Failed to prune old events")

            try:
                interval = int(self._settings.snapshot_cleanup_interval_seconds)
            except (ValueError, TypeError):
                interval = 3600

            for _ in range(interval):
                if not self._running:
                    break
                await asyncio.sleep(1)

    async def _watchdog(self) -> None:
        # Sleep for half the stall window so the maximum alert latency is
        # 1.5x stall_seconds rather than 2x (sleep-before-check pattern).
        half = max(15, self._settings.watcher_stall_seconds // 2)
        while self._running:
            try:
                await asyncio.sleep(half)
                await self._watchdog_tick(self._db)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Watcher watchdog tick raised unexpectedly")

    async def _watchdog_tick(self, db: Database) -> None:
        stall_s = self._settings.watcher_stall_seconds

        # Derive the effective stall threshold so that a large poll interval
        # (set via env or POST /api/settings) does not trigger false STALLED alerts.
        # A healthy loop writes a heartbeat on every tick; the earliest the watchdog
        # could see a legitimate stall is 2x the poll interval after the last tick,
        # so we use max(stall_s, 2 * poll_interval) as the effective threshold.
        try:
            poll_interval_str = await db.get_setting(
                "ml_poll_interval_seconds",
                str(self._settings.ml_poll_interval_seconds),
            )
            if poll_interval_str is None:
                poll_interval_str = str(self._settings.ml_poll_interval_seconds)
            poll_interval = float(poll_interval_str)
        except Exception:
            poll_interval = float(self._settings.ml_poll_interval_seconds)
        effective_stall = max(stall_s, 2.0 * poll_interval)

        heartbeat = await db.get_heartbeat()
        if heartbeat is None:
            return
        last = heartbeat.get("last_tick_utc")
        if not last:
            return
        dt = datetime.fromisoformat(last)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        age = (datetime.now(tz=UTC) - dt).total_seconds()

        if age > effective_stall and self.state != WatcherState.STALLED:
            logger.error("Heartbeat stale (age=%.0fs) — watcher stalled", age)
            async with self._state_lock:
                self.state = WatcherState.STALLED
                self._dispatcher.dispatch_stall()

    async def _check_and_send_state_reminders(self) -> None:
        detection_enabled = await self._db.get_setting("detection_enabled", "true")
        if detection_enabled == "false":
            text = "⚠️ A new print has started, but failure detection is currently DISABLED."
            self._dispatcher.dispatch_text(text)

        if self.state == WatcherState.CAMERA_OFFLINE:
            text = "⚠️ A new print has started, but the camera is offline. Detection is suspended."
            self._dispatcher.dispatch_text(text)
            return

        try:
            async with asyncio.timeout(3.0):
                await self._camera.grab()
        except CameraOfflineError:
            text = "⚠️ A new print has started, but the camera is offline. Detection is suspended."
            self._dispatcher.dispatch_text(text)
        except Exception:
            pass

    async def _safe_grab_jpeg(self) -> bytes | None:
        try:
            return await self._camera.grab()
        except Exception:
            return None
