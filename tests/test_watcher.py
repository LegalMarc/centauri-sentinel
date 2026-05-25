"""Tests for sentinel/watcher/loop.py and state.py."""

from __future__ import annotations

import asyncio
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

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

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
    import tempfile

    from sentinel.db.migrate import migrate

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    await migrate(db_path)
    db = Database(db_path)
    await db.connect()

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
    await db.update_heartbeat(stale_ts)

    await watcher._watchdog_tick(db)

    assert watcher.state == WatcherState.STALLED
    notifier.send_stall_alert.assert_called_once()


async def test_watchdog_no_alert_when_fresh() -> None:
    notifier = _make_notifier()
    watcher, _, _, _, db = await _make_watcher(notifiers=[notifier])

    fresh_ts = datetime.now(tz=UTC).isoformat()
    await db.update_heartbeat(fresh_ts)

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
    await db.update_heartbeat(stale_ts)
    await watcher._watchdog_tick(db)  # must not raise
    assert watcher.state == WatcherState.STALLED


# ---------------------------------------------------------------------------
# _on_confirmed_detection — CancelledError sets PAUSED before re-raising
# ---------------------------------------------------------------------------


async def test_cancelled_during_pause_sets_paused_state() -> None:
    watcher, _, _, _, _ = await _make_watcher()

    async def _raise_cancelled() -> bool:
        raise asyncio.CancelledError

    watcher._printer = MagicMock()
    watcher._printer.pause = _raise_cancelled

    with pytest.raises(asyncio.CancelledError):
        await watcher._on_confirmed_detection(MlResult(score=0.9))

    assert watcher.state == WatcherState.PAUSED


# ---------------------------------------------------------------------------
# _watchdog — loop execution (covers the while-loop lines)
# ---------------------------------------------------------------------------


async def test_watchdog_loop_fires_on_stale_heartbeat() -> None:
    notifier = _make_notifier()
    watcher, _, _, _, db = await _make_watcher(settings=_FAST_SETTINGS, notifiers=[notifier])
    stale_ts = (datetime.now(tz=UTC) - timedelta(seconds=120)).isoformat()
    await db.update_heartbeat(stale_ts)

    watcher._running = True
    task = asyncio.create_task(watcher._watchdog())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    notifier.send_stall_alert.assert_called()
