"""Tests for sentinel/db — migrate and repo, using in-memory SQLite."""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite
import pytest

from sentinel.db.migrate import CURRENT_VERSION, migrate
from sentinel.db.repo import Database

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[Database, None]:
    path = str(tmp_path / "test.db")
    await migrate(path)
    database = Database(path)
    await database.connect()
    yield database
    await database.close()


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


async def test_migrate_creates_tables(tmp_path: Path) -> None:
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


async def test_migrate_idempotent(tmp_path: Path) -> None:
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


async def test_migrate_version(tmp_path: Path) -> None:
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
    row_id = await db.record_detection(score=0.85, consecutive=1, confirmed=0)
    assert row_id == 1


async def test_record_detection_with_snapshot(db: Database) -> None:
    row_id = await db.record_detection(
        score=0.9,
        consecutive=3,
        confirmed=1,
        snapshot_path="/data/snapshots/abc123.jpg",
    )
    assert row_id >= 1


async def test_get_recent_detections_empty(db: Database) -> None:
    rows = await db.get_recent_detections()
    assert rows == []


async def test_get_recent_detections_order(db: Database) -> None:
    await db.record_detection(score=0.5, consecutive=1, confirmed=0)
    await db.record_detection(score=0.8, consecutive=2, confirmed=0)
    rows = await db.get_recent_detections()
    assert len(rows) == 2
    assert rows[0]["score"] == 0.8  # newest first


async def test_get_recent_detections_limit(db: Database) -> None:
    for i in range(5):
        await db.record_detection(score=float(i) / 10, consecutive=1, confirmed=0)
    rows = await db.get_recent_detections(limit=3)
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# Pause history
# ---------------------------------------------------------------------------


async def test_record_pause_returns_id(db: Database) -> None:
    pause_id = await db.record_pause(source="auto", result="ok")
    assert pause_id == 1


async def test_get_recent_pauses_empty(db: Database) -> None:
    rows = await db.get_recent_pauses()
    assert rows == []


async def test_get_recent_pauses_limit(db: Database) -> None:
    for _ in range(4):
        await db.record_pause(source="web", result="ok")
    rows = await db.get_recent_pauses(limit=2)
    assert len(rows) == 2


async def test_pause_metadata_stored(db: Database) -> None:
    await db.record_pause(source="telegram", result="error", error_message="timeout")
    rows = await db.get_recent_pauses()
    assert rows[0]["source"] == "telegram"
    assert rows[0]["result"] == "error"
    assert rows[0]["error_message"] == "timeout"


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
    hb = await db.get_heartbeat()
    assert hb is None


async def test_update_and_get_heartbeat(db: Database) -> None:
    await db.update_heartbeat("2026-05-23T12:00:00Z", "ARMED")
    hb = await db.get_heartbeat()
    assert hb is not None
    assert hb["last_tick_utc"] == "2026-05-23T12:00:00Z"
    assert hb["state"] == "ARMED"


async def test_update_heartbeat_upserts(db: Database) -> None:
    await db.update_heartbeat("2026-05-23T12:00:00Z", "ARMED")
    await db.update_heartbeat("2026-05-23T13:00:00Z", "IDLE")
    hb = await db.get_heartbeat()
    assert hb is not None
    assert hb["last_tick_utc"] == "2026-05-23T13:00:00Z"
    assert hb["state"] == "IDLE"


# ---------------------------------------------------------------------------
# Auth secret persistence
# ---------------------------------------------------------------------------


async def test_get_auth_secret_none_when_not_set(db: Database) -> None:
    result = await db.get_auth_secret()
    assert result is None


async def test_set_and_get_auth_secret(db: Database) -> None:
    secret = bytes(range(32))
    await db.set_auth_secret(secret)
    result = await db.get_auth_secret()
    assert result == secret


async def test_get_auth_secret_invalid_hex_returns_none(db: Database) -> None:
    await db.set_setting("auth_cookie_secret", "not_valid_hex!")
    result = await db.get_auth_secret()
    assert result is None


# ---------------------------------------------------------------------------
# Snapshot cleanup and deletion
# ---------------------------------------------------------------------------


async def test_snapshot_cleanup_and_deletion(db: Database) -> None:
    # Set up some detections with snapshot_paths
    for i in range(10):
        await db.record_detection(
            score=0.9,
            consecutive=1,
            confirmed=0,
            snapshot_path=f"/data/snapshots/snap_{i}.jpg",
        )

    # We want to keep 4. It should return oldest 6.
    old_snaps = await db.get_snapshots_for_cleanup(keep_limit=4)
    # The list is ordered by ID DESC, so newest first.
    # The newest 4 are snap_9, snap_8, snap_7, snap_6.
    # The old ones returned should be snap_5, snap_4, ..., snap_0 paths.
    assert len(old_snaps) == 6
    assert "/data/snapshots/snap_0.jpg" in old_snaps
    assert "/data/snapshots/snap_5.jpg" in old_snaps
    assert "/data/snapshots/snap_6.jpg" not in old_snaps

    # Let's delete them (clear snapshot_path fields)
    await db.delete_old_snapshots(old_snaps)

    # Get remaining snapshot_paths
    recent = await db.get_recent_detections(limit=10)
    for row in recent:
        if row["snapshot_path"]:
            assert row["snapshot_path"] in {
                "/data/snapshots/snap_6.jpg",
                "/data/snapshots/snap_7.jpg",
                "/data/snapshots/snap_8.jpg",
                "/data/snapshots/snap_9.jpg",
            }
        else:
            assert isinstance(row["id"], int) and row["id"] <= 6


# ---------------------------------------------------------------------------
# M11 — v1 → v2 migration
# ---------------------------------------------------------------------------


async def test_migrate_from_v1_schema(tmp_path: Path) -> None:
    """Starting from a v1 schema (schema_version=1) triggers the drop-and-rebuild path."""
    import aiosqlite

    path = str(tmp_path / "v1.db")

    # Build a minimal v1 schema manually
    async with aiosqlite.connect(path) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        await db.execute("CREATE TABLE IF NOT EXISTS detection_events (id INTEGER PRIMARY KEY)")
        await db.execute("CREATE TABLE IF NOT EXISTS pause_history (id INTEGER PRIMARY KEY)")
        await db.execute("CREATE TABLE IF NOT EXISTS runtime_settings (key TEXT PRIMARY KEY)")
        await db.execute("CREATE TABLE IF NOT EXISTS watcher_heartbeat (id INTEGER PRIMARY KEY)")
        await db.execute("INSERT INTO schema_version (version) VALUES (1)")
        await db.commit()

    # migrate() should detect v1 → drop → rebuild to CURRENT_VERSION
    await migrate(path)

    async with (
        aiosqlite.connect(path) as conn,
        conn.execute("SELECT MAX(version) FROM schema_version") as cur,
    ):
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == CURRENT_VERSION


# ---------------------------------------------------------------------------
# M5 — Database.ping()
# ---------------------------------------------------------------------------


async def test_ping_returns_true_on_live_connection(db: Database) -> None:
    assert await db.ping() is True


async def test_ping_returns_false_on_closed_connection(tmp_path: Path) -> None:
    path = str(tmp_path / "ping.db")
    await migrate(path)
    database = Database(path)
    await database.connect()
    await database.close()
    # Connection is closed — ping should return False
    result = await database.ping()
    assert result is False
