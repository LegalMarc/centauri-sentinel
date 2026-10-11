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
from typing import TYPE_CHECKING, Literal, Protocol

from sentinel.camera.errors import CameraOfflineError
from sentinel.ml.types import LastMlObservation
from sentinel.printer.errors import PauseDebouncedError, PrinterCommandUnacknowledgedError
from sentinel.watcher.state import (
    WatcherState,
    effective_stall_seconds,
    heartbeat_interval_seconds,
)

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

# How long a cancelled watcher waits for an in-flight pause publish to finish
# before giving up and re-raising (shutdown must not close the printer client
# under a half-sent pause).
_PAUSE_PUBLISH_CANCEL_WAIT_SECONDS = 15.0

# After a pause attempt with an unknown outcome (published but unacknowledged, or
# cancelled mid-flight), a "paused" status seen within this window is taken to be
# that pause landing. Later, it is the operator's own pause and
# must not be adopted (adoption makes it eligible for auto-stop).
_UNCONFIRMED_PAUSE_ADOPT_WINDOW_SECONDS = 60.0


# runtime_settings key: "false" when detection was already disabled at the start
# of a snooze, so the snooze's expiry must not re-enable it.
_SNOOZE_RESTORE_KEY = "snooze_restore_detection"

_INTERRUPT_STATES = (WatcherState.OFFLINE, WatcherState.STALLED)


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
        # pause_history source of the current Sentinel-owned pause ("auto" detection
        # or "ml_failure"); only meaningful while _paused_by_sentinel is True.
        self._pause_source: str = "auto"
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
        # True while OFFLINE/STALLED was entered from PAUSED, so the eventual
        # return to ARMED/WARMUP still runs the PAUSED-exit handling.
        self._interrupted_from_paused = False
        # (pause_history source, monotonic time) of a pause attempt whose outcome
        # is genuinely unknown ("unacknowledged": the printer may have acted on
        # it). A "paused" status within _UNCONFIRMED_PAUSE_ADOPT_WINDOW_SECONDS is
        # that pause landing late, not an external one. Never set for "failed"
        # (rejected or never published, so it cannot land). Cleared by a
        # successful pause, IDLE, a job change, leaving PAUSED, an operator
        # pause or resume (note_operator_pause / note_operator_resume) and the
        # adoption/external branch.
        self._pause_outcome_unknown: tuple[str, float] | None = None

    @property
    def state(self) -> WatcherState:
        return self._state

    @state.setter
    def state(self, value: WatcherState) -> None:
        old = self._state
        if old == WatcherState.PAUSED and value in _INTERRUPT_STATES:
            self._interrupted_from_paused = True
        if value in (WatcherState.ARMED, WatcherState.WARMUP) and (
            old == WatcherState.PAUSED
            or (old in _INTERRUPT_STATES and self._interrupted_from_paused)
        ):
            self._last_resume_time = time.monotonic()
            logger.info("Printer resumed — setting resume cooldown anchor")
            # Clear the printer's pause debounce so that a re-detection within
            # the 30-second window publishes a real pause rather than being
            # silently dropped (external resume path — client.resume() handles
            # the command-driven path directly).
            getattr(self._printer, "clear_pause_debounce", lambda: None)()
            # New context: a failed-pause episode from before the pause must
            # not make the next failure look like a silent retry.
            self._reset_detection_episode()
            self._pause_outcome_unknown = None
        if value not in _INTERRUPT_STATES:
            self._interrupted_from_paused = False
        self._state = value

    def _reset_detection_episode(self) -> None:
        """Drop the confirm streak and the alert-once guards.

        Called on every context change (pause/resume, detection toggle, camera
        error, new job) so a later failure is a fresh episode with its own
        alert, snapshot and detection row rather than a silent retry.
        """
        self._confirm_count = 0
        self._pause_failure_alerted = False
        self._ml_failure_alerted = False

    def note_operator_pause(self) -> None:
        """Record that the operator paused (web or Telegram).

        Drops any unknown-outcome pause marker so a later "paused" status is
        never mistaken for Sentinel's own late pause and adopted (adoption makes
        a pause eligible for auto-stop).
        """
        self._pause_outcome_unknown = None

    def note_operator_resume(self) -> None:
        """Record that the operator resumed (web or Telegram).

        The state setter only handles a PAUSED -> ARMED transition. When an
        unacknowledged pause actually landed, the watcher is still ARMED until its
        next tick, so the operator's resume cannot change state. Without this hook
        the confirm streak, the failure-alert guard and the unknown-outcome marker
        survive, and the next high frame re-pauses at once as a silent retry.
        Resets the detection episode, drops the marker and anchors the post-resume
        cooldown, exactly as the PAUSED -> ARMED path does (idempotent with it).
        """
        self._reset_detection_episode()
        self._pause_outcome_unknown = None
        self._last_resume_time = time.monotonic()

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
                        await self._end_snooze()
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
        # Remember whether detection was on when the snooze began, so expiry
        # does not switch on detection the operator had deliberately disabled
        # (/disable, then Snooze). A snooze already in progress keeps the flag
        # recorded by the first one: detection reads as disabled only because
        # of that snooze.
        if not await self._snooze_in_progress():
            was_enabled = await self._db.get_setting("detection_enabled", "true") != "false"
            await self._db.set_setting(_SNOOZE_RESTORE_KEY, "true" if was_enabled else "false")
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

    async def _snooze_in_progress(self) -> bool:
        """True if a snooze has been started and not yet ended or cancelled."""
        try:
            return float(await self._db.get_setting("snooze_until_utc", "0") or 0) > 0
        except (ValueError, TypeError):
            return False

    async def _end_snooze(self) -> bool:
        """Finish a snooze. Re-enables detection only if it was enabled when the
        snooze began; returns whether it did."""
        restore = await self._db.get_setting(_SNOOZE_RESTORE_KEY, "true") != "false"
        if restore:
            await self._db.set_setting("detection_enabled", "true")
        await self._db.set_setting("snooze_until_utc", "0")
        await self._db.set_setting(_SNOOZE_RESTORE_KEY, "true")
        return restore

    async def _re_enable_after(self, delay: float) -> None:
        current_task = asyncio.current_task()
        try:
            with contextlib.suppress(ValueError):
                # If delay is negative or invalid, sleep raises ValueError,
                # we just skip the wait and re-enable immediately.
                await asyncio.sleep(delay)
            if self._snooze_task is current_task:
                if await self._end_snooze():
                    self._dispatcher.dispatch_text("Detection re-enabled after snooze.")
                    logger.info("Detection re-enabled after %.0fs snooze", delay)
                else:
                    logger.info(
                        "Snooze ended after %.0fs; detection stays disabled "
                        "(it was disabled before the snooze)",
                        delay,
                    )
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
            hb_interval = heartbeat_interval_seconds(stall_s)

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

                    if (
                        auto_stop_timeout > 0
                        and pause_duration > auto_stop_timeout
                        and await self._auto_stop_allowed()
                    ):
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
                    # Reset the confirm counter, ML error streak and alert-once
                    # guards so that re-enabling detection does not carry stale
                    # consecutive hits (or a single ML error that would instantly
                    # fail-closed) into the new window.
                    if self._confirm_count > 0:
                        logger.debug("Detection disabled — resetting confirm counter")
                    self._reset_detection_episode()
                    self._ml_error_count = 0

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
            self._reset_detection_episode()
            self._pause_outcome_unknown = None
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
            self._reset_detection_episode()
            self._pause_outcome_unknown = None
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
                    # Single use: adopted or external, the marker is spent.
                    unknown = self._pause_outcome_unknown
                    self._pause_outcome_unknown = None
                    unknown_source = (
                        unknown[0]
                        if unknown is not None
                        and time.monotonic() - unknown[1] <= _UNCONFIRMED_PAUSE_ADOPT_WINDOW_SECONDS
                        else None
                    )
                    if unknown_source is not None:
                        logger.warning(
                            "Printer reports paused after an unconfirmed Sentinel pause — "
                            "treating it as our pause, not an external one"
                        )
                        self._mark_paused_by_sentinel(unknown_source)
                        self._reset_detection_episode()
                        await self._record_pause_row(unknown_source, True)
                    else:
                        logger.info("Printer paused externally — transitioning PAUSED")
                        self.state = WatcherState.PAUSED
                        self._reset_detection_episode()
                    if (
                        unknown_source is None
                        and prev_s not in (WatcherState.OFFLINE, WatcherState.STALLED)
                        and getattr(self._settings, "notify_on_print_paused", True)
                    ):
                        jpeg = await self._safe_grab_jpeg()
                        self._dispatcher.dispatch_external_pause(jpeg)
            if status.printing and not getattr(status, "stale", False):
                self.last_printed_status = copy.copy(status)
            return

        if elapsed < warmup:
            self.state = WatcherState.WARMUP
            self._reset_detection_episode()
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
                self._reset_detection_episode()
                if prev_state != WatcherState.CAMERA_OFFLINE:
                    logger.warning("Camera offline — suspending detection")
                    self._dispatcher.dispatch_camera_offline()
            return
        except Exception:
            logger.warning("Camera grab failed; skipping this tick")
            self._reset_detection_episode()
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
                outcome = await self._publish_pause(source="ml_failure", verify_on_error=True)
                await self._record_pause_row("ml_failure", outcome == "ok")
                if outcome == "unacknowledged":
                    self._pause_outcome_unknown = ("ml_failure", time.monotonic())
                if outcome == "ok":
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

    async def _publish_pause(
        self, *, source: str = "auto", verify_on_error: bool = False
    ) -> Literal["ok", "debounced", "failed", "unacknowledged"]:
        """Publish a pause and, on success, move to PAUSED. The only place the
        watcher sends a pause command (detection and ML fail-closed both use it).

        The publish runs as its own task behind a shield so a cancellation
        arriving mid-call cannot abort the MQTT command. On cancellation we wait
        (bounded) for that task before re-raising, so shutdown does not close the
        printer client under a half-sent pause. State only becomes PAUSED after
        the command is acknowledged or live status confirms the printer is
        paused; otherwise the watcher stays ARMED and the next tick retries.

        Returns "ok", "debounced" (suppressed and the printer is still printing),
        "unacknowledged" (the outcome is unknown: the command was sent but never
        confirmed, or the call was cancelled mid-flight) or "failed" (the command
        was never published or the printer rejected it, so it cannot land). Only
        "unacknowledged" arms the late-pause adoption marker. With
        verify_on_error, a raised command is followed by a live status check and
        counts as "ok" if the printer is in fact paused (the ML fail-closed path,
        which has no detection to retry). *source* is the pause_history source
        ("auto" or "ml_failure") recorded on the Sentinel-owned pause, which decides
        whether auto-stop may escalate it.
        """

        async def _publish() -> None:
            await self._printer.pause()

        inner = asyncio.create_task(_publish(), name="watcher-pause-publish")
        # Retrieve the result so a late failure after a timed-out wait is not
        # reported as a never-retrieved task exception.
        inner.add_done_callback(lambda t: t.cancelled() or t.exception())

        try:
            await asyncio.shield(inner)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if inner.cancelled() and task is not None and task.cancelling() == 0:
                # The pause call itself ended cancelled while the watcher is not
                # being cancelled: a failed pause, not a shutdown. Re-raising
                # would end the watcher loop silently.
                # The command may or may not have reached the printer.
                logger.error("Printer pause was cancelled internally; outcome unknown")
                return "unacknowledged"
            if not inner.done():
                await asyncio.wait({inner}, timeout=_PAUSE_PUBLISH_CANCEL_WAIT_SECONDS)
            if inner.done() and not inner.cancelled() and inner.exception() is None:
                self._mark_paused_by_sentinel(source)
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
                self._mark_paused_by_sentinel(source)
                return "ok"
            logger.warning(
                "Debounced pause: printer still printing — staying ARMED, will retry next tick"
            )
            return "debounced"
        except Exception as exc:
            logger.error("Printer pause failed: %s", exc)
            if verify_on_error:
                # A pause that raised may still have reached the printer: trust
                # live status over the exception.
                try:
                    live_status = await self._printer.status()
                    if live_status.print_state == "paused":
                        self._mark_paused_by_sentinel(source)
                        return "ok"
                except Exception:
                    logger.warning(
                        "Could not fetch live printer status after failed "
                        "pause; will retry on next tick"
                    )
            if isinstance(exc, PrinterCommandUnacknowledgedError):
                return "unacknowledged"
            return "failed"

        self._mark_paused_by_sentinel(source)
        return "ok"

    def _mark_paused_by_sentinel(self, source: str = "auto") -> None:
        self.state = WatcherState.PAUSED
        self._paused_since = datetime.now(tz=UTC)
        self._paused_by_sentinel = True
        self._pause_source = source
        self._pause_outcome_unknown = None

    async def _auto_stop_allowed(self) -> bool:
        """False for an ML-outage pause unless the operator opted in to stopping on it.

        The runtime setting is read each time so a dashboard change applies at once.
        """
        if self._pause_source != "ml_failure":
            return True
        default = bool(getattr(self._settings, "ml_failure_auto_stop", False))
        raw = await self._db.get_setting("ml_failure_auto_stop", str(default).lower())
        return raw == "true"

    async def _record_pause_row(self, source: str, ok: bool) -> None:
        try:
            await self._db.record_pause(
                source=source,
                result="ok" if ok else "error",
                error_message=None if ok else "Printer pause failed",
            )
        except Exception:
            logger.exception("DB write of pause_history failed (source=%s)", source)

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

        outcome = await self._publish_pause()
        pause_ok = outcome == "ok"
        if outcome == "unacknowledged":
            self._pause_outcome_unknown = ("auto", time.monotonic())
        if outcome == "debounced":
            # Restore the streak so the next high-score tick retries the
            # pause immediately instead of waiting for N more frames.
            self._confirm_count = consecutive_count
            if not is_retry:
                self._dispatcher.dispatch_text(
                    "⚠️ Pause suppressed by debounce window but printer is still printing. "
                    "Sentinel remains armed and will retry if failure is still detected."
                )
        elif outcome == "failed":
            self._confirm_count = consecutive_count
            if not is_retry:
                self._dispatcher.dispatch_text(
                    "⚠️ Printer pause command failed during failure detection! "
                    "G-code is still running. The watcher remains armed and will retry "
                    "if failure is still detected."
                )
        elif outcome == "unacknowledged":
            self._confirm_count = consecutive_count
            if not is_retry:
                self._dispatcher.dispatch_text(
                    "⚠️ The printer did not confirm the pause command during failure "
                    "detection, so it may or may not have taken effect. Check the printer. "
                    "The watcher remains armed and will retry if failure is still detected."
                )

        self._pause_failure_alerted = not pause_ok

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

        # A large poll interval (env or POST /api/settings) or heartbeat
        # throttling must not trigger false STALLED alerts; see
        # effective_stall_seconds for the derivation.
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
        effective_stall = effective_stall_seconds(stall_s, poll_interval)

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
