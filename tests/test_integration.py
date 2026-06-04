import contextlib
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from sentinel.config import Settings
from sentinel.db.repo import Database
from sentinel.ml.types import MlResult
from sentinel.notify.dispatcher import NotificationDispatcher
from sentinel.printer.types import PrinterStatus
from sentinel.watcher.loop import WatcherLoop
from sentinel.watcher.state import WatcherState


@pytest.fixture
async def setup_integration_env():
    # Setup temp DB
    from sentinel.db.migrate import migrate

    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "integration_test.db")
    await migrate(db_path)
    db = Database(db_path)
    await db.connect()

    # Settings
    settings = Settings(
        printer_ip="192.168.1.10",
        ml_confirm_count=2,
        ml_score_threshold=0.5,
        ml_poll_interval_seconds=1,
        detection_warmup_seconds=0,
        db_path=db_path,
    )

    # Subsystem Mocks
    printer = MagicMock()
    # Initial status is printing
    p_status = PrinterStatus(
        printing=True,
        elapsed_seconds=120.0,
        current_layer=10,
        total_layers=100,
        filename="integration_test.gcode",
        print_state="printing",
    )
    printer.status = AsyncMock(return_value=p_status)
    printer.pause = AsyncMock(return_value=True)

    camera = MagicMock()
    camera.grab = AsyncMock(return_value=b"fake_jpeg_data")

    ml = MagicMock()
    # Initially returns high score (failure detected)
    ml.detect = AsyncMock(return_value=MlResult(score=0.8))

    # Mocked notifier
    notifier = MagicMock()
    notifier.send_detection_alert = AsyncMock()
    dispatcher = NotificationDispatcher([notifier])

    watcher = WatcherLoop(
        settings=settings,
        printer=printer,
        camera=camera,
        ml=ml,
        db=db,
        dispatcher=dispatcher,
    )

    yield watcher, printer, camera, ml, db, notifier

    await db.close()
    # Clean up temp files
    with contextlib.suppress(OSError):
        os.remove(db_path)
        os.rmdir(temp_dir)


async def test_full_detection_to_pause_integration(setup_integration_env) -> None:
    watcher, printer, _camera, _ml, db, notifier = setup_integration_env

    # 1. Start state is IDLE
    assert watcher.state == WatcherState.IDLE

    # 2. First tick transitions to ARMED (warmup=0)
    await watcher.tick()
    assert watcher.state == WatcherState.ARMED
    assert watcher._confirm_count == 1
    # pause should not be called yet because ml_confirm_count is 2
    printer.pause.assert_not_called()
    notifier.send_detection_alert.assert_not_called()

    # 3. Second tick: consecutive detection count reaches 2
    await watcher.tick()
    # State should now be PAUSED, and confirmation counter is reset to 0
    assert watcher.state == WatcherState.PAUSED
    assert watcher._confirm_count == 0

    # Verification: Printer pause must be called
    printer.pause.assert_called_once()

    # Verification: Notifiers must be triggered
    # Notification dispatcher dispatches detection warning
    notifier.send_detection_alert.assert_called_once()

    # Verification: Database records
    detections = await db.get_recent_detections(limit=10)
    assert len(detections) >= 1
    # Check that confirmed is 1
    assert detections[0]["confirmed"] == 1
    assert detections[0]["score"] == 0.8

    pauses = await db.get_recent_pauses(limit=10)
    assert len(pauses) == 1
    assert pauses[0]["result"] == "ok"
