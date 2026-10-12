"""Run the real dashboard against a fake printer, for UI work without hardware.

    uv run python tools/dev_preview.py [--scenario printing|paused|idle|offline]
                                       [--auth] [--port 8765]

The app, routes, templates, auth middleware and SQLite repository are the real
ones. Only the printer, camera and watcher are fakes: the camera streams
tools/preview_assets/sample_frame.jpg and the printer reports a plausible job.
Pause/Resume/Stop change the fake's state so the controls can be exercised.

--auth enables login with the preview-only credentials below (never real ones).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import bcrypt
import uvicorn

from sentinel.config import Settings
from sentinel.db.repo import Database
from sentinel.ml.types import LastMlObservation
from sentinel.printer.types import PrinterStatus
from sentinel.watcher.state import WatcherState
from sentinel.web.app import create_app
from sentinel.web.sessions import SessionStore, credential_fingerprint

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

PREVIEW_USERNAME = "preview"
PREVIEW_PASSWORD = "preview-only"  # local preview server only

_FRAME = (Path(__file__).parent / "preview_assets" / "sample_frame.jpg").read_bytes()
_FILE = "Benchy_PLA_0.2mm_CC2.gcode"


def _status(scenario: str) -> PrinterStatus | None:
    if scenario == "offline":
        return None
    if scenario == "idle":
        return PrinterStatus(
            printing=False,
            elapsed_seconds=0,
            current_layer=0,
            total_layers=0,
            filename=None,
            extruder_temp=23.0,
            extruder_target=0,
            bed_temp=22.5,
            bed_target=0,
            print_state="idle",
            camera_connected=True,
        )
    return PrinterStatus(
        printing=scenario == "printing",
        elapsed_seconds=2843,
        current_layer=112,
        total_layers=240,
        filename=_FILE,
        extruder_temp=219.6,
        extruder_target=220,
        bed_temp=59.8,
        bed_target=60,
        progress=46.7,
        remaining_seconds=3260,
        print_state=scenario,
        camera_connected=True,
    )


class FakePrinter:
    def __init__(self, preview: Preview) -> None:
        self._preview = preview
        self.is_connected = True

    async def pause(self) -> None:
        self._preview.set_scenario("paused")

    async def resume(self) -> None:
        self._preview.set_scenario("printing")

    async def stop(self) -> None:
        self._preview.set_scenario("idle")

    async def status(self) -> PrinterStatus | None:
        return self._preview.status

    async def reconfigure(self, host: str) -> None:
        return None


class FakeCamera:
    is_connected = True

    async def grab(self) -> bytes:
        return _FRAME

    async def stream_proxy(self) -> AsyncIterator[bytes]:
        while True:
            yield _FRAME
            await asyncio.sleep(0.5)

    async def reconfigure(self, url: str) -> None:
        return None


class FakeDispatcher:
    def __init__(self) -> None:
        self.failed_channels: dict[str, str | None] = {}


class Preview:
    """The fake watcher: what routes.py reads from WatcherLoop."""

    def __init__(self, scenario: str) -> None:
        self.printer = FakePrinter(self)
        self.dispatcher = FakeDispatcher()
        self.last_ml_observation = LastMlObservation(score=0.18, ts=datetime.now(tz=UTC))
        self.set_scenario(scenario)

    def set_scenario(self, scenario: str) -> None:
        self.status = _status(scenario)
        self.last_printer_status = self.status
        self.state = {
            "printing": WatcherState.ARMED,
            "paused": WatcherState.PAUSED,
            "idle": WatcherState.IDLE,
            "offline": WatcherState.OFFLINE,
        }[scenario]

    async def get_fresh_status(self, force: bool = False) -> PrinterStatus | None:
        self.last_ml_observation = LastMlObservation(
            score=self.last_ml_observation.score, ts=datetime.now(tz=UTC)
        )
        return self.status

    async def external_transition(
        self, new_state: WatcherState, *, from_states: tuple[WatcherState, ...]
    ) -> bool:
        if self.state in from_states:
            self.state = new_state
            return True
        return False

    async def snooze(self, seconds: float) -> None:
        return None

    def note_operator_pause(self) -> None:
        return None

    def note_operator_resume(self) -> None:
        return None


async def _seed(db: Database) -> None:
    now = datetime.now(tz=UTC)
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    jobs = [
        ("Gearbox_housing_PETG.gcode", 26, 15120, "completed"),
        ("Cable_clip_x12_PLA.gcode", 21, 2880, "completed"),
        ("Vase_spiral_PLA.gcode", 18, 9540, "failed"),
        ("Hinge_test_PLA.gcode", 9, 1260, "completed"),
    ]
    for name, hours_ago, duration, status in jobs:
        start = now - timedelta(hours=hours_ago)
        job_id = await db.record_print_start(name, iso(start))
        await db.record_print_end(
            job_id, iso(start + timedelta(seconds=duration)), duration, 0.0, status
        )
    await db.record_print_start(_FILE, iso(now - timedelta(seconds=2843)))
    await db.record_detection(score=0.91, consecutive=3, confirmed=1)
    await db.record_detection(score=0.64, consecutive=1, confirmed=0)
    await db.record_pause(source="auto", result="ok")
    await db.record_pause(source="web", result="ok")
    await db.update_heartbeat(now.isoformat(), "ARMED")


async def _main(args: argparse.Namespace) -> None:
    tmp = tempfile.mkdtemp(prefix="sentinel-preview-")
    auth: dict[str, Any] = {}
    if args.auth:
        auth = {
            "auth_username": PREVIEW_USERNAME,
            "auth_password_bcrypt": bcrypt.hashpw(
                PREVIEW_PASSWORD.encode(), bcrypt.gensalt(rounds=4)
            ).decode(),
        }
    settings = Settings(
        printer_ip="192.168.1.50",
        printer_access_code="preview",
        db_path=os.path.join(tmp, "sentinel.db"),
        bind_host="127.0.0.1",
        bind_port=args.port,
        **auth,
    )
    db = Database(settings.db_path)
    await db.connect()
    await _seed(db)
    sessions = SessionStore(
        credential_fingerprint(settings.auth_username or "", settings.auth_password_bcrypt or ""),
        db,
    )
    app = create_app(
        settings, db=db, watcher=Preview(args.scenario), camera=FakeCamera(), sessions=sessions
    )

    async def _heartbeat() -> None:
        while True:
            await db.update_heartbeat(datetime.now(tz=UTC).isoformat(), "ARMED")
            await asyncio.sleep(5)

    beat = asyncio.create_task(_heartbeat())
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning")
    )
    try:
        await server.serve()
    finally:
        beat.cancel()
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--scenario", choices=("printing", "paused", "idle", "offline"), default="printing"
    )
    parser.add_argument("--auth", action="store_true", help="enable login (preview credentials)")
    parser.add_argument("--port", type=int, default=8765)
    asyncio.run(_main(parser.parse_args()))
