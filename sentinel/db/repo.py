"""Async repository layer — all DB writes go through a single asyncio.Lock."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)


class Database:
    """Wrapper around an aiosqlite connection with a serialising writer lock."""

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        from sentinel.db.migrate import migrate

        await migrate(self._path)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def _write(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        async with self._lock:
            assert self._conn is not None, "Database.connect() was not called"
            yield self._conn
            await self._conn.commit()

    @property
    def _db(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database.connect() was not called"
        return self._conn

    # ------------------------------------------------------------------
    # Detection events
    # ------------------------------------------------------------------

    async def record_detection(
        self,
        score: float,
        snapshot_id: str | None = None,
        action: str = "alert",
    ) -> int:
        """Insert a detection event; returns the new row id."""
        async with self._write() as db:
            cursor = await db.execute(
                "INSERT INTO detection_events (score, snapshot_id, action) VALUES (?, ?, ?)",
                (score, snapshot_id, action),
            )
            return cursor.lastrowid or 0

    async def get_recent_detections(self, limit: int = 50) -> list[dict[str, object]]:
        async with self._db.execute(
            "SELECT id, ts, score, snapshot_id, action"
            " FROM detection_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_snapshots_for_cleanup(self, keep_limit: int = 50) -> list[str]:
        """Return snapshot_ids of old detection events that should be deleted."""
        async with self._db.execute(
            "SELECT snapshot_id FROM detection_events WHERE snapshot_id IS NOT NULL"
            " ORDER BY id DESC"
        ) as cur:
            rows = list(await cur.fetchall())
            if len(rows) <= keep_limit:
                return []
            return [row["snapshot_id"] for row in rows[keep_limit:]]

    async def delete_old_snapshots(self, snapshot_ids: list[str]) -> None:
        """Clear snapshot_id fields for deleted snapshots."""
        if not snapshot_ids:
            return
        async with self._write() as db:
            placeholders = ",".join("?" for _ in snapshot_ids)
            query = (
                "UPDATE detection_events SET snapshot_id = NULL"
                f" WHERE snapshot_id IN ({placeholders})"
            )
            await db.execute(query, snapshot_ids)

    # ------------------------------------------------------------------
    # Pause history
    # ------------------------------------------------------------------

    async def record_pause(self, reason: str = "detection") -> int:
        """Open a new pause entry; returns the new row id."""
        async with self._write() as db:
            cursor = await db.execute(
                "INSERT INTO pause_history (reason) VALUES (?)",
                (reason,),
            )
            return cursor.lastrowid or 0

    async def end_pause(self, pause_id: int) -> None:
        async with self._write() as db:
            await db.execute(
                "UPDATE pause_history SET ended_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
                " WHERE id = ? AND ended_at IS NULL",
                (pause_id,),
            )

    async def get_recent_pauses(self, limit: int = 50) -> list[dict[str, object]]:
        async with self._db.execute(
            "SELECT id, started_at, ended_at, reason FROM pause_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Runtime settings (key/value)
    # ------------------------------------------------------------------

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        async with self._db.execute(
            "SELECT value FROM runtime_settings WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return str(row["value"]) if row else default

    async def set_setting(self, key: str, value: str) -> None:
        async with self._write() as db:
            await db.execute(
                "INSERT INTO runtime_settings (key, value)"
                " VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                "   updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')",
                (key, value),
            )

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

    async def update_heartbeat(self, ts: str) -> None:
        async with self._write() as db:
            await db.execute(
                "INSERT INTO watcher_heartbeat (id, last_beat) VALUES (1, ?)"
                " ON CONFLICT(id) DO UPDATE SET last_beat = excluded.last_beat",
                (ts,),
            )

    async def get_heartbeat(self) -> str | None:
        async with self._db.execute("SELECT last_beat FROM watcher_heartbeat WHERE id = 1") as cur:
            row = await cur.fetchone()
            return str(row["last_beat"]) if row else None
