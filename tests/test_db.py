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


# (v1 migration removed)


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


# ---------------------------------------------------------------------------
# Print Jobs & Analytics
# ---------------------------------------------------------------------------


async def test_print_jobs_crud_and_analytics(db: Database) -> None:
    # 1. Record print start
    job_id = await db.record_print_start("benchy.gcode", "2026-05-26T23:00:00Z")
    assert job_id == 1

    # 2. Get recent jobs
    recent = await db.get_recent_jobs()
    assert len(recent) == 1
    assert recent[0]["filename"] == "benchy.gcode"
    assert recent[0]["status"] == "printing"
    assert recent[0]["pauses_count"] == 0

    # 3. Increment pauses
    await db.increment_job_pauses(job_id)
    recent = await db.get_recent_jobs()
    assert recent[0]["pauses_count"] == 1

    # 4. Record print end (completed)
    await db.record_print_end(job_id, "2026-05-26T23:10:00Z", 600, 15.5, "completed")
    recent = await db.get_recent_jobs()
    assert recent[0]["status"] == "completed"
    assert recent[0]["duration_seconds"] == 600
    assert recent[0]["filament_used_g"] == 15.5

    # 5. Record another job (failed)
    job_id2 = await db.record_print_start("spaghetti.gcode", "2026-05-26T23:15:00Z")
    await db.increment_job_pauses(job_id2)
    await db.increment_job_pauses(job_id2)
    await db.record_print_end(job_id2, "2026-05-26T23:20:00Z", 300, 5.0, "failed")

    # 6. Verify analytics summary
    summary = await db.get_analytics_summary()
    assert summary["total_prints"] == 2
    assert summary["success_rate_percent"] == 50.0
    assert summary["total_filament_g"] == 20.5
    assert summary["total_pauses"] == 3
    assert summary["avg_duration_seconds"] == 600.0


async def test_get_analytics_summary_uses_single_atomic_query(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The totals and avg_duration must come from one SELECT, not two sequential
    reads — two separate reads could straddle a concurrent write (e.g. a print
    job completing mid-request) and return a mismatched summary."""
    await db.record_print_start("benchy.gcode", "2026-05-26T23:00:00Z")
    await db.record_print_end(1, "2026-05-26T23:10:00Z", 600, 15.5, "completed")

    orig_execute = db._conn.execute
    query_count = 0

    def counting_execute(sql: str, *args: object, **kwargs: object) -> object:
        nonlocal query_count
        if "FROM print_jobs" in sql:
            query_count += 1
        return orig_execute(sql, *args, **kwargs)

    monkeypatch.setattr(db._conn, "execute", counting_execute)

    summary = await db.get_analytics_summary()

    assert query_count == 1, f"expected a single atomic query, got {query_count}"
    assert summary["total_prints"] == 1
    assert summary["avg_duration_seconds"] == 600.0


async def test_get_analytics_summary_avg_excludes_null_duration(db: Database) -> None:
    """A completed job with a NULL duration must not break/skew avg_duration_seconds,
    but must still be counted toward total_prints/success_rate_percent."""
    await db.record_print_start("a.gcode", "2026-05-26T23:00:00Z")
    await db.record_print_end(1, "2026-05-26T23:10:00Z", 600, 10.0, "completed")

    # Insert a second completed job with no duration recorded, bypassing
    # record_print_end (which always supplies one) to exercise the edge case.
    async with db._write(clear_analytics_cache=True) as conn:
        await conn.execute(
            "INSERT INTO print_jobs (filename, started_at, status, duration_seconds,"
            " filament_used_g, pauses_count) VALUES (?, ?, 'completed', NULL, 5.0, 0)",
            ("b.gcode", "2026-05-26T23:20:00Z"),
        )

    summary = await db.get_analytics_summary()
    assert summary["total_prints"] == 2
    assert summary["success_rate_percent"] == 100.0
    # avg must be computed only over the row with a non-NULL duration.
    assert summary["avg_duration_seconds"] == 600.0


async def test_write_exception_rolls_back(db: Database) -> None:
    try:
        async with db._write() as conn:
            await conn.execute(
                "INSERT INTO runtime_settings (key, value) VALUES ('test_rollback', 'val')"
            )
            # Invalid SQL to trigger an exception
            await conn.execute("INSERT INTO non_existent_table (foo) VALUES ('bar')")
    except Exception:
        pass

    val = await db.get_setting("test_rollback")
    assert val is None


async def test_db_checkpoint(db: Database) -> None:
    # Verify checkpoint method runs successfully
    await db.checkpoint()


async def test_db_busy_timeout_configured(db: Database) -> None:
    # Verify busy_timeout is set to 30000 ms
    async with db._db.execute("PRAGMA busy_timeout") as cur:
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == 30000


async def test_db_concurrency_heartbeats(db: Database) -> None:
    # Concurrent write/read operations simulating high-frequency heartbeats
    # and concurrent queries. All operations must complete without locking exceptions.
    import random

    async def run_heartbeats() -> None:
        for i in range(50):
            await db.update_heartbeat(f"2026-06-03T12:00:{i:02d}Z", "ARMED")
            await asyncio.sleep(0.001)

    async def run_writes() -> None:
        for i in range(30):
            await db.record_detection(score=random.random(), consecutive=i, confirmed=0)
            await db.set_setting(f"key_{i}", f"val_{i}")
            await asyncio.sleep(0.001)

    async def run_reads() -> None:
        for _ in range(40):
            await db.get_recent_detections(limit=10)
            await db.get_setting("key_1")
            await asyncio.sleep(0.001)

    import asyncio

    await asyncio.gather(
        run_heartbeats(),
        run_writes(),
        run_reads(),
    )


async def test_explain_query_plan_indices(db: Database) -> None:
    # Verify that status / confirmed / result filters utilize indices instead of scanning tables.

    # 1. print_jobs (status)
    async with db._db.execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM print_jobs WHERE status = 'completed'"
    ) as cur:
        rows = await cur.fetchall()
        details = [row["detail"] for row in rows]
        assert any("idx_print_jobs_status" in detail for detail in details)

    # 2. detection_events (confirmed)
    async with db._db.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM detection_events WHERE confirmed = 1"
    ) as cur:
        rows = await cur.fetchall()
        details = [row["detail"] for row in rows]
        assert any("idx_detection_events_confirmed" in detail for detail in details)

    # 3. pause_history (result)
    async with db._db.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM pause_history WHERE result = 'ok'"
    ) as cur:
        rows = await cur.fetchall()
        details = [row["detail"] for row in rows]
        assert any("idx_pause_history_result" in detail for detail in details)


# ---------------------------------------------------------------------------
# Migration error handling / sync tests
# ---------------------------------------------------------------------------


def test_migrate_sync(tmp_path: Path) -> None:
    from sentinel.db.migrate import migrate_sync

    path = str(tmp_path / "sync.db")
    migrate_sync(path)

    import os

    assert os.path.exists(path)


# (drop test removed)


async def test_migrate_executescript_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure mid-schema (injected via a bad SQL statement) must propagate
    as an exception and leave no partial tables committed."""
    import sentinel.db.migrate as migrate_mod

    path = str(tmp_path / "script_error.db")

    original_split = migrate_mod._split_sql

    def patched_split(sql: str) -> list[str]:
        stmts = original_split(sql)
        # Inject a guaranteed-to-fail statement after the first real statement.
        stmts.insert(1, "SELECT * FROM __nonexistent_table_xyz__")
        return stmts

    monkeypatch.setattr(migrate_mod, "_split_sql", patched_split)

    with pytest.raises(aiosqlite.OperationalError):
        await migrate(path)


async def test_migrate_atomicity_mid_schema_no_partial_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure mid-schema must leave no schema_version row and no partial
    tables — the transaction must be fully rolled back.

    We inject a failing SQL statement between the first and second real schema
    statements.  Because all schema DDL now runs inside a single explicit
    transaction, the rollback must undo the first CREATE TABLE as well.
    """
    import sentinel.db.migrate as migrate_mod

    path = str(tmp_path / "atomic.db")

    original_split = migrate_mod._split_sql

    def patched_split(sql: str) -> list[str]:
        stmts = original_split(sql)
        # Inject a bad statement between the first CREATE TABLE (schema_version)
        # and the second (detection_events), simulating a mid-migration crash.
        stmts.insert(1, "INSERT INTO __no_such_table__ (x) VALUES (1)")
        return stmts

    monkeypatch.setattr(migrate_mod, "_split_sql", patched_split)

    with pytest.raises(aiosqlite.OperationalError):
        await migrate(path)

    # Restore the original split so we can inspect the DB cleanly.
    monkeypatch.setattr(migrate_mod, "_split_sql", original_split)

    # The transaction must have been rolled back — no schema_version table.
    async with (
        aiosqlite.connect(path) as conn,
        conn.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur,
    ):
        tables = {row[0] for row in await cur.fetchall()}

    assert "schema_version" not in tables, (
        f"schema_version must not exist after a rolled-back migration; found tables: {tables}"
    )


async def test_v1_rename_atomicity_crash_after_first_rename(
    tmp_path: Path,
) -> None:
    """A crash after the first v1 table rename (but before schema_version is
    renamed) must leave the DB re-migratable.

    We simulate the crash by directly constructing the interrupted state:
      - detection_events has already been renamed to detection_events_v1
      - schema_version still exists with version=1 (it would have been renamed
        LAST, so it survives the crash / rollback)
    This is the exact state left by a process crash mid-v1-sequence when
    schema_version is renamed last and the rename sequence is transactional
    (crash → rollback → detection_events_v1 renamed back; but here we test
    the OLD non-transactional case that the fix defends against by making
    schema_version last).

    With the new transactional approach the whole sequence rolls back on crash,
    so schema_version always survives and version=1 is still readable.  We
    test both the re-enterable state and successful completion.
    """
    path = str(tmp_path / "v1_crash.db")

    # Build a database that represents the interrupted mid-v1-rename state:
    # detection_events has been renamed but schema_version has NOT yet been
    # renamed (schema_version is renamed LAST in the new code).
    interrupted_stmts = [
        # schema_version still intact — version=1 is readable
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))",
        # detection_events already renamed
        "CREATE TABLE detection_events_v1 (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, score REAL NOT NULL, consecutive INTEGER NOT NULL, confirmed INTEGER NOT NULL, snapshot_path TEXT)",
        # other tables still at original names
        "CREATE TABLE pause_history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, source TEXT NOT NULL, result TEXT NOT NULL, error_message TEXT)",
        "CREATE TABLE runtime_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))",
        "CREATE TABLE watcher_heartbeat (id INTEGER PRIMARY KEY CHECK (id = 1), last_tick_utc TEXT NOT NULL, state TEXT NOT NULL)",
        "INSERT INTO schema_version (version) VALUES (1)",
        # data row in the already-renamed table
        "INSERT INTO detection_events_v1 (ts_utc, score, consecutive, confirmed) VALUES ('2026-01-01T00:00:00Z', 0.9, 1, 1)",
    ]
    async with aiosqlite.connect(path) as conn:
        for stmt in interrupted_stmts:
            await conn.execute(stmt)
        await conn.commit()

    # migrate() must recover from this state and complete successfully.
    # (detection_events rename will fail silently because detection_events_v1
    # already exists; the others will succeed; schema_version renamed last.)
    await migrate(path)

    # The full schema must now exist.
    async with (
        aiosqlite.connect(path) as conn,
        conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name") as cur,
    ):
        tables = {row[0] for row in await cur.fetchall()}

    expected_new = {
        "schema_version",
        "detection_events",
        "pause_history",
        "runtime_settings",
        "watcher_heartbeat",
    }
    assert expected_new <= tables, f"Expected new schema tables; got {tables}"

    # The _v1 data tables must still be present (data preserved).
    assert "detection_events_v1" in tables, (
        f"detection_events_v1 must be preserved after successful v1 migration; got {tables}"
    )

    # The data row must still be accessible.
    async with (
        aiosqlite.connect(path) as conn,
        conn.execute("SELECT COUNT(*) FROM detection_events_v1") as cur,
    ):
        row = await cur.fetchone()
    assert row is not None and row[0] == 1, "Data row in detection_events_v1 must be preserved"


async def test_repo_additional_coverage(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. checkpoint exception path
    orig_execute = db._conn.execute

    def mock_execute_pragma(sql: str, *args: object, **kwargs: object) -> object:
        if "wal_checkpoint" in sql:
            raise RuntimeError("checkpoint fail")
        return orig_execute(sql, *args, **kwargs)

    monkeypatch.setattr(db._conn, "execute", mock_execute_pragma)
    # This should log warning but not raise
    await db.checkpoint()

    # Reset connection execute
    monkeypatch.setattr(db._conn, "execute", orig_execute)

    # 2. delete_old_snapshots with empty list
    await db.delete_old_snapshots([])  # should return immediately

    # 3. clear_all_data
    # Let's populate some data first
    await db.record_detection(score=0.9, consecutive=1, confirmed=0)
    await db.record_pause(source="web", result="ok")
    await db.record_print_start("benchy.gcode", "2026-05-26T23:00:00Z")

    counts = await db.clear_all_data()
    assert counts["detections"] == 1
    assert counts["pauses"] == 1
    assert counts["jobs"] == 1

    # 4. prune_old_events with retention_days <= 0
    p_zero = await db.prune_old_events(retention_days=0)
    assert p_zero == {"detections": 0, "pauses": 0, "jobs": 0}

    # prune_old_events with retention_days > 0
    # Let's record a detection event manually with old timestamp
    # Note: we need to use write block to set back ts_utc
    async with db._write() as conn:
        await conn.execute(
            "INSERT INTO detection_events (ts_utc, score, consecutive, confirmed)"
            " VALUES (strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-5 days'), 0.8, 1, 0)"
        )
        await conn.execute(
            "INSERT INTO pause_history (ts_utc, source, result)"
            " VALUES (strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-5 days'), 'auto', 'ok')"
        )
        await conn.execute(
            "INSERT INTO print_jobs (started_at, filename, status)"
            " VALUES (strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-5 days'), 'foo.gcode', 'completed')"
        )

    # Prune rows older than 2 days
    pruned = await db.prune_old_events(retention_days=2)
    assert pruned["detections"] == 1
    assert pruned["pauses"] == 1
    assert pruned["jobs"] == 1

    # 5. get_analytics_summary cached hit
    # Call it once to populate cache
    await db.get_analytics_summary()
    assert db._analytics_cache is not None
    # Call it second time to hit cache
    cached_summary = await db.get_analytics_summary()
    assert cached_summary["total_prints"] == 0


# ---------------------------------------------------------------------------
# close_stale_jobs — startup reconciliation
# ---------------------------------------------------------------------------


async def test_close_stale_jobs_marks_interrupted(db: Database) -> None:
    """All status='printing' rows must be closed as 'interrupted' with ended_at set."""
    # Two in-progress rows, one already completed
    id1 = await db.record_print_start("a.gcode", "2026-06-11T00:00:00Z")
    id2 = await db.record_print_start("b.gcode", "2026-06-11T00:01:00Z")
    await db.record_print_end(
        await db.record_print_start("c.gcode", "2026-06-11T00:02:00Z"),
        "2026-06-11T00:10:00Z",
        600,
        0.0,
        "completed",
    )

    ended_at = "2026-06-11T01:00:00Z"
    closed = await db.close_stale_jobs(ended_at)
    assert closed == 2

    rows = await db.get_recent_jobs()
    by_id = {r["id"]: r for r in rows}

    assert by_id[id1]["status"] == "interrupted"
    assert by_id[id1]["ended_at"] == ended_at
    assert by_id[id2]["status"] == "interrupted"
    assert by_id[id2]["ended_at"] == ended_at
    # The completed row must be untouched
    completed = [r for r in rows if r["status"] == "completed"]
    assert len(completed) == 1


async def test_close_stale_jobs_returns_zero_when_none(db: Database) -> None:
    """close_stale_jobs returns 0 when there are no stale rows."""
    closed = await db.close_stale_jobs("2026-06-11T01:00:00Z")
    assert closed == 0


async def test_close_stale_jobs_does_not_affect_non_printing(db: Database) -> None:
    """Only status='printing' rows are updated; failed/completed/interrupted are untouched."""
    job_id = await db.record_print_start("x.gcode", "2026-06-11T00:00:00Z")
    await db.record_print_end(job_id, "2026-06-11T00:10:00Z", 600, 0.0, "failed")

    closed = await db.close_stale_jobs("2026-06-11T01:00:00Z")
    assert closed == 0

    rows = await db.get_recent_jobs()
    assert rows[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# Read isolation: readers must not see uncommitted writes
# ---------------------------------------------------------------------------


async def test_read_does_not_see_uncommitted_write(db: Database) -> None:
    """A concurrent read must not observe a row that has been executed but not committed.

    We simulate an in-progress write by holding the writer lock manually (via
    _write), suspending inside it, and then performing a read from a second
    coroutine.  Because both reads and writes now acquire the same lock, the
    reader is blocked until the writer rolls back — it never sees the dirty row.

    The writer deliberately rolls back (never fires allow_commit), so the only
    committed state is "row absent".  If the lock were bypassed, the reader
    would complete during the writer's open transaction and return the dirty
    value 'dirty_sentinel'; with the lock it returns None (row absent after
    rollback).  This makes a dirty read detectable.
    """
    import asyncio

    write_started = asyncio.Event()
    allow_rollback = asyncio.Event()

    async def slow_writer() -> None:
        try:
            async with db._write() as conn:
                await conn.execute(
                    "INSERT INTO runtime_settings (key, value)"
                    " VALUES ('isolation_test', 'dirty_sentinel')"
                )
                write_started.set()
                # Pause here — transaction is open, row exists but not committed.
                # We will roll back by raising after allow_rollback fires.
                await allow_rollback.wait()
                raise RuntimeError("deliberate rollback")
        except RuntimeError:
            pass  # expected — ensures the transaction is rolled back

    async def concurrent_reader() -> str | None:
        # Wait until the writer has executed (but not committed) before reading.
        await write_started.wait()
        # This read must block on the lock until the writer releases it.
        return await db.get_setting("isolation_test")

    writer_task = asyncio.create_task(slow_writer())
    reader_task = asyncio.create_task(concurrent_reader())

    # Give both tasks real scheduling time so that a reader NOT blocked by a
    # lock would have enough time to complete its aiosqlite worker round-trip.
    await asyncio.sleep(0.05)

    # The reader must still be pending — blocked on the lock.
    # If get_setting bypassed _read() (no lock), it could have completed already.
    assert not reader_task.done(), (
        "Reader completed before the writer released the lock — "
        "dirty read possible (lock not held during read)"
    )

    # Release the writer so it rolls back, which frees the lock.
    allow_rollback.set()
    await writer_task

    # Now the reader can proceed; the transaction was rolled back so the row is
    # absent — a dirty read would have returned 'dirty_sentinel'.
    result = await reader_task
    assert result is None, (
        f"Expected None (row absent after rollback) but got {result!r} — "
        "reader may have seen uncommitted data"
    )


# ---------------------------------------------------------------------------
# CancelledError in _write leaves no pending statements
# ---------------------------------------------------------------------------


async def test_cancelled_write_rolls_back(db: Database) -> None:
    """asyncio.CancelledError inside _write must trigger rollback so the next
    write starts with a clean connection and does not accidentally commit the
    cancelled write's statements.
    """
    import asyncio
    import contextlib

    inside_write = asyncio.Event()

    async def write_that_gets_cancelled() -> None:
        async with db._write() as conn:
            await conn.execute(
                "INSERT INTO runtime_settings (key, value) VALUES ('cancel_test', 'should_not_persist')"
            )
            inside_write.set()
            # Yield control so the cancellation can land.
            await asyncio.sleep(10)  # Will be cancelled here.

    task = asyncio.create_task(write_that_gets_cancelled())
    await inside_write.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # After cancellation the transaction must have been rolled back.
    # A subsequent read must not find the cancelled insert.
    val = await db.get_setting("cancel_test")
    assert val is None, f"Cancelled write must not persist; got {val!r}"

    # The connection must be usable for a fresh write.
    await db.set_setting("after_cancel", "ok")
    result = await db.get_setting("after_cancel")
    assert result == "ok"
