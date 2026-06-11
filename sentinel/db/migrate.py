"""Database migrations — idempotent, schema-versioned."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text()
CURRENT_VERSION = 7

# Tables to rename during v1 → v2 migration, in the order they should be
# processed.  schema_version is renamed LAST so that an interrupted run can be
# detected and re-entered on the next startup (the version row still exists
# until the very last rename succeeds).
_V1_TABLES = (
    "detection_events",
    "pause_history",
    "runtime_settings",
    "watcher_heartbeat",
    "schema_version",  # LAST — must stay last; see Addendum in issue #55
)


def _split_sql(sql: str) -> list[str]:
    """Split a SQL script into individual statements.

    Uses a character-level state machine that tracks single-quoted strings and
    ``--`` line comments so that semicolons inside those contexts are not
    treated as statement terminators.  This schema has no triggers or
    dollar-quoting, so the state machine is minimal.

    Chunks that contain no SQL keyword (e.g. comment-only preamble) are
    discarded.
    """
    _SQL_KEYWORDS = frozenset(
        [
            "CREATE",
            "INSERT",
            "UPDATE",
            "DELETE",
            "DROP",
            "ALTER",
            "PRAGMA",
            "BEGIN",
            "COMMIT",
            "ROLLBACK",
            "SELECT",
        ]
    )

    statements: list[str] = []
    current: list[str] = []
    in_string = False  # inside single-quoted string literal
    in_line_comment = False  # inside -- comment (until end of line)
    i = 0

    while i < len(sql):
        ch = sql[i]

        if in_line_comment:
            current.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_string:
            current.append(ch)
            if ch == "'":
                # Check for escaped quote ''
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    current.append(sql[i + 1])
                    i += 2
                    continue
                in_string = False
            i += 1
            continue

        # Normal context
        if ch == "'":
            in_string = True
            current.append(ch)
            i += 1
            continue

        if ch == "-" and i + 1 < len(sql) and sql[i + 1] == "-":
            in_line_comment = True
            current.append(ch)
            i += 1
            continue

        if ch == ";":
            # Statement terminator — flush the current buffer.
            stmt_text = "".join(current).strip()
            current = []
            # Check if the chunk contains a recognisable SQL keyword.
            first_kw = None
            for line in stmt_text.splitlines():
                words = line.strip().split()
                if words and not line.strip().startswith("--"):
                    first_kw = words[0].upper()
                    break
            if first_kw and first_kw in _SQL_KEYWORDS:
                statements.append(stmt_text)
            i += 1
            continue

        current.append(ch)
        i += 1

    # Handle any trailing content (no trailing semicolon).
    stmt_text = "".join(current).strip()
    if stmt_text:
        first_kw = None
        for line in stmt_text.splitlines():
            words = line.strip().split()
            if words and not line.strip().startswith("--"):
                first_kw = words[0].upper()
                break
        if first_kw and first_kw in _SQL_KEYWORDS:
            statements.append(stmt_text)

    return statements


async def migrate(db_path: str) -> None:
    """Apply all pending migrations to *db_path*.

    Safe to call on every startup — already-applied migrations are skipped.

    Atomicity guarantee
    -------------------
    All schema DDL statements and the version insert are executed inside a
    single explicit transaction.  A crash mid-migration leaves the DB in its
    pre-migration state; the idempotent ``IF NOT EXISTS`` guards allow a clean
    retry on the next startup.

    The v1 → v2 rename sequence is also wrapped in an explicit transaction.
    ``schema_version`` is renamed LAST, so an interrupted rename sequence is
    detectable: the version row still exists and the next startup re-enters the
    same path and retries the renames (``IF EXISTS`` / OperationalError
    suppression makes this safe).
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=30000")

        # Determine current version before executing the schema script
        current = 0
        try:
            async with db.execute("SELECT MAX(version) FROM schema_version") as cur:
                row = await cur.fetchone()
                current = row[0] if row and row[0] is not None else 0
        except aiosqlite.OperationalError:
            pass  # Table doesn't exist yet

        # If it's a version 1 database, rename all tables to _v1 to preserve
        # data and then recreate the schema from scratch.
        #
        # The rename sequence is wrapped in a single transaction.
        # schema_version is renamed LAST so that an interrupted sequence
        # (partially renamed tables but version row still present) is
        # detectable: the next startup reads current == 1 again and retries.
        if current == 1:
            logger.info(
                "v1 database detected; renaming tables to _v1 to preserve data and recreate schema"
            )
            try:
                await db.execute("BEGIN")
                for table in _V1_TABLES:
                    with contextlib.suppress(aiosqlite.OperationalError):
                        await db.execute(f"ALTER TABLE {table} RENAME TO {table}_v1")
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            current = 0

        # Wrap schema creation and version tracking in a single transaction.
        # We execute each statement individually (rather than using
        # executescript) because sqlite3's executescript issues an implicit
        # COMMIT before running the script, which would break atomicity.
        try:
            await db.execute("BEGIN")
            for stmt in _split_sql(_SCHEMA_SQL):
                await db.execute(stmt)
            if current < CURRENT_VERSION:
                await db.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (CURRENT_VERSION,),
                )
            await db.commit()
            if current < CURRENT_VERSION:
                logger.info("Database migrated to version %d", CURRENT_VERSION)
            else:
                logger.debug("Database already at version %d", current)
        except Exception:
            await db.rollback()
            raise


def migrate_sync(db_path: str) -> None:
    asyncio.run(migrate(db_path))
