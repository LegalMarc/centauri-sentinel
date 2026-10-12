"""SessionStore: persistence across restarts, revocation, credential rotation."""

from __future__ import annotations

import asyncio
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


async def test_concurrent_overflow_eviction_does_not_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two creates at the cap must not both evict (and then KeyError on) the same session."""
    monkeypatch.setattr(sessions_mod, "MAX_SESSIONS", 2)
    deleted: list[str] = []

    class SlowDb:
        async def insert_auth_session(self, *_: object) -> None:
            return None

        async def delete_auth_session(self, token_hash: str) -> None:
            await asyncio.sleep(0.05)
            deleted.append(token_hash)

    store = SessionStore(FP, SlowDb())  # type: ignore[arg-type]
    for _ in range(2):
        await store.create()
    oldest = min(store._sessions, key=lambda h: store._sessions[h].refreshed_at)
    store._sessions[oldest].refreshed_at = 0

    tokens = await asyncio.gather(store.create(), store.create())

    assert len(store._sessions) == 2
    assert all(store.is_valid(t) for t in tokens)
    assert len(deleted) == 2
    assert len(set(deleted)) == 2


async def test_eviction_survives_db_delete_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed row delete must not turn a successful login into a 500."""
    monkeypatch.setattr(sessions_mod, "MAX_SESSIONS", 1)

    class FailingDeleteDb:
        async def insert_auth_session(self, *_: object) -> None:
            return None

        async def delete_auth_session(self, _token_hash: str) -> None:
            raise RuntimeError("database is locked")

    store = SessionStore(FP, FailingDeleteDb())  # type: ignore[arg-type]
    first = await store.create()
    store._sessions[sessions_mod._hash_token(first)].refreshed_at = 0
    with caplog.at_level("WARNING", logger="sentinel.web.sessions"):
        second = await store.create()  # evicts `first`, delete fails, must not raise

    assert not store.is_valid(first)  # in-memory eviction stands
    assert store.is_valid(second)
    assert "Failed to delete evicted login session" in caplog.text


async def test_load_enforces_max_sessions(db_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _open(db_path)
    store = SessionStore(FP, db)
    tokens = [await store.create() for _ in range(3)]
    for age, token in enumerate(tokens):  # tokens[0] is the least recently refreshed
        store._sessions[sessions_mod._hash_token(token)].refreshed_at = 1000 + age
        await db.touch_auth_session(sessions_mod._hash_token(token), 1000 + age)
    await db.close()

    monkeypatch.setattr(sessions_mod, "MAX_SESSIONS", 2)
    monkeypatch.setattr(sessions_mod, "COOKIE_MAX_AGE", 10**12)  # keep the old rows
    db = await _open(db_path)
    reloaded = SessionStore(FP, db)
    await reloaded.load()

    assert not reloaded.is_valid(tokens[0])
    assert reloaded.is_valid(tokens[1])
    assert reloaded.is_valid(tokens[2])
    assert len(await db.list_auth_sessions()) == 2  # the row was deleted too
    await db.close()


async def test_revoke_all_clears_memory_and_database(db_path: str) -> None:
    db = await _open(db_path)
    store = SessionStore(FP, db)
    tokens = [await store.create() for _ in range(3)]

    assert await store.revoke_all() == 3

    assert not any(store.is_valid(t) for t in tokens)
    assert await db.list_auth_sessions() == []
    await db.close()

    db = await _open(db_path)
    reloaded = SessionStore(FP, db)
    await reloaded.load()
    assert not any(reloaded.is_valid(t) for t in tokens)
    await db.close()


async def test_revoke_all_memory_only_store() -> None:
    store = SessionStore(FP)
    token = await store.create()
    assert await store.revoke_all() == 1
    assert not store.is_valid(token)
    assert await store.revoke_all() == 0


async def test_revoke_all_returns_the_database_row_count(db_path: str) -> None:
    db = await _open(db_path)
    store = SessionStore(FP, db)
    for _ in range(2):
        await store.create()
    # A row the in-memory index does not know about (another replica's login).
    await db.insert_auth_session("f" * 64, FP, 1)

    assert await store.revoke_all() == 3  # the DB count, not the 2 in memory
    assert await db.list_auth_sessions() == []
    await db.close()


async def test_revoke_all_db_failure_keeps_sessions(db_path: str) -> None:
    db = await _open(db_path)
    store = SessionStore(FP, db)
    token = await store.create()

    async def boom() -> int:
        raise OSError("db is down")

    db.delete_all_auth_sessions = boom  # type: ignore[method-assign]
    with pytest.raises(OSError):
        await store.revoke_all()

    assert store.is_valid(token)  # fail closed: still valid in both places
    await db.close()
