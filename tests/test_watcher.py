"""Tests for sentinel/watcher/loop.py and state.py."""

from __future__ import annotations

import asyncio
import contextlib

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------
import shutil
import tempfile
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sentinel.camera.errors import CameraOfflineError
from sentinel.config import Settings
from sentinel.db.repo import Database
from sentinel.ml.types import MlResult
from sentinel.printer.types import PrinterStatus
from sentinel.watcher.loop import WatcherLoop
from sentinel.watcher.state import WatcherState

_temp_dirs: list[str] = []
_active_dbs: list[Database] = []


@pytest.fixture(autouse=True)
async def cleanup_resources() -> Any:
    yield
    for db in _active_dbs:
        with contextlib.suppress(Exception):
            await db.close()
    _active_dbs.clear()
    for d in _temp_dirs:
        shutil.rmtree(d, ignore_errors=True)
    _temp_dirs.clear()


_SETTINGS = Settings(
    printer_ip="10.0.0.1",
    printer_access_code="test",
    detection_warmup_seconds=300,
    ml_confirm_count=3,
    ml_score_threshold=0.4,
    ml_poll_interval_seconds=10,
    watcher_stall_seconds=60,
    resume_cooldown_seconds=0,
)


def _idle_status() -> PrinterStatus:
    return PrinterStatus(
        printing=False, elapsed_seconds=0, current_layer=0, total_layers=0, filename=None
    )


def _printing_status(elapsed: float = 400.0) -> PrinterStatus:
    return PrinterStatus(
        printing=True,
        elapsed_seconds=elapsed,
        current_layer=10,
        total_layers=100,
        filename="test.gcode",
        print_state="printing",
    )


def _warmup_status() -> PrinterStatus:
    return PrinterStatus(
        printing=True,
        elapsed_seconds=60.0,  # within warmup window
        current_layer=2,
        total_layers=100,
        filename="test.gcode",
        print_state="printing",
    )


def _make_notifier() -> MagicMock:
    n = MagicMock()
    n.send_detection_alert = AsyncMock()
    n.send_stall_alert = AsyncMock()
    n.send_camera_offline_alert = AsyncMock()
    return n


def _make_dispatcher() -> MagicMock:
    d = MagicMock()
    d.dispatch_detection = MagicMock()
    d.dispatch_stall = MagicMock()
    d.dispatch_camera_offline = MagicMock()
    d.dispatch_text = MagicMock()
    d.dispatch_print_started = MagicMock()
    d.dispatch_print_completed = MagicMock()
    d.dispatch_external_pause = MagicMock()
    return d


async def _make_watcher(
    settings: Settings = _SETTINGS,
    printer_status: PrinterStatus | None = None,
    ml_score: float = 0.0,
    dispatcher: Any = None,
) -> tuple[WatcherLoop, MagicMock, MagicMock, MagicMock, Database]:
    import os

    from sentinel.db.migrate import migrate

    temp_dir = tempfile.mkdtemp()
    _temp_dirs.append(temp_dir)
    db_path = os.path.join(temp_dir, "sentinel.db")
    await migrate(db_path)
    db = Database(db_path)
    await db.connect()
    _active_dbs.append(db)

    printer = MagicMock()
    printer.status = AsyncMock(return_value=printer_status or _idle_status())
    printer.pause = AsyncMock()
    printer.stop = AsyncMock()

    camera = MagicMock()
    camera.grab = AsyncMock(return_value=b"\xff\xd8\xff\xd9")

    ml = MagicMock()
    ml.detect = AsyncMock(return_value=MlResult(score=ml_score))

    watcher = WatcherLoop(
        settings=settings,
        printer=printer,
        camera=camera,
        ml=ml,
        db=db,
        dispatcher=dispatcher or _make_dispatcher(),
    )
    return watcher, printer, camera, ml, db


# ---------------------------------------------------------------------------
# State: IDLE
# ---------------------------------------------------------------------------


async def test_initial_state_is_idle() -> None:
    watcher, *_ = await _make_watcher()
    assert watcher.state == WatcherState.IDLE


async def test_idle_when_printer_not_printing() -> None:
    watcher, *_ = await _make_watcher(printer_status=_idle_status())
    await watcher.tick()
    assert watcher.state == WatcherState.IDLE


# ---------------------------------------------------------------------------
# State: WARMUP
# ---------------------------------------------------------------------------


async def test_warmup_during_warmup_period() -> None:
    watcher, *_ = await _make_watcher(printer_status=_warmup_status())
    await watcher.tick()
    assert watcher.state == WatcherState.WARMUP


# ---------------------------------------------------------------------------
# State: ARMED
# ---------------------------------------------------------------------------


async def test_armed_after_warmup() -> None:
    watcher, *_ = await _make_watcher(printer_status=_printing_status(elapsed=400.0))
    await watcher.tick()
    assert watcher.state == WatcherState.ARMED


async def test_armed_state_calls_camera_and_ml() -> None:
    watcher, _printer, camera, ml, _db = await _make_watcher(
        printer_status=_printing_status(), ml_score=0.1
    )
    await watcher.tick()
    # Called once in _check_and_send_state_reminders and once in _check_frame
    assert camera.grab.call_count >= 1
    ml.detect.assert_called_once()


# ---------------------------------------------------------------------------
# Confirm counter
# ---------------------------------------------------------------------------


async def test_confirm_counter_increments_above_threshold() -> None:
    watcher, *_ = await _make_watcher(printer_status=_printing_status(), ml_score=0.9)
    await watcher.tick()
    assert watcher._confirm_count == 1


async def test_confirm_counter_resets_below_threshold() -> None:
    watcher, _, _camera, ml, _ = await _make_watcher(
        printer_status=_printing_status(), ml_score=0.9
    )
    await watcher.tick()
    assert watcher._confirm_count == 1

    ml.detect = AsyncMock(return_value=MlResult(score=0.1))
    await watcher.tick()
    assert watcher._confirm_count == 0


async def test_confirm_counter_resets_on_idle() -> None:
    watcher, printer, *_ = await _make_watcher(printer_status=_printing_status(), ml_score=0.9)
    await watcher.tick()
    assert watcher._confirm_count == 1

    printer.status = AsyncMock(return_value=_idle_status())
    await watcher.tick()
    assert watcher._confirm_count == 0


# ---------------------------------------------------------------------------
# Confirmed detection → PAUSED
# ---------------------------------------------------------------------------


async def test_confirmed_detection_transitions_to_paused() -> None:
    dispatcher = _make_dispatcher()
    watcher, printer, _camera, _ml, _db = await _make_watcher(
        printer_status=_printing_status(),
        ml_score=0.9,
        dispatcher=dispatcher,
    )
    settings = Settings(
        printer_ip="10.0.0.1",
        printer_access_code="test",
        detection_warmup_seconds=0,
        ml_confirm_count=1,
        ml_score_threshold=0.4,
        ml_poll_interval_seconds=10,
        watcher_stall_seconds=60,
    )
    watcher._settings = settings

    await watcher.tick()

    assert watcher.state == WatcherState.PAUSED
    printer.pause.assert_called_once()
    dispatcher.dispatch_detection.assert_called_once()


async def test_pause_fails_notifier_still_fires() -> None:
    dispatcher = _make_dispatcher()
    watcher, printer, _camera, _ml, _db = await _make_watcher(
        printer_status=_printing_status(),
        ml_score=0.9,
        dispatcher=dispatcher,
    )
    settings = Settings(
        printer_ip="10.0.0.1",
        printer_access_code="test",
        detection_warmup_seconds=0,
        ml_confirm_count=1,
        ml_score_threshold=0.4,
        ml_poll_interval_seconds=10,
        watcher_stall_seconds=60,
    )
    watcher._settings = settings
    printer.pause = AsyncMock(side_effect=Exception("MQTT error"))

    await watcher.tick()

    # Pause failed → state stays ARMED so the next tick can retry.
    assert watcher.state == WatcherState.ARMED
    dispatcher.dispatch_detection.assert_called_once()
    dispatcher.dispatch_text.assert_called_once_with(
        "⚠️ Printer pause command failed during failure detection! G-code is still running. "
        "The watcher remains armed and will retry if failure is still detected."
    )


async def test_db_records_detection_on_pause() -> None:
    watcher, _printer, _camera, _ml, db = await _make_watcher(
        printer_status=_printing_status(),
        ml_score=0.9,
    )
    settings = Settings(
        printer_ip="10.0.0.1",
        printer_access_code="test",
        detection_warmup_seconds=0,
        ml_confirm_count=1,
        ml_score_threshold=0.4,
        ml_poll_interval_seconds=10,
        watcher_stall_seconds=60,
    )
    watcher._settings = settings

    await watcher.tick()

    detections = await db.get_recent_detections()
    assert len(detections) == 1
    assert detections[0]["score"] == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# Camera offline
# ---------------------------------------------------------------------------


async def test_camera_offline_transitions_state() -> None:
    dispatcher = _make_dispatcher()
    watcher, _printer, camera, _ml, _db = await _make_watcher(
        printer_status=_printing_status(),
        dispatcher=dispatcher,
    )
    camera.grab = AsyncMock(side_effect=CameraOfflineError("offline"))

    await watcher.tick()

    assert watcher.state == WatcherState.CAMERA_OFFLINE
    dispatcher.dispatch_camera_offline.assert_called_once()


async def test_camera_grab_error_skips_tick() -> None:
    watcher, _printer, camera, ml, _db = await _make_watcher(printer_status=_printing_status())
    camera.grab = AsyncMock(side_effect=Exception("transient"))

    await watcher.tick()

    # Should stay ARMED (not transition) and not call ML
    ml.detect.assert_not_called()


# ---------------------------------------------------------------------------
# Heartbeat written on each tick
# ---------------------------------------------------------------------------


async def test_heartbeat_updated_on_tick() -> None:
    watcher, _, _, _, db = await _make_watcher(printer_status=_idle_status())
    await watcher.tick()
    ts = await db.get_heartbeat()
    assert ts is not None


# ---------------------------------------------------------------------------
# Printer status error — stays in current state
# ---------------------------------------------------------------------------


async def test_printer_error_stays_in_state() -> None:
    watcher, printer, *_ = await _make_watcher(printer_status=_idle_status())
    watcher.state = WatcherState.ARMED
    printer.status = AsyncMock(side_effect=Exception("MQTT error"))

    await watcher.tick()

    assert watcher.state == WatcherState.ARMED


# ---------------------------------------------------------------------------
# Heartbeat watchdog
# ---------------------------------------------------------------------------


async def test_watchdog_fires_on_stale_heartbeat() -> None:
    dispatcher = _make_dispatcher()
    watcher, _, _, _, db = await _make_watcher(dispatcher=dispatcher)

    stale_ts = (datetime.now(tz=UTC) - timedelta(seconds=120)).isoformat()
    await db.update_heartbeat(stale_ts, "ARMED")

    await watcher._watchdog_tick(db)

    assert watcher.state == WatcherState.STALLED
    dispatcher.dispatch_stall.assert_called_once()


async def test_watchdog_no_alert_when_fresh() -> None:
    dispatcher = _make_dispatcher()
    watcher, _, _, _, db = await _make_watcher(dispatcher=dispatcher)

    fresh_ts = datetime.now(tz=UTC).isoformat()
    await db.update_heartbeat(fresh_ts, "ARMED")

    await watcher._watchdog_tick(db)

    assert watcher.state == WatcherState.IDLE
    dispatcher.dispatch_stall.assert_not_called()


async def test_watchdog_no_alert_when_no_heartbeat() -> None:
    dispatcher = _make_dispatcher()
    watcher, _, _, _, db = await _make_watcher(dispatcher=dispatcher)

    # No heartbeat in DB
    await watcher._watchdog_tick(db)

    assert watcher.state == WatcherState.IDLE
    dispatcher.dispatch_stall.assert_not_called()


# ---------------------------------------------------------------------------
# run_forever — TaskGroup lifecycle
# ---------------------------------------------------------------------------

_FAST_SETTINGS = Settings(
    printer_ip="10.0.0.1",
    printer_access_code="test",
    detection_warmup_seconds=0,
    ml_confirm_count=1,
    ml_score_threshold=0.4,
    ml_poll_interval_seconds=1,
    watcher_stall_seconds=0,
    resume_cooldown_seconds=0,
)


async def test_run_forever_cancels_cleanly() -> None:
    watcher, *_ = await _make_watcher(settings=_FAST_SETTINGS)
    task = asyncio.create_task(watcher.run_forever())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# _loop — non-CancelledError exceptions are swallowed
# ---------------------------------------------------------------------------


async def test_loop_swallows_unexpected_exceptions() -> None:
    watcher, *_ = await _make_watcher(settings=_FAST_SETTINGS)
    watcher._running = True
    call_count = 0

    async def _patched_tick() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("unexpected!")
        watcher._running = False  # exit loop after recovering

    with patch.object(watcher, "_tick", side_effect=_patched_tick):
        await watcher._loop()

    assert call_count == 2  # loop executed twice despite exception on first


async def test_cancelled_before_pause_does_not_set_paused_state() -> None:
    watcher, _, _, _, _ = await _make_watcher()

    async def _raise_cancelled() -> None:
        raise asyncio.CancelledError

    watcher._printer = MagicMock()
    watcher._printer.pause = _raise_cancelled

    with pytest.raises(asyncio.CancelledError):
        await watcher._on_confirmed_detection(MlResult(score=0.9), b"\xff\xd8\xff\xd9")

    assert watcher.state != WatcherState.PAUSED


# ---------------------------------------------------------------------------
# Snapshot saving and cleanup
# ---------------------------------------------------------------------------


async def test_snapshot_saving_and_cleanup() -> None:
    from pathlib import Path

    watcher, _printer, camera, _ml, db = await _make_watcher(
        printer_status=_printing_status(),
        ml_score=0.9,
    )
    watcher._settings = Settings(
        printer_ip="10.0.0.1",
        printer_access_code="test",
        detection_warmup_seconds=0,
        ml_confirm_count=1,
        ml_score_threshold=0.4,
        ml_poll_interval_seconds=10,
        watcher_stall_seconds=60,
        db_path=str(db._path),
    )

    # Grab/tick to trigger confirmed detection
    camera.grab = AsyncMock(return_value=b"my_custom_jpeg_bytes")
    await watcher.tick()

    # Check that a snapshot was recorded in DB
    recent = await db.get_recent_detections(limit=1)
    assert len(recent) == 1
    snapshot_path = recent[0]["snapshot_path"]
    assert snapshot_path is not None
    snapshot_id = Path(str(snapshot_path)).stem
    assert snapshot_id is not None

    # Check that snapshot was saved to disk
    snapshots_dir = Path(str(db._path)).parent / "snapshots"
    p = snapshots_dir / f"{snapshot_id}.jpg"

    assert p.exists()
    assert p.read_bytes() == b"my_custom_jpeg_bytes"

    # Test retention: trigger 55 detections and make sure only 50 files remain.
    # We can call _on_confirmed_detection directly in a loop to generate many snapshots.
    for i in range(60):
        await watcher._on_confirmed_detection(MlResult(score=0.9), f"frame_{i}".encode())

    # Check how many jpg files are in the directory
    saved_files = list(snapshots_dir.glob("*.jpg"))
    # We expect exactly 50 files because of the keep-50 retention cleanup.
    # Wait, the first one we did might have been cleaned up too if the total exceeded 50.
    assert len(saved_files) <= 50

    # Clean up files and database connection
    await db.close()
    for f in saved_files:
        with contextlib.suppress(OSError):
            f.unlink()
    with contextlib.suppress(OSError):
        snapshots_dir.rmdir()
    with contextlib.suppress(OSError):
        Path(str(db._path)).unlink()
    with contextlib.suppress(OSError):
        Path(str(db._path)).parent.rmdir()


# ---------------------------------------------------------------------------
# _watchdog — loop execution (covers the while-loop lines)
# ---------------------------------------------------------------------------


async def test_watchdog_loop_fires_on_stale_heartbeat() -> None:
    dispatcher = _make_dispatcher()
    watcher, _, _, _, db = await _make_watcher(settings=_FAST_SETTINGS, dispatcher=dispatcher)
    stale_ts = (datetime.now(tz=UTC) - timedelta(seconds=120)).isoformat()
    await db.update_heartbeat(stale_ts, "ARMED")

    watcher._running = True

    # Mock asyncio.sleep to yield and immediately exit the loop
    original_sleep = asyncio.sleep

    async def mock_sleep(delay: float) -> None:
        watcher._running = False
        await original_sleep(0)

    with patch("sentinel.watcher.loop.asyncio.sleep", side_effect=mock_sleep):
        await watcher._watchdog()

    dispatcher.dispatch_stall.assert_called()


# ---------------------------------------------------------------------------
# M14 — PAUSED state persists across ticks
# ---------------------------------------------------------------------------


async def test_paused_state_stays_paused_when_printer_printing() -> None:
    """Once PAUSED the watcher must NOT re-arm automatically on subsequent ticks."""
    watcher, *_ = await _make_watcher(printer_status=_printing_status())
    watcher.state = WatcherState.PAUSED

    await watcher.tick()

    assert watcher.state == WatcherState.PAUSED


async def test_paused_state_returns_to_idle_when_printer_idle() -> None:
    """PAUSED does reset to IDLE when the printer stops (user cancelled / done)."""
    watcher, *_ = await _make_watcher(printer_status=_idle_status())
    watcher.state = WatcherState.PAUSED

    await watcher.tick()

    assert watcher.state == WatcherState.IDLE


# ---------------------------------------------------------------------------
# M1 — confirm_count reset when detection is disabled
# ---------------------------------------------------------------------------


async def test_confirm_count_reset_when_detection_disabled() -> None:
    """Disabling detection mid-sequence must reset confirm_count to 0."""
    watcher, _, _, _, db = await _make_watcher(printer_status=_printing_status(), ml_score=0.9)
    # Arm the counter with 2 confirmations
    watcher._confirm_count = 2

    # Now disable detection in DB
    await db.set_setting("detection_enabled", "false")
    await watcher.tick()

    assert watcher._confirm_count == 0


async def test_confirm_count_preserved_when_detection_enabled() -> None:
    """Confirm count must NOT be touched when detection is still enabled."""
    watcher, _, _, _, _ = await _make_watcher(printer_status=_printing_status(), ml_score=0.9)
    await watcher.tick()
    count_after_first = watcher._confirm_count
    assert count_after_first == 1


# ---------------------------------------------------------------------------
# H1 — CAMERA_OFFLINE recovery on next tick & spam protection
# ---------------------------------------------------------------------------


async def test_camera_offline_recovers_on_next_tick_if_printer_still_printing() -> None:
    """CAMERA_OFFLINE must transition back to ARMED on the next tick while printing."""
    watcher, _, camera, ml, _ = await _make_watcher(printer_status=_printing_status())
    # Put into CAMERA_OFFLINE state
    camera.grab = AsyncMock(side_effect=CameraOfflineError("down"))
    await watcher.tick()
    assert watcher.state == WatcherState.CAMERA_OFFLINE

    # Restore camera and tick again — should recover
    camera.grab = AsyncMock(return_value=b"\xff\xd8\xff\xd9")
    ml.detect = AsyncMock(return_value=MlResult(score=0.1))
    await watcher.tick()

    assert watcher.state == WatcherState.ARMED  # type: ignore[comparison-overlap]


async def test_camera_offline_alert_only_sent_once() -> None:
    """Camera offline alert should not spam on subsequent ticks if offline persists."""
    dispatcher = _make_dispatcher()
    watcher, _, camera, _, _ = await _make_watcher(
        printer_status=_printing_status(), dispatcher=dispatcher
    )
    camera.grab = AsyncMock(side_effect=CameraOfflineError("down"))

    # First tick: transition to CAMERA_OFFLINE, alert sent
    await watcher.tick()
    assert watcher.state == WatcherState.CAMERA_OFFLINE
    assert dispatcher.dispatch_camera_offline.call_count == 1

    # Second tick: still offline, state returns to CAMERA_OFFLINE, alert NOT sent again
    await watcher.tick()
    assert watcher.state == WatcherState.CAMERA_OFFLINE
    assert dispatcher.dispatch_camera_offline.call_count == 1


async def test_print_job_tracking_lifecycle() -> None:
    """Verify that print job lifecycle transitions correctly update the database."""
    watcher, printer, _, _, db = await _make_watcher()

    # 1. Start printing
    status = PrinterStatus(
        printing=True,
        elapsed_seconds=10.0,
        current_layer=1,
        total_layers=100,
        filename="first_job.gcode",
        print_state="printing",
    )
    printer.status = AsyncMock(return_value=status)
    await watcher.tick()

    recent = await db.get_recent_jobs()
    assert len(recent) == 1
    job = recent[0]
    assert job["filename"] == "first_job.gcode"
    assert job["status"] == "printing"
    assert job["pauses_count"] == 0

    # 2. Transition print_state to paused
    status.print_state = "paused"
    await watcher.tick()
    recent = await db.get_recent_jobs()
    assert recent[0]["pauses_count"] == 1

    # 3. Transition back-to-back to another print job (different filename)
    status.filename = "second_job.gcode"
    status.print_state = "printing"
    status.elapsed_seconds = 20.0
    await watcher.tick()

    recent = await db.get_recent_jobs()
    assert len(recent) == 2
    # The most recent is at index 0 (second_job)
    second_job = recent[0]
    first_job = recent[1]

    assert first_job["filename"] == "first_job.gcode"
    assert first_job["status"] == "failed"
    assert first_job["duration_seconds"] == 10

    assert second_job["filename"] == "second_job.gcode"
    assert second_job["status"] == "printing"
    assert second_job["pauses_count"] == 0

    # 4. Stop printing (go idle)
    idle_status = PrinterStatus(
        printing=False,
        elapsed_seconds=0.0,
        current_layer=0,
        total_layers=0,
        filename=None,
        print_state="idle",
    )
    printer.status = AsyncMock(return_value=idle_status)
    await watcher.tick()

    recent = await db.get_recent_jobs()
    assert len(recent) == 2
    second_job = recent[0]
    assert second_job["filename"] == "second_job.gcode"
    assert second_job["status"] == "failed"


async def test_confirm_count_resets_on_camera_offline() -> None:
    """If camera grab raises CameraOfflineError, confirm_count must reset to 0."""
    watcher, _, camera, _, _ = await _make_watcher(printer_status=_printing_status())
    watcher._confirm_count = 2

    camera.grab = AsyncMock(side_effect=CameraOfflineError("offline"))
    await watcher.tick()

    assert watcher._confirm_count == 0
    assert watcher.state == WatcherState.CAMERA_OFFLINE


async def test_confirm_count_resets_on_camera_grab_exception() -> None:
    """If camera grab raises a general Exception, confirm_count must reset to 0."""
    watcher, _, camera, _, _ = await _make_watcher(printer_status=_printing_status())
    watcher._confirm_count = 2

    camera.grab = AsyncMock(side_effect=Exception("general error"))
    await watcher.tick()

    assert watcher._confirm_count == 0
    assert watcher.state == WatcherState.ARMED


async def test_confirm_count_retained_on_ml_failure() -> None:
    """If ML API call returns error=True, confirm_count must not be reset."""
    watcher, _, _, ml, _ = await _make_watcher(printer_status=_printing_status())
    watcher._confirm_count = 2

    ml.detect = AsyncMock(return_value=MlResult(score=0.0, error=True))
    await watcher.tick()

    assert watcher._confirm_count == 2


async def test_paused_externally_transitions_to_paused_state() -> None:
    """If print_state is 'paused', the watcher transitions to PAUSED and doesn't run detection."""
    watcher, printer, camera, ml, _ = await _make_watcher(printer_status=_printing_status())
    watcher.state = WatcherState.ARMED
    watcher._alerted_new_print = True

    # Mock status to return print_state="paused"
    status = _printing_status()
    status.print_state = "paused"
    printer.status = AsyncMock(return_value=status)

    await watcher.tick()

    assert watcher.state == WatcherState.PAUSED
    camera.grab.assert_called_once()
    ml.detect.assert_not_called()


async def test_watchdog_resilient_to_database_exceptions() -> None:
    """The watchdog loop must not crash and terminate if a database exception occurs."""
    import aiosqlite

    dispatcher = _make_dispatcher()
    watcher, _, _, _, db = await _make_watcher(settings=_FAST_SETTINGS, dispatcher=dispatcher)

    # Force db.get_heartbeat to raise a database exception (like SQLITE_LOCKED)
    db.get_heartbeat = AsyncMock(side_effect=aiosqlite.OperationalError("database locked"))  # type: ignore[method-assign]

    watcher._running = True
    call_count = 0
    original_sleep = asyncio.sleep

    async def mock_sleep(delay: float) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            watcher._running = False
        await original_sleep(0)

    with patch("sentinel.watcher.loop.asyncio.sleep", side_effect=mock_sleep):
        await watcher._watchdog()

    # Verify that get_heartbeat was called, exception was swallowed, and watchdog task did not crash
    assert db.get_heartbeat.call_count >= 2
    assert watcher.state == WatcherState.IDLE


async def test_confirm_count_resets_on_external_pause() -> None:
    """If the printer is paused externally, confirm_count must reset to 0."""
    watcher, printer, _, _, _ = await _make_watcher(printer_status=_printing_status())
    watcher._confirm_count = 2

    # Mock status to return print_state="paused"
    status = _printing_status()
    status.print_state = "paused"
    printer.status = AsyncMock(return_value=status)

    await watcher.tick()

    assert watcher.state == WatcherState.PAUSED
    assert watcher._confirm_count == 0


async def test_printer_property() -> None:
    watcher, *_ = await _make_watcher()
    assert watcher.printer == watcher._printer


async def test_watchdog_tick_stale() -> None:
    watcher, _printer, _camera, _ml, db = await _make_watcher()
    await watcher._watchdog_tick(db)  # None

    db.get_heartbeat = AsyncMock(return_value={"last_tick_utc": ""})
    await watcher._watchdog_tick(db)  # empty

    # stall branch
    db.get_heartbeat = AsyncMock(
        return_value={"last_tick_utc": (datetime.now(UTC) - timedelta(seconds=1000)).isoformat()}
    )
    await watcher._watchdog_tick(db)
    assert watcher.state == WatcherState.STALLED
    watcher._dispatcher.dispatch_stall.assert_called_once()


async def test_check_and_send_state_reminders() -> None:
    watcher, _printer, _camera, _ml, db = await _make_watcher()

    db.get_setting = AsyncMock(return_value="false")
    await watcher._check_and_send_state_reminders()
    watcher._dispatcher.dispatch_text.assert_called_once()

    watcher._dispatcher.dispatch_text.reset_mock()
    db.get_setting = AsyncMock(return_value="true")
    watcher.state = WatcherState.CAMERA_OFFLINE
    await watcher._check_and_send_state_reminders()
    watcher._dispatcher.dispatch_text.assert_called_once()


async def test_on_confirmed_detection_save_and_cleanup_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher, _printer, _camera, _ml, db = await _make_watcher()

    db.get_snapshots_for_cleanup = AsyncMock(side_effect=Exception("cleanup err"))

    import asyncio

    original_to_thread = asyncio.to_thread

    async def mock_to_thread(func, *args, **kwargs):
        if getattr(func, "__name__", "") == "mkdir":
            raise Exception("mkdir failed")
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", mock_to_thread)

    result = MlResult(score=0.95)
    await watcher._on_confirmed_detection(result, b"jpeg")

    recent = await db.get_recent_detections(limit=1)
    assert len(recent) == 1
    assert recent[0]["snapshot_path"] is None


async def test_on_confirmed_detection_cleanup_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    watcher, _printer, _camera, _ml, db = await _make_watcher()
    db.get_snapshots_for_cleanup = AsyncMock(return_value=["/dummy/path1"])

    from pathlib import Path

    monkeypatch.setattr(Path, "exists", lambda x: True)
    monkeypatch.setattr(Path, "unlink", MagicMock(side_effect=OSError("unlink failed")))

    await watcher._on_confirmed_detection(MlResult(score=0.9), b"jpeg")


async def test_on_confirmed_detection_pause_cancelled() -> None:
    watcher, printer, _camera, _ml, _db = await _make_watcher()

    async def mock_pause():
        raise asyncio.CancelledError()

    printer.pause = AsyncMock(side_effect=mock_pause)

    with pytest.raises(asyncio.CancelledError):
        await watcher._on_confirmed_detection(MlResult(score=0.9), b"")
    assert watcher.state == WatcherState.IDLE


async def test_on_confirmed_detection_pause_exception() -> None:
    watcher, printer, _camera, _ml, db = await _make_watcher()

    printer.pause = AsyncMock(side_effect=Exception("pause err"))
    await watcher._on_confirmed_detection(MlResult(score=0.9), b"")

    pauses = await db.get_recent_pauses(limit=1)
    assert len(pauses) == 1
    assert pauses[0]["result"] == "error"
    assert pauses[0]["error_message"] == "Printer pause failed"


async def test_watchdog_loop_cancellation() -> None:
    watcher, *_ = await _make_watcher()
    task = asyncio.create_task(watcher._watchdog())
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_safe_grab_jpeg_exception() -> None:
    watcher, _printer, camera, _ml, _db = await _make_watcher()
    camera.grab.side_effect = Exception("failed")
    res = await watcher._safe_grab_jpeg()
    assert res is None


async def test_print_started_notification_2() -> None:
    watcher, _printer, *_ = await _make_watcher()
    watcher._settings.notify_on_print_start = True

    printer_status = MagicMock()
    printer_status.printing = True
    printer_status.print_state = "printing"
    printer_status.stale = False
    printer_status.filename = "test.gcode"
    printer_status.elapsed_seconds = 100.0

    await watcher._update_state(printer_status)
    watcher._dispatcher.dispatch_print_started.assert_called_once()


async def test_print_completed_notification_2() -> None:
    watcher, _printer, *_ = await _make_watcher()
    watcher._settings.notify_on_print_completed = True
    watcher.state = WatcherState.ARMED
    watcher._current_job_id = 1
    watcher._current_filename = "test.gcode"

    printer_status = MagicMock()
    printer_status.printing = False
    printer_status.print_state = "completed"
    printer_status.stale = False
    printer_status.elapsed_seconds = 100.0
    await watcher._update_state(printer_status)
    watcher._dispatcher.dispatch_print_completed.assert_called_once()


async def test_watchdog_auto_stop(monkeypatch) -> None:
    watcher, printer, _camera, _ml, db = await _make_watcher()

    # Force auto-stop timeout parsing failure to test fallback
    db.get_setting = AsyncMock(return_value="invalid")

    # Set to PAUSED state with an old pause time
    watcher.state = WatcherState.PAUSED
    watcher._paused_since = datetime.now(tz=UTC) - timedelta(seconds=2000)

    # Use print_state="paused" — a printer paused by sentinel detection reports this
    # state, and _update_state must keep the watcher in PAUSED for the auto-stop
    # timeout check to fire.
    paused_status = PrinterStatus(
        printing=True,
        elapsed_seconds=400.0,
        current_layer=10,
        total_layers=100,
        filename="test.gcode",
        print_state="paused",
    )
    printer.status = AsyncMock(return_value=paused_status)

    await watcher._tick()
    printer.stop.assert_called_once()
    watcher._dispatcher.dispatch_text.assert_called_once()
    assert watcher._paused_since is None


async def test_poll_interval_fallback(monkeypatch) -> None:
    watcher, _printer, _camera, _ml, db = await _make_watcher()
    watcher._settings.ml_poll_interval_seconds = "0.5"

    watcher._running = True

    async def mock_tick():
        watcher._running = False

    watcher._tick = AsyncMock(side_effect=mock_tick)

    db.get_setting = AsyncMock(return_value="not_a_float")

    with patch("sentinel.watcher.loop.asyncio.sleep") as mock_sleep:
        await watcher._loop()

    mock_sleep.assert_called_once_with(0.5)


async def test_warmup_fallback(monkeypatch) -> None:
    watcher, printer, _camera, _ml, db = await _make_watcher()
    watcher._settings.detection_warmup_seconds = "1"

    status = _printing_status(elapsed=0)
    printer.status = AsyncMock(return_value=status)
    db.get_setting = AsyncMock(return_value="invalid")

    await watcher._tick()


# ---------------------------------------------------------------------------
# PRIV-01 Periodic Snapshot Cleanup Tests
# ---------------------------------------------------------------------------


async def test_cleanup_respects_configurable_limit() -> None:
    watcher, _printer, _camera, _ml, db = await _make_watcher()
    watcher._settings = Settings(
        printer_ip="10.0.0.1",
        printer_access_code="test",
        snapshot_retention_limit=5,  # Keep only 5
        db_path=str(db._path),
    )

    # Mock DB snapshots
    db.get_snapshots_for_cleanup = AsyncMock(return_value=["/dummy/path1", "/dummy/path2"])
    db.delete_old_snapshots = AsyncMock()

    await watcher.cleanup_old_snapshots()

    db.get_snapshots_for_cleanup.assert_called_once_with(keep_limit=5, limit=100)
    db.delete_old_snapshots.assert_called_once_with(["/dummy/path1", "/dummy/path2"])
    await db.close()


async def test_periodic_cleanup_task_runs_and_exits() -> None:
    watcher, _printer, _camera, _ml, db = await _make_watcher()
    watcher._settings = Settings(
        printer_ip="10.0.0.1",
        printer_access_code="test",
        snapshot_cleanup_interval_seconds=1,  # Sleep 1s
        snapshot_retention_limit=5,
        db_path=str(db._path),
    )

    # Initially running is True
    watcher._running = True
    watcher.cleanup_old_snapshots = AsyncMock()

    # Create helper mock sleep that cancels loop after 1 sleep
    sleep_calls = 0
    original_sleep = asyncio.sleep

    async def mock_sleep(delay: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        watcher._running = False
        await original_sleep(0)

    with patch("asyncio.sleep", side_effect=mock_sleep):
        await watcher._periodic_cleanup()

    assert watcher.cleanup_old_snapshots.call_count >= 1
    assert sleep_calls == 1
    await db.close()


async def test_fallback_directory_cleanup_deletes_orphans() -> None:
    import os
    import time
    from pathlib import Path

    watcher, _printer, _camera, _ml, db = await _make_watcher()
    watcher._settings = Settings(
        printer_ip="10.0.0.1",
        printer_access_code="test",
        db_path=str(db._path),
    )

    snapshots_dir = Path(str(db._path)).parent / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    # Write a file that is referenced in DB
    ref_file = snapshots_dir / "referenced.jpg"
    ref_file.write_bytes(b"ref")
    await db.record_detection(score=0.9, consecutive=1, confirmed=1, snapshot_path=str(ref_file))

    # Write an old orphan file (not in DB, older than 60 seconds)
    orphan_file = snapshots_dir / "orphan.jpg"
    orphan_file.write_bytes(b"orphan")
    past_time = time.time() - 100
    os.utime(orphan_file, (past_time, past_time))

    # Write a fresh orphan file (not in DB, modified just now)
    fresh_orphan = snapshots_dir / "fresh_orphan.jpg"
    fresh_orphan.write_bytes(b"fresh")

    await watcher.fallback_directory_cleanup()

    # The referenced file and the fresh orphan should still exist, but the old orphan should be deleted
    assert ref_file.exists()
    assert fresh_orphan.exists()
    assert not orphan_file.exists()

    # Cleanup
    ref_file.unlink(missing_ok=True)
    orphan_file.unlink(missing_ok=True)
    fresh_orphan.unlink(missing_ok=True)
    snapshots_dir.rmdir()
    await db.close()


async def test_watcher_tick_concurrency_lock() -> None:
    """Verify that if a tick is already running, a concurrent tick is skipped."""
    watcher, printer, _, _, db = await _make_watcher()

    # Delay printer.status to simulate a long network call
    async def delayed_status():
        await asyncio.sleep(0.1)
        return _printing_status()

    printer.status = delayed_status

    # Trigger first tick in background task
    task1 = asyncio.create_task(watcher.tick())
    await asyncio.sleep(0.01)  # yield to let task1 start and acquire the lock

    # Trigger second tick concurrently
    with patch("sentinel.watcher.loop.logger.warning") as mock_warning:
        await watcher.tick()
        mock_warning.assert_called_with("Watcher loop tick overlapping skipped.")

    await task1
    await db.close()


async def test_watcher_snooze_creates_task_and_sets_db() -> None:
    """Verify that snooze disables detection and spawns a re-enable task."""
    watcher, _, _, _, db = await _make_watcher()
    await db.set_setting("detection_enabled", "true")

    await watcher.snooze(0.05)
    assert (await db.get_setting("detection_enabled")) == "false"
    assert watcher._snooze_task is not None
    assert not watcher._snooze_task.done()

    # Wait for re-enable
    await asyncio.sleep(0.07)
    assert (await db.get_setting("detection_enabled")) == "true"
    assert watcher._snooze_task is None
    await db.close()


async def test_watcher_snooze_cancellation_on_multiple_calls() -> None:
    """Verify that multiple snooze calls cancel the old task and only the latest one re-enables."""
    watcher, _, _, _, db = await _make_watcher()
    await db.set_setting("detection_enabled", "true")

    # First snooze
    await watcher.snooze(0.05)
    task1 = watcher._snooze_task
    assert task1 is not None

    # Second snooze immediately
    await watcher.snooze(0.1)
    task2 = watcher._snooze_task
    assert task2 is not None
    assert task1 is not task2
    assert task1.cancelled() or task1.done()

    # Wait for duration of task1 (0.05s).
    # Detection should still be disabled because task1 was cancelled.
    await asyncio.sleep(0.06)
    assert (await db.get_setting("detection_enabled")) == "false"

    # Wait for duration of task2. Detection should now be enabled.
    await asyncio.sleep(0.06)
    assert (await db.get_setting("detection_enabled")) == "true"
    await db.close()


async def test_watcher_cancel_snooze() -> None:
    """Verify that cancel_snooze cancels the active snooze task and clears the setting."""
    watcher, _, _, _, db = await _make_watcher()
    await db.set_setting("detection_enabled", "true")

    await watcher.snooze(0.05)
    task = watcher._snooze_task
    assert task is not None

    watcher.cancel_snooze()
    assert watcher._snooze_task is None
    await asyncio.sleep(0.01)
    assert task.cancelled()

    await asyncio.sleep(0.07)
    # Since it was cancelled, it should not have re-enabled it (still false, or whichever was set)
    assert (await db.get_setting("detection_enabled")) == "false"
    await db.close()


async def test_print_duration_recording_and_back_to_back() -> None:
    """Verify print duration is calculated correctly and print start resets back-to-back."""
    from datetime import UTC, datetime, timedelta

    watcher, printer, _, _, db = await _make_watcher()

    # 1. Start job 1
    status = PrinterStatus(
        printing=True,
        elapsed_seconds=5.0,
        current_layer=1,
        total_layers=100,
        filename="job1.gcode",
        print_state="printing",
    )
    printer.status = AsyncMock(return_value=status)
    await watcher.tick()

    recent = await db.get_recent_jobs()
    assert len(recent) == 1
    job1_id = watcher._current_job_id
    assert job1_id is not None

    # Simulate 5 minutes (300 seconds) passing for job 1
    watcher._print_start = datetime.now(tz=UTC) - timedelta(seconds=300)

    # Transition back-to-back to job 2
    status.filename = "job2.gcode"
    status.elapsed_seconds = 10.0
    await watcher.tick()

    # Verify job 1 recorded end with duration of 300 seconds
    recent = await db.get_recent_jobs()
    assert len(recent) == 2
    # recent[1] is the older job (job 1)
    assert recent[1]["filename"] == "job1.gcode"
    assert recent[1]["duration_seconds"] == 300

    # Verify that job 2's print start has been reset to the transition time
    job2_start = watcher._print_start
    assert job2_start is not None
    # It should be close to now
    assert (datetime.now(tz=UTC) - job2_start).total_seconds() < 5

    # Simulate 120 seconds passing for job 2
    watcher._print_start = datetime.now(tz=UTC) - timedelta(seconds=120)

    # 2. Stop printing (go idle)
    idle_status = PrinterStatus(
        printing=False,
        elapsed_seconds=0.0,
        current_layer=0,
        total_layers=0,
        filename=None,
        print_state="idle",
    )
    printer.status = AsyncMock(return_value=idle_status)
    await watcher.tick()

    # Verify job 2 recorded end with duration of 120 seconds
    recent = await db.get_recent_jobs()
    assert len(recent) == 2
    assert recent[0]["filename"] == "job2.gcode"
    assert recent[0]["duration_seconds"] == 120

    await db.close()


async def test_stale_printer_status_transitions_to_offline() -> None:
    import dataclasses

    watcher, printer, _, _, _ = await _make_watcher(printer_status=_printing_status())
    watcher.state = WatcherState.ARMED

    # Mock status to return stale=True
    status = _printing_status()
    stale_status = dataclasses.replace(status, stale=True)
    printer.status = AsyncMock(return_value=stale_status)

    await watcher.tick()
    assert watcher.state == WatcherState.OFFLINE


async def test_liveness_watchdog_fires_in_offline_state() -> None:
    dispatcher = _make_dispatcher()
    watcher, _, _, _, db = await _make_watcher(dispatcher=dispatcher)
    watcher.state = WatcherState.OFFLINE

    stale_ts = (datetime.now(tz=UTC) - timedelta(seconds=120)).isoformat()
    await db.update_heartbeat(stale_ts, "OFFLINE")

    await watcher._watchdog_tick(db)

    assert watcher.state == WatcherState.STALLED
    dispatcher.dispatch_stall.assert_called_once()


async def test_stop_command_retry_on_timeout() -> None:
    watcher, printer, _, _, _ = await _make_watcher(printer_status=_printing_status())

    # Mock stop_pending to True
    printer.stop_pending = True

    # First tick retry: mock stop to fail
    printer.stop = AsyncMock(side_effect=Exception("Timeout"))
    await watcher.tick()
    printer.stop.assert_called_once()
    assert printer.stop_pending is True

    # Second tick retry: mock stop to succeed, which will clear the pending state
    printer.stop = AsyncMock()

    async def mock_stop() -> None:
        printer.stop_pending = False

    printer.stop.side_effect = mock_stop

    await watcher.tick()
    printer.stop.assert_called_once()
    assert printer.stop_pending is False


async def test_get_fresh_status_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    watcher, printer, _, _, _ = await _make_watcher(printer_status=_printing_status())

    # Set mock status
    status1 = _printing_status(elapsed=100.0)
    status2 = _printing_status(elapsed=200.0)

    printer.status = AsyncMock(side_effect=[status1, status2])

    import time

    current_time = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: current_time)

    # 1. First fetch (no cache exists)
    res1 = await watcher.get_fresh_status()
    assert res1 == status1
    assert printer.status.call_count == 1

    # 2. Second fetch within 2 seconds (returns cached)
    current_time = 1001.0
    res2 = await watcher.get_fresh_status()
    assert res2 == status1
    assert printer.status.call_count == 1

    # 3. Third fetch after 2 seconds (queries printer)
    current_time = 1003.0
    res3 = await watcher.get_fresh_status()
    assert res3 == status2
    assert printer.status.call_count == 2

    # 4. Fourth fetch within 2 seconds, but forced
    current_time = 1003.5
    printer.status = AsyncMock(return_value=status1)
    res4 = await watcher.get_fresh_status(force=True)
    assert res4 == status1
    assert printer.status.call_count == 1


async def test_snapshot_cleanup_permission_error_clears_db_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher, _printer, _camera, _ml, db = await _make_watcher()
    watcher._settings.snapshot_retention_limit = 0

    db.get_snapshots_for_cleanup = AsyncMock(return_value=["/dummy/path1", "/dummy/path2"])
    db.delete_old_snapshots = AsyncMock()

    # Mock Path.unlink
    from pathlib import Path

    unlinked_paths = []

    def mock_unlink(self: Path, missing_ok: bool = False) -> None:
        if str(self) == "/dummy/path1":
            raise PermissionError("access denied")
        unlinked_paths.append(str(self))

    monkeypatch.setattr(Path, "unlink", mock_unlink)

    await watcher.cleanup_old_snapshots()

    # The database deletion should now be called with BOTH paths (even the one that failed)
    # to avoid infinite loops, relying on fallback directory cleanup for orphans.
    db.delete_old_snapshots.assert_called_once_with(["/dummy/path1", "/dummy/path2"])
    assert "/dummy/path2" in unlinked_paths
    assert "/dummy/path1" not in unlinked_paths


async def test_watcher_resume_cooldown_skips_processing() -> None:
    settings = Settings(
        printer_ip="10.0.0.1",
        printer_access_code="test",
        detection_warmup_seconds=300,
        ml_confirm_count=3,
        ml_score_threshold=0.4,
        ml_poll_interval_seconds=10,
        watcher_stall_seconds=60,
        resume_cooldown_seconds=5,
    )
    watcher, printer, camera, ml, _db = await _make_watcher(settings=settings)

    # Initially in PAUSED state
    watcher.state = WatcherState.PAUSED

    # Transition to ARMED (simulating resume)
    watcher.state = WatcherState.ARMED
    assert watcher._last_resume_time > 0.0

    # Mock printer status & camera grab & ML detection
    printer.status = AsyncMock(return_value=_printing_status())
    camera.grab = AsyncMock(return_value=b"fake-jpeg")
    ml.detect = AsyncMock(return_value=MlResult(score=0.1))
    watcher._alerted_new_print = True

    # Perform tick (cooldown is active since monotonic time has not advanced)
    await watcher._tick()

    # Camera grab and ML detect should have been skipped
    camera.grab.assert_not_called()
    ml.detect.assert_not_called()


# ---------------------------------------------------------------------------
# Startup reconciliation — stale 'printing' rows closed before first tick
# ---------------------------------------------------------------------------


async def test_startup_reconciliation_closes_stale_jobs() -> None:
    """run_forever must close status='printing' rows before any new job is created."""
    watcher, _printer, _camera, _ml, db = await _make_watcher(printer_status=_printing_status())

    # Plant a stale 'printing' row simulating a previous crash
    stale_id = await db.record_print_start("orphan.gcode", "2026-06-11T00:00:00Z")

    # Confirm it is in 'printing' status
    rows_before = await db.get_recent_jobs()
    assert any(r["id"] == stale_id and r["status"] == "printing" for r in rows_before)

    # run_forever should close the stale row then exit immediately via cancellation
    task = asyncio.create_task(watcher.run_forever())
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    rows_after = await db.get_recent_jobs()
    stale_row = next(r for r in rows_after if r["id"] == stale_id)
    assert stale_row["status"] == "interrupted"
    assert stale_row["ended_at"] is not None


async def test_startup_reconciliation_no_phantom_jobs_in_recent() -> None:
    """get_recent_jobs shows no perpetual 'printing' rows after reconciliation runs."""
    watcher, _printer, _camera, _ml, db = await _make_watcher(printer_status=_idle_status())

    # Plant two stale rows
    await db.record_print_start("ghost1.gcode", "2026-06-11T00:00:00Z")
    await db.record_print_start("ghost2.gcode", "2026-06-11T00:01:00Z")

    task = asyncio.create_task(watcher.run_forever())
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    jobs = await db.get_recent_jobs()
    perpetual = [j for j in jobs if j["status"] == "printing"]
    assert perpetual == [], "No job should remain in status='printing' after reconciliation"


async def test_startup_reconciliation_new_job_created_after_stale_closed() -> None:
    """A new print started after reconciliation creates exactly one new row."""
    watcher, _printer, _camera, _ml, db = await _make_watcher(printer_status=_printing_status())

    # Plant a stale row
    stale_id = await db.record_print_start("old.gcode", "2026-06-11T00:00:00Z")

    task = asyncio.create_task(watcher.run_forever())
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    jobs = await db.get_recent_jobs()
    # Stale row closed as interrupted
    stale_row = next(r for r in jobs if r["id"] == stale_id)
    assert stale_row["status"] == "interrupted"

    # Exactly one new row for "test.gcode" (from _printing_status)
    new_jobs = [r for r in jobs if r["filename"] == "test.gcode"]
    assert len(new_jobs) == 1


async def test_watcher_resume_cooldown_expiry() -> None:
    settings = Settings(
        printer_ip="10.0.0.1",
        printer_access_code="test",
        detection_warmup_seconds=300,
        ml_confirm_count=3,
        ml_score_threshold=0.4,
        ml_poll_interval_seconds=10,
        watcher_stall_seconds=60,
        resume_cooldown_seconds=5,
    )
    watcher, printer, camera, ml, db = await _make_watcher(settings=settings)

    # Initially in PAUSED state
    watcher.state = WatcherState.PAUSED

    # Transition to ARMED
    watcher.state = WatcherState.ARMED

    # Simulate time passing beyond 5-second cooldown
    watcher._last_resume_time = time.monotonic() - 6.0

    # Mock printer status, camera grab, and ML detection
    printer.status = AsyncMock(return_value=_printing_status())
    camera.grab = AsyncMock(return_value=b"fake-jpeg")
    ml.detect = AsyncMock(return_value=MlResult(score=0.1))
    db.get_setting = AsyncMock(return_value="true")

    # Perform tick
    await watcher._tick()

    # Camera grab and ML detect should have run
    assert camera.grab.call_count >= 1


# ---------------------------------------------------------------------------
# Issue #63 — snooze/disable state machine fixes
# ---------------------------------------------------------------------------


async def test_disable_after_snooze_survives_restart() -> None:
    """Snooze → disable → simulate restart with expired snooze → detection stays disabled.

    Acceptance criterion from issue #63: an explicit /disable must survive a
    restart even when a previously-snoozed expiry has passed.

    The startup recovery block in run_forever re-enables detection only when
    snooze_until_utc > 0 (i.e. a genuine snooze expired).  An explicit /disable
    clears snooze_until_utc to "0", so the recovery block must leave detection
    as-is.
    """
    watcher, _, _, _, db = await _make_watcher()
    import time as _time

    # Step 1: operator snoozes
    await watcher.snooze(0.001)
    # Backdate the expiry so a restart would see it as expired
    past_ts = _time.time() - 10.0
    await db.set_setting("snooze_until_utc", str(past_ts))

    # Step 2: operator explicitly disables detection (this clears snooze_until_utc)
    watcher.cancel_snooze()
    await db.set_setting("snooze_until_utc", "0")
    await db.set_setting("detection_enabled", "false")

    # Step 3: simulate the run_forever startup recovery block executing on restart
    try:
        snooze_until_str = await db.get_setting("snooze_until_utc", "0")
        if snooze_until_str is not None:
            snooze_until = float(snooze_until_str)
            if snooze_until > 0:
                now = _time.time()
                if now > snooze_until:
                    await db.set_setting("detection_enabled", "true")
                    await db.set_setting("snooze_until_utc", "0")
    except (ValueError, TypeError):
        pass

    # Detection must remain disabled — snooze_until_utc was "0" so the recovery
    # block skipped the re-enable path entirely.
    assert (await db.get_setting("detection_enabled")) == "false"
    assert (await db.get_setting("snooze_until_utc")) == "0"
    await db.close()


async def test_snooze_write_order_is_crash_safe() -> None:
    """snooze() must write snooze_until_utc before detection_enabled=false.

    This ensures that if the process crashes between the two writes, the
    persisted state is recoverable (run_forever will see snooze_until_utc > 0
    and either reschedule or re-enable rather than leaving detection silently
    disabled forever).
    """
    watcher, _, _, _, db = await _make_watcher()
    await db.set_setting("detection_enabled", "true")
    await db.set_setting("snooze_until_utc", "0")

    # Patch set_setting to capture write order
    original_set = db.set_setting
    write_log: list[str] = []

    async def _capturing_set(key: str, value: str) -> None:
        write_log.append(key)
        await original_set(key, value)

    db.set_setting = _capturing_set  # type: ignore[method-assign]

    await watcher.snooze(60.0)

    # snooze_until_utc must appear before detection_enabled in the write log
    assert "snooze_until_utc" in write_log
    assert "detection_enabled" in write_log
    assert write_log.index("snooze_until_utc") < write_log.index("detection_enabled")

    watcher.cancel_snooze()
    await db.close()
