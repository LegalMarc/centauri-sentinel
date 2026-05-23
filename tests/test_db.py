"""Tests for sentinel/db — migrate and repo, using in-memory SQLite."""

from __future__ import annotations

import aiosqlite
import pytest

from sentinel.db.migrate import CURRENT_VERSION, migrate
from sentinel.db.repo import Database

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path: pytest.TempPathFactory) -> Database:  # type: ignore[type-arg]
    path = str(tmp_path / "test.db")
    await migrate(path)
    database = Database(path)
    await database.connect()
    yield database  # type: ignore[misc]
    await database.close()


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


async def test_migrate_creates_tables(tmp_path: pytest.TempPathFactory) -> None:  # type: ignore[type-arg]
    path = str(tmp_path / "m.db")
    await migrate(path)

    expected = {
        "detection_events",
        "pause_history",
        "runtime_settings",
        "schema_version",
        "watcher_heartbeat",
    }
    async with (
        aiosqlite.connect(path) as conn,
        conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name") as cur,
    ):
        tables = {row[0] for row in await cur.fetchall()}
    assert expected <= tables


async def test_migrate_idempotent(tmp_path: pytest.TempPathFactory) -> None:  # type: ignore[type-arg]
    path = str(tmp_path / "m.db")
    await migrate(path)
    await migrate(path)  # second call must not raise

    async with (
        aiosqlite.connect(path) as conn,
        conn.execute("SELECT COUNT(*) FROM schema_version") as cur,
    ):
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == 1


async def test_migrate_version(tmp_path: pytest.TempPathFactory) -> None:  # type: ignore[type-arg]
    path = str(tmp_path / "m.db")
    await migrate(path)

    async with (
        aiosqlite.connect(path) as conn,
        conn.execute("SELECT MAX(version) FROM schema_version") as cur,
    ):
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == CURRENT_VERSION


# ---------------------------------------------------------------------------
# Detection events
# ---------------------------------------------------------------------------


async def test_record_detection_returns_id(db: Database) -> None:
    row_id = await db.record_detection(score=0.85)
    assert row_id == 1


async def test_record_detection_with_snapshot(db: Database) -> None:
    row_id = await db.record_detection(score=0.9, snapshot_id="abc123", action="paused")
    assert row_id >= 1


async def test_get_recent_detections_empty(db: Database) -> None:
    rows = await db.get_recent_detections()
    assert rows == []


async def test_get_recent_detections_order(db: Database) -> None:
    await db.record_detection(score=0.5)
    await db.record_detection(score=0.8)
    rows = await db.get_recent_detections()
    assert len(rows) == 2
    assert rows[0]["score"] == 0.8  # newest first


async def test_get_recent_detections_limit(db: Database) -> None:
    for i in range(5):
        await db.record_detection(score=float(i) / 10)
    rows = await db.get_recent_detections(limit=3)
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# Pause history
# ---------------------------------------------------------------------------


async def test_record_pause_returns_id(db: Database) -> None:
    pause_id = await db.record_pause()
    assert pause_id == 1


async def test_end_pause_sets_ended_at(db: Database) -> None:
    pause_id = await db.record_pause()
    await db.end_pause(pause_id)
    rows = await db.get_recent_pauses()
    assert rows[0]["ended_at"] is not None


async def test_end_pause_idempotent(db: Database) -> None:
    pause_id = await db.record_pause()
    await db.end_pause(pause_id)
    await db.end_pause(pause_id)  # must not raise
    rows = await db.get_recent_pauses()
    assert rows[0]["ended_at"] is not None


async def test_get_recent_pauses_empty(db: Database) -> None:
    rows = await db.get_recent_pauses()
    assert rows == []


async def test_get_recent_pauses_limit(db: Database) -> None:
    for _ in range(4):
        await db.record_pause()
    rows = await db.get_recent_pauses(limit=2)
    assert len(rows) == 2


async def test_pause_reason_stored(db: Database) -> None:
    await db.record_pause(reason="manual")
    rows = await db.get_recent_pauses()
    assert rows[0]["reason"] == "manual"


# ---------------------------------------------------------------------------
# Runtime settings
# ---------------------------------------------------------------------------


async def test_get_setting_missing_returns_default(db: Database) -> None:
    val = await db.get_setting("no_such_key", default="fallback")
    assert val == "fallback"


async def test_get_setting_missing_no_default(db: Database) -> None:
    val = await db.get_setting("no_such_key")
    assert val is None


async def test_set_and_get_setting(db: Database) -> None:
    await db.set_setting("detection_enabled", "true")
    val = await db.get_setting("detection_enabled")
    assert val == "true"


async def test_set_setting_upsert(db: Database) -> None:
    await db.set_setting("foo", "bar")
    await db.set_setting("foo", "baz")
    val = await db.get_setting("foo")
    assert val == "baz"


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


async def test_get_heartbeat_none_initially(db: Database) -> None:
    ts = await db.get_heartbeat()
    assert ts is None


async def test_update_and_get_heartbeat(db: Database) -> None:
    await db.update_heartbeat("2026-05-23T12:00:00Z")
    ts = await db.get_heartbeat()
    assert ts == "2026-05-23T12:00:00Z"


async def test_update_heartbeat_upserts(db: Database) -> None:
    await db.update_heartbeat("2026-05-23T12:00:00Z")
    await db.update_heartbeat("2026-05-23T13:00:00Z")
    ts = await db.get_heartbeat()
    assert ts == "2026-05-23T13:00:00Z"
