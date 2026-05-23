"""Database migrations — idempotent, schema-versioned."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text()
CURRENT_VERSION = 1


async def migrate(db_path: str) -> None:
    """Apply all pending migrations to *db_path*.

    Safe to call on every startup — already-applied migrations are skipped.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")

        await db.executescript(_SCHEMA_SQL)

        async with db.execute("SELECT MAX(version) FROM schema_version") as cur:
            row = await cur.fetchone()
            current = row[0] if row and row[0] is not None else 0

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
