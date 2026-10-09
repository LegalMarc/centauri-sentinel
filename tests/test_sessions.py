"""SessionStore: persistence across restarts, revocation, credential rotation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sentinel.db.repo import Database
from sentinel.web import sessions as sessions_mod
from sentinel.web.sessions import (
    COOKIE_MAX_AGE,
    REFRESH_INTERVAL,
    SessionStore,
    credential_fingerprint,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

FP = credential_fingerprint("admin", "$2b$12$hash")


@pytest.fixture
async def db_path(tmp_path: Path) -> AsyncGenerator[str, None]:
    yield str(tmp_path / "s.db")


async def _open(path: str) -> Database:
    db = Database(path)
    await db.connect()
    return db


async def test_session_survives_restart(db_path: str) -> None:
    db = await _open(db_path)
    token = await SessionStore(FP, db).create()
    await db.close()

    db = await _open(db_path)
    store = SessionStore(FP, db)
    await store.load()
    assert store.is_valid(token)
    await db.close()


async def test_revoke_persists_across_restart(db_path: str) -> None:
    db = await _open(db_path)
    store = SessionStore(FP, db)
    token = await store.create()
    await store.revoke(token)
    assert not store.is_valid(token)
    await db.close()

    db = await _open(db_path)
    store = SessionStore(FP, db)
    await store.load()
    assert not store.is_valid(token)
    await db.close()


async def test_credential_change_invalidates_sessions(db_path: str) -> None:
    db = await _open(db_path)
    token = await SessionStore(FP, db).create()
    await db.close()

    db = await _open(db_path)
    store = SessionStore(credential_fingerprint("admin", "$2b$12$rotated"), db)
    await store.load()
    assert not store.is_valid(token)
    await db.close()


async def test_session_idle_past_cookie_lifetime_is_dropped(
    db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _open(db_path)
    token = await SessionStore(FP, db).create()
    await db.close()

    import time

    real_time = time.time
    monkeypatch.setattr(sessions_mod.time, "time", lambda: real_time() + COOKIE_MAX_AGE + 60)
    db = await _open(db_path)
    store = SessionStore(FP, db)
    await store.load()
    assert not store.is_valid(token)
    await db.close()


async def test_refresh_resets_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    store = SessionStore(FP)
    token = await store.create()
    assert not store.needs_refresh(token)

    import time

    real_time = time.time
    monkeypatch.setattr(sessions_mod.time, "time", lambda: real_time() + REFRESH_INTERVAL + 1)
    assert store.needs_refresh(token)
    await store.refresh(token)
    assert not store.needs_refresh(token)


async def test_unknown_and_empty_tokens_are_invalid() -> None:
    store = SessionStore(FP)
    assert not store.is_valid("")
    assert not store.is_valid("not-a-session")
    await store.revoke("not-a-session")  # no-op, must not raise


async def test_overflow_evicts_least_recently_refreshed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions_mod, "MAX_SESSIONS", 2)
    store = SessionStore(FP)
    first = await store.create()
    store._sessions[sessions_mod._hash_token(first)].refreshed_at = 0
    second = await store.create()
    third = await store.create()
    assert not store.is_valid(first)
    assert store.is_valid(second)
    assert store.is_valid(third)
