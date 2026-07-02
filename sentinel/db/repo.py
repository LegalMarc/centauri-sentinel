"""Async repository layer — all DB reads and writes go through a single asyncio.Lock."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)


class Database:
    """Wrapper around an aiosqlite connection with a serialising lock for all operations.

    A single asyncio.Lock serialises both reads and writes so that readers never
    observe uncommitted (dirty) data from concurrent writers on the same connection.
    aiosqlite serialises individual statements per connection, but that does not
    prevent a read from landing between another task's execute and commit, which
    would expose not-yet-committed rows that may later be rolled back.
    """

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._settings_cache: dict[str, str | None] = {}
        self._analytics_cache: dict[str, Any] | None = None

    async def connect(self) -> None:
        from sentinel.db.migrate import migrate

        await migrate(self._path)
        self._conn = await aiosqlite.connect(self._path, timeout=30.0)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=30000")

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def checkpoint(self) -> None:
        """Flush SQLite WAL to database file using PRAGMA wal_checkpoint(TRUNCATE)."""
        if self._conn:
            try:
                await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                logger.info("Database WAL checkpoint completed successfully.")
            except Exception as exc:
                logger.warning("Database WAL checkpoint failed: %s", exc)

    @asynccontextmanager
    async def _write(
        self, clear_analytics_cache: bool = False
    ) -> AsyncGenerator[aiosqlite.Connection, None]:
        # Both reads and writes acquire this lock so that no read can land
        # between a writer's execute and commit on the same connection.
        # On any exit that did not reach commit (including asyncio.CancelledError
        # and other BaseExceptions), the transaction is rolled back and the
        # exception re-raised, so the next writer starts with a clean connection.
        async with self._lock:
            assert self._conn is not None, "Database.connect() was not called"
            committed = False
            try:
                yield self._conn
                await self._conn.commit()
                committed = True
                if clear_analytics_cache:
                    self._analytics_cache = None
            except BaseException:
                if not committed:
                    await self._conn.rollback()
                raise

    @asynccontextmanager
    async def _read(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Context manager that serialises reads with the writer lock."""
        async with self._lock:
            assert self._conn is not None, "Database.connect() was not called"
            yield self._conn

    @property
    def _db(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database.connect() was not called"
        return self._conn

    async def ping(self) -> bool:
        """Return True if the DB connection is live (used by /readyz)."""
        try:
            async with self._read() as db, db.execute("SELECT 1") as cur:
                await cur.fetchone()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Detection events
    # ------------------------------------------------------------------

    async def record_detection(
        self,
        score: float,
        consecutive: int,
        confirmed: int,
        snapshot_path: str | None = None,
    ) -> int:
        """Insert a detection event; returns the new row id."""
        async with self._write() as db:
            cursor = await db.execute(
                "INSERT INTO detection_events (score, consecutive, confirmed, snapshot_path)"
                " VALUES (?, ?, ?, ?)",
                (score, consecutive, confirmed, snapshot_path),
            )
            return cursor.lastrowid or 0

    async def get_recent_detections(self, limit: int = 50) -> list[dict[str, object]]:
        async with (
            self._read() as db,
            db.execute(
                "SELECT id, ts_utc, score, consecutive, confirmed, snapshot_path"
                " FROM detection_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ) as cur,
        ):
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_snapshots_for_cleanup(self, keep_limit: int = 50, limit: int = -1) -> list[str]:
        """Return snapshot_paths of old detection events that should be deleted."""
        async with (
            self._read() as db,
            db.execute(
                "SELECT snapshot_path FROM detection_events WHERE snapshot_path IS NOT NULL"
                " ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, keep_limit),
            ) as cur,
        ):
            rows = await cur.fetchall()
            return [row["snapshot_path"] for row in rows]

    async def delete_old_snapshots(self, snapshot_paths: list[str]) -> None:
        """Clear snapshot_path fields for deleted snapshots."""
        if not snapshot_paths:
            return
        async with self._write() as db:
            for i in range(0, len(snapshot_paths), 500):
                chunk = snapshot_paths[i : i + 500]
                placeholders = ",".join("?" for _ in chunk)
                query = (
                    "UPDATE detection_events SET snapshot_path = NULL"
                    f" WHERE snapshot_path IN ({placeholders})"
                )
                await db.execute(query, chunk)

    # ------------------------------------------------------------------
    # Pause history
    # ------------------------------------------------------------------

    async def record_pause(self, source: str, result: str, error_message: str | None = None) -> int:
        """Open a new pause entry; returns the new row id."""
        async with self._write() as db:
            cursor = await db.execute(
                "INSERT INTO pause_history (source, result, error_message) VALUES (?, ?, ?)",
                (source, result, error_message),
            )
            return cursor.lastrowid or 0

    async def get_recent_pauses(self, limit: int = 50) -> list[dict[str, object]]:
        async with (
            self._read() as db,
            db.execute(
                "SELECT id, ts_utc, source, result, error_message"
                " FROM pause_history ORDER BY id DESC LIMIT ?",
                (limit,),
            ) as cur,
        ):
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Runtime settings (key/value)
    # ------------------------------------------------------------------

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        if key in self._settings_cache:
            val = self._settings_cache[key]
            return val if val is not None else default

        async with (
            self._read() as db,
            db.execute("SELECT value FROM runtime_settings WHERE key = ?", (key,)) as cur,
        ):
            row = await cur.fetchone()
            val = str(row["value"]) if row else None
            self._settings_cache[key] = val
            return val if val is not None else default

    async def set_setting(self, key: str, value: str) -> None:
        async with self._write() as db:
            await db.execute(
                "INSERT INTO runtime_settings (key, value)"
                " VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                "   updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')",
                (key, value),
            )
        self._settings_cache[key] = value

    # ------------------------------------------------------------------
    # Auth session secret
    # ------------------------------------------------------------------

    async def get_auth_secret(self) -> bytes | None:
        """Return the persisted HMAC secret, or None if not yet generated."""
        val = await self.get_setting("auth_cookie_secret")
        if val is None:
            return None
        try:
            return bytes.fromhex(val)
        except ValueError:
            return None

    async def set_auth_secret(self, secret: bytes) -> None:
        """Persist the HMAC secret so sessions survive restarts."""
        await self.set_setting("auth_cookie_secret", secret.hex())

    # ------------------------------------------------------------------
    # Watcher heartbeat
    # ------------------------------------------------------------------

    async def update_heartbeat(self, ts: str, state: str) -> None:
        async with self._write() as db:
            await db.execute(
                "INSERT INTO watcher_heartbeat (id, last_tick_utc, state) VALUES (1, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                " last_tick_utc = excluded.last_tick_utc, state = excluded.state",
                (ts, state),
            )

    async def get_heartbeat(self) -> dict[str, Any] | None:
        async with (
            self._read() as db,
            db.execute("SELECT last_tick_utc, state FROM watcher_heartbeat WHERE id = 1") as cur,
        ):
            row = await cur.fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------
    # Print jobs tracking
    # ------------------------------------------------------------------

    async def record_print_start(self, filename: str, started_at: str) -> int:
        """Insert a new print job starting; returns the job id."""
        async with self._write() as db:
            cursor = await db.execute(
                "INSERT INTO print_jobs (filename, started_at, status, pauses_count)"
                " VALUES (?, ?, 'printing', 0)",
                (filename, started_at),
            )
            return cursor.lastrowid or 0

    async def record_print_end(
        self,
        job_id: int,
        ended_at: str,
        duration_seconds: int,
        filament_used_g: float,
        status: str,
    ) -> None:
        """Update job entry when print ends."""
        async with self._write(clear_analytics_cache=True) as db:
            await db.execute(
                "UPDATE print_jobs SET ended_at = ?, duration_seconds = ?,"
                " filament_used_g = ?, status = ?"
                " WHERE id = ?",
                (ended_at, duration_seconds, filament_used_g, status, job_id),
            )

    async def increment_job_pauses(self, job_id: int) -> None:
        """Increment the pauses count for the given print job."""
        async with self._write(clear_analytics_cache=True) as db:
            await db.execute(
                "UPDATE print_jobs SET pauses_count = pauses_count + 1 WHERE id = ?",
                (job_id,),
            )

    async def get_recent_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent print jobs."""
        async with (
            self._read() as db,
            db.execute(
                "SELECT id, filename, started_at, ended_at, duration_seconds,"
                " filament_used_g, status, pauses_count"
                " FROM print_jobs ORDER BY id DESC LIMIT ?",
                (limit,),
            ) as cur,
        ):
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def clear_all_data(self) -> dict[str, int]:
        """Delete all events, pauses, and jobs. Returns counts per table."""
        async with self._write(clear_analytics_cache=True) as db:
            det = await db.execute("DELETE FROM detection_events")
            pau = await db.execute("DELETE FROM pause_history")
            job = await db.execute("DELETE FROM print_jobs")
            return {
                "detections": det.rowcount or 0,
                "pauses": pau.rowcount or 0,
                "jobs": job.rowcount or 0,
            }

    async def prune_old_events(self, retention_days: int) -> dict[str, int]:
        """Delete rows older than retention_days. Returns counts per table."""
        if retention_days <= 0:
            return {"detections": 0, "pauses": 0, "jobs": 0}
        cutoff = f"-{retention_days} days"
        async with self._write(clear_analytics_cache=True) as db:
            det = await db.execute(
                "DELETE FROM detection_events WHERE ts_utc < "
                "strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)",
                (cutoff,),
            )
            pau = await db.execute(
                "DELETE FROM pause_history WHERE ts_utc < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)",
                (cutoff,),
            )
            job = await db.execute(
                "DELETE FROM print_jobs WHERE started_at < "
                "strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)",
                (cutoff,),
            )
            return {
                "detections": det.rowcount or 0,
                "pauses": pau.rowcount or 0,
                "jobs": job.rowcount or 0,
            }

    async def get_analytics_summary(self) -> dict[str, Any]:
        """Calculate and return key statistics for completed/failed prints."""
        if self._analytics_cache is not None:
            return self._analytics_cache

        # Both the completed/failed totals and the average duration are computed
        # by one query so the whole summary reflects a single atomic snapshot —
        # two sequential queries could otherwise straddle a concurrent write
        # (e.g. a print job completing mid-request) and return a summary where
        # avg_duration_seconds reflects a different set of rows than total_jobs.
        async with (
            self._read() as db,
            db.execute(
                "SELECT COUNT(*) as total_jobs,"
                " SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed_jobs,"
                " SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_jobs,"
                " SUM(duration_seconds) as total_duration_seconds,"
                " SUM(filament_used_g) as total_filament_g,"
                " SUM(pauses_count) as total_pauses,"
                " AVG(CASE WHEN status = 'completed' THEN duration_seconds END) as avg_duration"
                " FROM print_jobs WHERE status IN ('completed', 'failed')"
            ) as cur,
        ):
            row = await cur.fetchone()
            res = dict(row) if row else {}

        total = res.get("total_jobs") or 0
        completed = res.get("completed_jobs") or 0
        success_rate = (completed / total * 100) if total > 0 else 0.0
        avg_duration_raw = res.get("avg_duration")
        avg_duration = avg_duration_raw if avg_duration_raw is not None else 0.0

        summary = {
            "total_prints": total,
            "success_rate_percent": success_rate,
            "total_filament_g": res.get("total_filament_g") or 0.0,
            "total_pauses": res.get("total_pauses") or 0,
            "avg_duration_seconds": avg_duration,
        }
        self._analytics_cache = summary
        return summary

    async def close_stale_jobs(self, ended_at: str) -> int:
        """Mark all status='printing' rows as 'interrupted' with ended_at=ended_at.

        Called once on startup to close phantom rows left by a previous crash or
        restart that happened while a job was in progress.  Returns the number of
        rows closed.
        """
        async with self._write(clear_analytics_cache=True) as db:
            cursor = await db.execute(
                "UPDATE print_jobs SET status = 'interrupted', ended_at = ?"
                " WHERE status = 'printing'",
                (ended_at,),
            )
            return cursor.rowcount or 0

    async def get_all_active_snapshot_paths(self) -> list[str]:
        """Return all active snapshot paths stored in the database."""
        async with (
            self._read() as db,
            db.execute(
                "SELECT snapshot_path FROM detection_events WHERE snapshot_path IS NOT NULL"
            ) as cur,
        ):
            rows = await cur.fetchall()
            return [row["snapshot_path"] for row in rows if row["snapshot_path"]]
