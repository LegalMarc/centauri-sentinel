"""The no-hardware preview server (tools/dev_preview.py) behaves like the real
watcher where the dashboard can see the difference."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from sentinel.config import Settings
from sentinel.db.repo import Database
from sentinel.web.app import create_app

if TYPE_CHECKING:
    from types import ModuleType

_PREVIEW_PATH = Path(__file__).resolve().parent.parent / "tools" / "dev_preview.py"


def _load_preview() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dev_preview", _PREVIEW_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["dev_preview"] = module
    spec.loader.exec_module(module)
    return module


async def test_preview_snooze_shows_on_dashboard(tmp_path: Path) -> None:
    """Snooze in the preview must reach /api/printer as it does in production,
    or the "Snoozed to HH:MM" chip cannot be checked without hardware."""
    preview = _load_preview()
    settings = Settings(
        printer_ip="192.168.1.50",
        printer_access_code="preview",
        db_path=str(tmp_path / "sentinel.db"),
        bind_host="127.0.0.1",
    )
    db = Database(settings.db_path)
    await db.connect()
    try:
        app = create_app(
            settings,
            db=db,
            watcher=preview.Preview("printing", db),
            camera=preview.FakeCamera(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
            base_url="http://test",
            headers={"Host": "test", "Origin": "http://test"},
        ) as c:
            before = (await c.get("/api/printer")).json()
            r = await c.post("/api/control/snooze", json={"seconds": 600})
            after = (await c.get("/api/printer")).json()
    finally:
        await db.close()

    assert before["detection_enabled"] is True
    assert before["snooze_until"] is None
    assert r.status_code == 200
    assert after["detection_enabled"] is False
    assert after["snooze_until"] is not None
