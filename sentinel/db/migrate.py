"""Database migrations — idempotent, schema-versioned."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text()
CURRENT_VERSION = 2


async def migrate(db_path: str) -> None:
    """Apply all pending migrations to *db_path*.

    Safe to call on every startup — already-applied migrations are skipped.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")

        # Determine current version before executing the schema script
        current = 0
        try:
            async with db.execute("SELECT MAX(version) FROM schema_version") as cur:
                row = await cur.fetchone()
                current = row[0] if row and row[0] is not None else 0
        except aiosqlite.OperationalError:
            pass  # Table doesn't exist yet

        # If it's a version 1 database, drop all tables to rebuild the schema.
        # Wrapped in an explicit transaction so a crash mid-migration
        # leaves the DB in the original state rather than partially destroyed.
        if current == 1:
            logger.info("Dropping v1 database tables for schema migration")
            async with db.execute("BEGIN"):
                for table in (
                    "schema_version",
                    "detection_events",
                    "pause_history",
                    "runtime_settings",
                    "watcher_heartbeat",
                ):
                    await db.execute(f"DROP TABLE IF EXISTS {table}")
            await db.commit()
            current = 0

        await db.executescript(_SCHEMA_SQL)

        if current < CURRENT_VERSION:
            await db.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (CURRENT_VERSION,),
            )
            await db.commit()
            logger.info("Database migrated to version %d", CURRENT_VERSION)
        else:
            logger.debug("Database already at version %d", current)


def migrate_sync(db_path: str) -> None:
    asyncio.run(migrate(db_path))
