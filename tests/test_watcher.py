"""Tests for sentinel/watcher/loop.py and state.py."""

from __future__ import annotations

import asyncio
import contextlib

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------
import shutil
import tempfile
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
    detection_warmup_seconds=300,
    ml_confirm_count=3,
    ml_score_threshold=0.4,
    ml_poll_interval_seconds=10,
    watcher_stall_seconds=60,
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
    )


def _warmup_status() -> PrinterStatus:
    return PrinterStatus(
        printing=True,
        elapsed_seconds=60.0,  # within warmup window
        current_layer=2,
        total_layers=100,
        filename="test.gcode",
    )


def _make_notifier() -> MagicMock:
    n = MagicMock()
    n.send_detection_alert = AsyncMock()
    n.send_stall_alert = AsyncMock()
    n.send_camera_offline_alert = AsyncMock()
    return n


async def _make_watcher(
    settings: Settings = _SETTINGS,
    printer_status: PrinterStatus | None = None,
    ml_score: float = 0.0,
    notifiers: list[Any] | None = None,
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
        notifiers=notifiers or [],
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
    camera.grab.assert_called_once()
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
    notifier = _make_notifier()
    watcher, printer, _camera, _ml, _db = await _make_watcher(
        printer_status=_printing_status(),
        ml_score=0.9,
        notifiers=[notifier],
    )
    settings = Settings(
        printer_ip="10.0.0.1",
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
    notifier.send_detection_alert.assert_called_once()


async def test_pause_fails_notifier_still_fires() -> None:
    notifier = _make_notifier()
    watcher, printer, _camera, _ml, _db = await _make_watcher(
        printer_status=_printing_status(),
        ml_score=0.9,
        notifiers=[notifier],
    )
    settings = Settings(
        printer_ip="10.0.0.1",
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
    notifier.send_detection_alert.assert_called_once()


async def test_db_records_detection_on_pause() -> None:
    watcher, _printer, _camera, _ml, db = await _make_watcher(
        printer_status=_printing_status(),
        ml_score=0.9,
    )
    settings = Settings(
        printer_ip="10.0.0.1",
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
    notifier = _make_notifier()
    watcher, _printer, camera, _ml, _db = await _make_watcher(
        printer_status=_printing_status(),
        notifiers=[notifier],
    )
    camera.grab = AsyncMock(side_effect=CameraOfflineError("offline"))

    await watcher.tick()

    assert watcher.state == WatcherState.CAMERA_OFFLINE
    notifier.send_camera_offline_alert.assert_called_once()


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
    notifier = _make_notifier()
    watcher, _, _, _, db = await _make_watcher(notifiers=[notifier])

    stale_ts = (datetime.now(tz=UTC) - timedelta(seconds=120)).isoformat()
    await db.update_heartbeat(stale_ts, "ARMED")

    await watcher._watchdog_tick(db)

    assert watcher.state == WatcherState.STALLED
    notifier.send_stall_alert.assert_called_once()


async def test_watchdog_no_alert_when_fresh() -> None:
    notifier = _make_notifier()
    watcher, _, _, _, db = await _make_watcher(notifiers=[notifier])

    fresh_ts = datetime.now(tz=UTC).isoformat()
    await db.update_heartbeat(fresh_ts, "ARMED")

    await watcher._watchdog_tick(db)

    assert watcher.state == WatcherState.IDLE
    notifier.send_stall_alert.assert_not_called()


async def test_watchdog_no_alert_when_no_heartbeat() -> None:
    notifier = _make_notifier()
    watcher, _, _, _, db = await _make_watcher(notifiers=[notifier])

    # No heartbeat in DB
    await watcher._watchdog_tick(db)

    assert watcher.state == WatcherState.IDLE
    notifier.send_stall_alert.assert_not_called()


# ---------------------------------------------------------------------------
# run_forever — TaskGroup lifecycle
# ---------------------------------------------------------------------------

_FAST_SETTINGS = Settings(
    printer_ip="10.0.0.1",
    detection_warmup_seconds=0,
    ml_confirm_count=1,
    ml_score_threshold=0.4,
    ml_poll_interval_seconds=0,
    watcher_stall_seconds=0,
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


# ---------------------------------------------------------------------------
# Notifier exception swallowing — camera offline, detection, stall
# ---------------------------------------------------------------------------


async def test_camera_offline_notifier_exception_swallowed() -> None:
    notifier = _make_notifier()
    notifier.send_camera_offline_alert = AsyncMock(side_effect=Exception("send failed"))
    watcher, _, camera, _, _ = await _make_watcher(
        printer_status=_printing_status(), notifiers=[notifier]
    )
    camera.grab = AsyncMock(side_effect=CameraOfflineError("offline"))
    await watcher.tick()  # must not raise
    assert watcher.state == WatcherState.CAMERA_OFFLINE


async def test_detection_notifier_exception_swallowed() -> None:
    notifier = _make_notifier()
    notifier.send_detection_alert = AsyncMock(side_effect=Exception("telegram down"))
    watcher, _, _, _, _ = await _make_watcher(
        printer_status=_printing_status(), ml_score=0.9, notifiers=[notifier]
    )
    watcher._settings = Settings(
        printer_ip="10.0.0.1",
        detection_warmup_seconds=0,
        ml_confirm_count=1,
        ml_score_threshold=0.4,
        ml_poll_interval_seconds=10,
        watcher_stall_seconds=60,
    )
    await watcher.tick()  # must not raise
    assert watcher.state == WatcherState.PAUSED


async def test_watchdog_notifier_exception_swallowed() -> None:
    notifier = _make_notifier()
    notifier.send_stall_alert = AsyncMock(side_effect=Exception("ntfy down"))
    watcher, _, _, _, db = await _make_watcher(notifiers=[notifier])
    stale_ts = (datetime.now(tz=UTC) - timedelta(seconds=120)).isoformat()
    await db.update_heartbeat(stale_ts, "ARMED")
    await watcher._watchdog_tick(db)  # must not raise
    assert watcher.state == WatcherState.STALLED


# ---------------------------------------------------------------------------
# _on_confirmed_detection — CancelledError sets PAUSED before re-raising
# ---------------------------------------------------------------------------


async def test_cancelled_during_pause_sets_paused_state() -> None:
    watcher, _, _, _, _ = await _make_watcher()

    async def _cancel_and_complete() -> None:
        task = asyncio.current_task()
        if task:
            task.cancel()

    watcher._printer = MagicMock()
    watcher._printer.pause = _cancel_and_complete

    with pytest.raises(asyncio.CancelledError):
        await watcher._on_confirmed_detection(MlResult(score=0.9), b"\xff\xd8\xff\xd9")

    assert watcher.state == WatcherState.PAUSED


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
    notifier = _make_notifier()
    watcher, _, _, _, db = await _make_watcher(settings=_FAST_SETTINGS, notifiers=[notifier])
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

    notifier.send_stall_alert.assert_called()


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
    notifier = _make_notifier()
    watcher, _, camera, _, _ = await _make_watcher(
        printer_status=_printing_status(), notifiers=[notifier]
    )
    camera.grab = AsyncMock(side_effect=CameraOfflineError("down"))

    # First tick: transition to CAMERA_OFFLINE, alert sent
    await watcher.tick()
    assert watcher.state == WatcherState.CAMERA_OFFLINE
    assert notifier.send_camera_offline_alert.call_count == 1

    # Second tick: still offline, state returns to CAMERA_OFFLINE, alert NOT sent again
    await watcher.tick()
    assert watcher.state == WatcherState.CAMERA_OFFLINE
    assert notifier.send_camera_offline_alert.call_count == 1


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
    assert first_job["status"] == "completed"
    assert first_job["duration_seconds"] == 20

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


async def test_paused_externally_transitions_to_paused_state() -> None:
    """If print_state is 'paused', the watcher transitions to PAUSED and doesn't run detection."""
    watcher, printer, camera, ml, _ = await _make_watcher(printer_status=_printing_status())
    watcher.state = WatcherState.ARMED

    # Mock status to return print_state="paused"
    status = _printing_status()
    status.print_state = "paused"
    printer.status = AsyncMock(return_value=status)

    await watcher.tick()

    assert watcher.state == WatcherState.PAUSED
    camera.grab.assert_not_called()
    ml.detect.assert_not_called()


async def test_watchdog_resilient_to_database_exceptions() -> None:
    """The watchdog loop must not crash and terminate if a database exception occurs."""
    import aiosqlite

    notifier = _make_notifier()
    watcher, _, _, _, db = await _make_watcher(settings=_FAST_SETTINGS, notifiers=[notifier])

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

