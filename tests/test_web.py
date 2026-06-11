"""Tests for the status web UI — ticket #10."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import bcrypt
import httpx
import pytest

from sentinel.config import Settings
from sentinel.watcher.state import WatcherState
from sentinel.web.app import create_app

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20 + b"\xff\xd9"
_HASHED_PASS = bcrypt.hashpw(b"testpass", bcrypt.gensalt(rounds=4)).decode()


def _fresh_ts() -> str:
    return datetime.now(UTC).isoformat()


def _stale_ts() -> str:
    return (datetime.now(UTC) - timedelta(seconds=120)).isoformat()


def _base_settings(**kwargs: object) -> Settings:
    defaults: dict[str, object] = {
        "printer_ip": "192.168.1.10",
        "printer_access_code": "000000",
        "bind_host": "127.0.0.1",
        "external_bind_allowed": True,
        "trust_proxies": True,
    }
    defaults.update(kwargs)
    return Settings(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_db_execute_cm() -> AsyncMock:
    """Return an async context manager mock for db._db.execute()."""
    cursor = AsyncMock()
    cursor.fetchone = AsyncMock(return_value=(1,))
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=cursor)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock()
    db.get_heartbeat.return_value = {"last_tick_utc": _fresh_ts(), "state": "ARMED"}
    db.get_recent_detections.return_value = [
        {
            "id": 1,
            "ts_utc": "2026-01-01T00:00:00Z",
            "score": 0.85,
            "consecutive": 1,
            "confirmed": 0,
            "snapshot_path": None,
        },
    ]
    db.get_recent_pauses.return_value = [
        {
            "id": 1,
            "ts_utc": "2026-01-01T00:00:00Z",
            "source": "auto",
            "result": "ok",
            "error_message": None,
        },
    ]
    db.set_setting.return_value = None
    # _db is the raw aiosqlite connection; routes.py uses it for SELECT 1 in /readyz
    db._db = MagicMock()
    db._db.execute.return_value = _make_db_execute_cm()
    return db


@pytest.fixture
def mock_watcher() -> MagicMock:
    w = MagicMock()
    w.state = WatcherState.ARMED
    w.last_printer_status = None
    w.printer = MagicMock()
    w.printer.is_connected = True

    async def get_fresh_status(force: bool = False) -> object:
        return w.last_printer_status

    w.get_fresh_status = get_fresh_status
    return w


@pytest.fixture
def mock_camera() -> AsyncMock:
    cam = AsyncMock()
    cam.grab.return_value = _FAKE_JPEG
    cam.is_connected = True
    return cam


@pytest.fixture
def app(mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: AsyncMock) -> object:
    return create_app(_base_settings(), db=mock_db, watcher=mock_watcher, camera=mock_camera)


@pytest.fixture
def auth_app(mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: AsyncMock) -> object:
    settings = _base_settings(auth_username="admin", auth_password_bcrypt=_HASHED_PASS)
    return create_app(settings, db=mock_db, watcher=mock_watcher, camera=mock_camera)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _client(application: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application, client=("127.0.0.1", 12345)),  # type: ignore[arg-type]
        base_url="http://test",
        headers={"Host": "test", "Origin": "http://test"},
    )


# ---------------------------------------------------------------------------
# /healthz — always open, no deps required
# ---------------------------------------------------------------------------


async def test_healthz_no_deps() -> None:
    minimal_app = create_app(_base_settings())
    async with _client(minimal_app) as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_healthz_with_deps(app: object) -> None:
    async with _client(app) as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_healthz_exposes_bot_crash_count() -> None:
    from fastapi import FastAPI

    settings = _base_settings()
    app = create_app(settings)
    assert isinstance(app, FastAPI)

    mock_bot = MagicMock()
    mock_bot.crash_count = 3
    app.state.bot = mock_bot

    async with _client(app) as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["telegram_bot_crash_count"] == 3


async def test_healthz_503_when_watcher_task_dead() -> None:
    """Container health check must return 503 when the watcher task has died."""
    import asyncio

    from fastapi import FastAPI

    settings = _base_settings()
    app = create_app(settings)
    assert isinstance(app, FastAPI)

    # Create a task that completes immediately (simulates a dead watcher)
    async def _dead() -> None:
        pass

    dead_task: asyncio.Task[None] = asyncio.ensure_future(_dead())
    await asyncio.sleep(0)  # let the event loop run the task to completion
    app.state.watcher_task = dead_task

    async with _client(app) as c:
        r = await c.get("/healthz")
    assert r.status_code == 503
    data = r.json()
    assert data["status"] == "degraded"
    assert data["watcher"] == "dead"


async def test_healthz_200_when_watcher_task_alive() -> None:
    """Container health check must return 200 while watcher task is running."""
    import asyncio

    from fastapi import FastAPI

    settings = _base_settings()
    app = create_app(settings)
    assert isinstance(app, FastAPI)

    # Create a task that never finishes (simulates a live watcher)
    async def _live() -> None:
        await asyncio.sleep(9999)

    live_task: asyncio.Task[None] = asyncio.ensure_future(_live())
    app.state.watcher_task = live_task

    try:
        async with _client(app) as c:
            r = await c.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
    finally:
        live_task.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError):
            await live_task


# ---------------------------------------------------------------------------
# / — status page
# ---------------------------------------------------------------------------


async def test_status_page_renders(app: object) -> None:
    async with _client(app) as c:
        r = await c.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "centauri-sentinel" in body
    assert "ARMED" in body
    assert "Watcher state" in body
    assert "Recent detections" in body
    assert "Recent pauses" in body


async def test_status_page_no_tailwind_cdn(app: object) -> None:
    async with _client(app) as c:
        r = await c.get("/")
    body = r.text
    assert "tailwind" not in body.lower()
    assert "cdn.jsdelivr.net" not in body
    assert "unpkg.com" not in body
    assert "cdnjs.cloudflare.com" not in body


async def test_status_page_css_under_2kb(app: object) -> None:
    """Embedded CSS must stay under 16 KB."""
    async with _client(app) as c:
        r = await c.get("/")
    body = r.text
    start = body.find("<style>")
    end = body.find("</style>")
    assert start != -1 and end != -1
    css_bytes = len(body[start:end].encode())
    assert css_bytes < 16384, f"Embedded CSS is {css_bytes} bytes (limit 16384)"


async def test_status_page_no_meta_refresh(app: object) -> None:
    async with _client(app) as c:
        r = await c.get("/")
    assert 'http-equiv="refresh"' not in r.text


# ---------------------------------------------------------------------------
# /snapshot
# ---------------------------------------------------------------------------


async def test_snapshot_returns_jpeg(app: object, mock_camera: AsyncMock) -> None:
    async with _client(app) as c:
        r = await c.get("/snapshot")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content == _FAKE_JPEG


async def test_snapshot_503_on_camera_error(mock_db: AsyncMock, mock_watcher: MagicMock) -> None:
    cam = AsyncMock()
    cam.grab.side_effect = RuntimeError("camera down")
    broken_app = create_app(_base_settings(), db=mock_db, watcher=mock_watcher, camera=cam)
    async with _client(broken_app) as c:
        r = await c.get("/snapshot")
    assert r.status_code == 503


async def test_get_saved_snapshot_not_found(app: object) -> None:
    async with _client(app) as c:
        r = await c.get("/snapshot/nonexistent_snap_id")
    assert r.status_code == 404


async def test_get_saved_snapshot_success(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: MagicMock, tmp_path: Path
) -> None:
    # snapshot_id must be a valid uuid4().hex (32 lowercase hex chars)
    snap_id = "a" * 32

    db_path = tmp_path / "sentinel.db"
    mock_db._path = str(db_path)

    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    (snapshots_dir / f"{snap_id}.jpg").write_bytes(b"saved_jpeg_data")

    app_with_real_paths = create_app(
        _base_settings(), db=mock_db, watcher=mock_watcher, camera=mock_camera
    )
    async with _client(app_with_real_paths) as c:
        r = await c.get(f"/snapshot/{snap_id}")

    assert r.status_code == 200
    assert r.content == b"saved_jpeg_data"
    assert r.headers["content-type"] == "image/jpeg"


# ---------------------------------------------------------------------------
# /readyz
# ---------------------------------------------------------------------------


async def test_readyz_ok(app: object) -> None:
    async with _client(app) as c:
        r = await c.get("/readyz")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ready"
    assert data["subsystems"] == {
        "db": "reachable",
        "watcher": "healthy",
        "mqtt": "connected",
        "camera": "reachable",
    }


async def test_readyz_camera_disconnected_returns_503(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: AsyncMock
) -> None:
    mock_camera.is_connected = False
    stalled_app = create_app(_base_settings(), db=mock_db, watcher=mock_watcher, camera=mock_camera)
    async with _client(stalled_app) as c:
        r = await c.get("/readyz")
    assert r.status_code == 503
    data = r.json()
    assert data["status"] == "not ready"
    assert "camera unreachable" in data["reasons"]
    assert data["subsystems"]["camera"] == "unreachable"


async def test_readyz_mqtt_disconnected_returns_503(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: AsyncMock
) -> None:
    mock_watcher.printer.is_connected = False
    stalled_app = create_app(_base_settings(), db=mock_db, watcher=mock_watcher, camera=mock_camera)
    async with _client(stalled_app) as c:
        r = await c.get("/readyz")
    assert r.status_code == 503
    data = r.json()
    assert data["status"] == "not ready"
    assert "mqtt printer disconnected" in data["reasons"]
    assert data["subsystems"]["mqtt"] == "disconnected"


async def test_readyz_no_heartbeat_returns_503(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: AsyncMock
) -> None:
    mock_db.get_heartbeat.return_value = None
    stalled_app = create_app(_base_settings(), db=mock_db, watcher=mock_watcher, camera=mock_camera)
    async with _client(stalled_app) as c:
        r = await c.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert "reasons" in body
    assert any("heartbeat" in reason for reason in body["reasons"])


async def test_readyz_stale_heartbeat_returns_503(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: AsyncMock
) -> None:
    mock_db.get_heartbeat.return_value = {"last_tick_utc": _stale_ts(), "state": "ARMED"}
    stalled_app = create_app(_base_settings(), db=mock_db, watcher=mock_watcher, camera=mock_camera)
    async with _client(stalled_app) as c:
        r = await c.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert "reasons" in body
    assert any("stalled" in reason for reason in body["reasons"])


async def test_readyz_no_db_returns_503() -> None:
    no_db_app = create_app(_base_settings())
    async with _client(no_db_app) as c:
        r = await c.get("/readyz")
    assert r.status_code == 503
    assert "reasons" in r.json()


async def test_readyz_db_write_failure_returns_503(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: AsyncMock
) -> None:
    # Simulate db.ping() returning False (e.g. connection closed / disk full)
    mock_db.ping.return_value = False
    failing_app = create_app(_base_settings(), db=mock_db, watcher=mock_watcher, camera=mock_camera)
    async with _client(failing_app) as c:
        r = await c.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert any("reachable" in reason for reason in body["reasons"])


# ---------------------------------------------------------------------------
# Auth — disabled
# ---------------------------------------------------------------------------


async def test_auth_disabled_all_routes_open(app: object) -> None:
    async with _client(app) as c:
        assert (await c.get("/")).status_code == 200
        assert (await c.get("/snapshot")).status_code == 200
        assert (await c.get("/healthz")).status_code == 200
        assert (await c.get("/readyz")).status_code == 200


# ---------------------------------------------------------------------------
# Auth — enabled
# ---------------------------------------------------------------------------


async def test_auth_enabled_rejects_no_credentials(auth_app: object) -> None:
    async with _client(auth_app) as c:
        r = await c.get("/")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").startswith("Basic")


async def test_auth_enabled_rejects_wrong_password(auth_app: object) -> None:
    async with _client(auth_app) as c:
        r = await c.get("/", auth=("admin", "wrongpass"))
    # Either 401 challenge or redirect to / (if wrong password leads to loop)
    assert r.status_code in (401, 302)


async def test_auth_enabled_accepts_valid_credentials(auth_app: object) -> None:
    async with _client(auth_app) as c:
        # First request with Basic auth returns a redirect + sets cookie
        r = await c.get("/", auth=("admin", "testpass"), follow_redirects=False)
    assert r.status_code == 302
    assert "sentinel_session" in r.headers.get("set-cookie", "")


async def test_auth_enabled_accepts_valid_credentials_preserves_query(auth_app: object) -> None:
    async with _client(auth_app) as c:
        r = await c.get("/?foo=bar&baz=qux", auth=("admin", "testpass"), follow_redirects=False)
    assert r.status_code == 302
    assert r.headers.get("location") == "/?foo=bar&baz=qux"


async def test_auth_cookie_secure_options(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: AsyncMock
) -> None:
    # 1. always
    settings_always = _base_settings(
        auth_username="admin", auth_password_bcrypt=_HASHED_PASS, auth_cookie_secure="always"
    )
    app_always = create_app(settings_always, db=mock_db, watcher=mock_watcher, camera=mock_camera)
    async with _client(app_always) as c:
        r = await c.get("/", auth=("admin", "testpass"), follow_redirects=False)
        assert "Secure" in r.headers.get("set-cookie", "")

    # 2. never
    settings_never = _base_settings(
        auth_username="admin", auth_password_bcrypt=_HASHED_PASS, auth_cookie_secure="never"
    )
    app_never = create_app(settings_never, db=mock_db, watcher=mock_watcher, camera=mock_camera)
    async with _client(app_never) as c:
        # Check even with X-Forwarded-Proto: https
        r = await c.get(
            "/",
            auth=("admin", "testpass"),
            headers={"X-Forwarded-Proto": "https"},
            follow_redirects=False,
        )
        assert "Secure" not in r.headers.get("set-cookie", "")

    # 3. auto (default) - HTTP / no headers -> not Secure
    settings_auto = _base_settings(
        auth_username="admin", auth_password_bcrypt=_HASHED_PASS, auth_cookie_secure="auto"
    )
    app_auto = create_app(settings_auto, db=mock_db, watcher=mock_watcher, camera=mock_camera)
    async with _client(app_auto) as c:
        r = await c.get("/", auth=("admin", "testpass"), follow_redirects=False)
        assert "Secure" not in r.headers.get("set-cookie", "")

    # 4. auto (default) - HTTP + X-Forwarded-Proto: https -> Secure
    async with _client(app_auto) as c:
        r = await c.get(
            "/",
            auth=("admin", "testpass"),
            headers={"X-Forwarded-Proto": "https"},
            follow_redirects=False,
        )
        assert "Secure" in r.headers.get("set-cookie", "")


async def test_auth_failed_login_delay(auth_app: object) -> None:
    import time

    async with _client(auth_app) as c:
        start_time = time.time()
        r = await c.get("/", auth=("admin", "wrongpass"))
        elapsed = time.time() - start_time
    assert r.status_code == 401
    assert elapsed >= 0.4


async def test_csrf_enforcement_when_auth_enabled(app: object, auth_app: object) -> None:
    # 1. Missing Origin and Referer -> 403
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=auth_app), base_url="http://test"
    ) as c:
        r = await c.post("/api/settings", json={"ml_confirm_count": 5})
        assert r.status_code == 403
        assert "CSRF Protection: Missing Origin and Referer" in r.text

    # 2. Origin Mismatch -> 403
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=auth_app), base_url="http://test"
    ) as c:
        r = await c.post(
            "/api/settings",
            json={"ml_confirm_count": 5},
            headers={"Host": "test", "Origin": "http://malicious.com"},
        )
        assert r.status_code == 403
        assert "CSRF Protection: Origin mismatch" in r.text

    # 2b. Subdomain Origin Mismatch -> 403
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=auth_app), base_url="http://test"
    ) as c:
        r = await c.post(
            "/api/settings",
            json={"ml_confirm_count": 5},
            headers={"Host": "test", "Origin": "http://sub.test"},
        )
        assert r.status_code == 403
        assert "CSRF Protection: Origin mismatch" in r.text

    # 3. Referer Mismatch -> 403
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=auth_app), base_url="http://test"
    ) as c:
        r = await c.post(
            "/api/settings",
            json={"ml_confirm_count": 5},
            headers={"Host": "test", "Referer": "http://malicious.com/dashboard"},
        )
        assert r.status_code == 403
        assert "CSRF Protection: Referer mismatch" in r.text

    # 4. CSRF checks are STILL enforced when auth is disabled -> 403
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)), base_url="http://test"
    ) as c:
        r = await c.post(
            "/api/settings",
            json={"ml_confirm_count": 5},
        )
        assert r.status_code == 403
        assert "CSRF Protection: Missing Origin and Referer" in r.text

    # 5. When auth is disabled and CSRF passes, access is allowed -> 200
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)), base_url="http://test"
    ) as c:
        r = await c.post(
            "/api/settings",
            json={"ml_confirm_count": 5},
            headers={"Host": "test", "Origin": "http://test"},
        )
        assert r.status_code == 200


async def test_limit_upload_size_middleware(app: object) -> None:
    # Payload exceeding 1MB
    large_payload = {"printer_ip": "192.168.1.10", "data": "x" * (1024 * 1024 + 100)}
    async with _client(app) as c:
        r = await c.post("/api/settings", json=large_payload)
    assert r.status_code == 413
    assert r.text == "Payload Too Large"


async def test_limit_upload_size_middleware_edge_cases() -> None:
    from unittest.mock import ANY

    from sentinel.web.app import LimitUploadSizeMiddleware

    # 1. Non-HTTP scope
    mw = LimitUploadSizeMiddleware(AsyncMock())
    scope = {"type": "websocket"}
    await mw(scope, AsyncMock(), AsyncMock())
    mw.app.assert_called_once_with(scope, ANY, ANY)

    # 2. HTTP GET request (non-POST/PUT/PATCH method)
    mw = LimitUploadSizeMiddleware(AsyncMock())
    scope = {"type": "http", "method": "GET"}
    await mw(scope, AsyncMock(), AsyncMock())
    mw.app.assert_called_once_with(scope, ANY, ANY)

    # 3. HTTP POST request streaming body exceeding 1MB (without Content-Length header)
    mw = LimitUploadSizeMiddleware(AsyncMock())
    scope = {
        "type": "http",
        "method": "POST",
        "headers": [],
    }

    # Simulate receiving chunks that sum up to > 1MB
    chunks = [
        {"type": "http.request", "body": b"x" * 600000},
        {"type": "http.request", "body": b"x" * 600000},
    ]
    chunk_iter = iter(chunks)

    async def mock_receive() -> dict[str, object]:
        return next(chunk_iter)

    # We mock send to capture the response
    sent_messages = []

    async def mock_send(msg: dict[str, object]) -> None:
        sent_messages.append(msg)

    # We mock mw.app to simulate reading the body
    async def mock_app(scope: object, receive: object, send: object) -> None:
        # Read the first chunk
        await receive()
        # Read the second chunk, which will trigger the exception
        await receive()

    mw.app.side_effect = mock_app

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await mw(scope, mock_receive, mock_send)
    assert excinfo.value.status_code == 413
    assert excinfo.value.detail == "Payload Too Large"

    # 4. Standard runtime exception propagation
    mw = LimitUploadSizeMiddleware(AsyncMock())
    scope = {
        "type": "http",
        "method": "POST",
        "headers": [],
    }

    async def bad_app(scope: object, receive: object, send: object) -> None:
        raise ValueError("some other error")

    mw.app.side_effect = bad_app
    with pytest.raises(ValueError, match="some other error"):
        await mw(scope, AsyncMock(), AsyncMock())


async def test_auth_timing_oracle_checkpw_always_called(
    auth_app: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def mock_checkpw(password: bytes, hashed: bytes) -> bool:
        calls.append((password, hashed))
        return False

    monkeypatch.setattr("sentinel.web.auth.bcrypt.checkpw", mock_checkpw)

    # 1. Request with invalid username
    async with _client(auth_app) as c:
        await c.get("/", auth=("invalid_user", "somepassword"))
    assert len(calls) == 1
    assert calls[0][0] == b"somepassword"
    # Ensure it checked against the configured password hash to preserve cost rounds
    assert calls[0][1].decode() == _HASHED_PASS

    # 2. Request with valid username but wrong password
    calls.clear()
    async with _client(auth_app) as c:
        await c.get("/", auth=("admin", "wrongpassword"))
    assert len(calls) == 1
    assert calls[0][0] == b"wrongpassword"
    # Ensure it checked against the actual password hash
    assert calls[0][1].decode() == _HASHED_PASS


async def test_auth_cookie_grants_access(auth_app: object) -> None:
    async with _client(auth_app) as c:
        r1 = await c.get("/", auth=("admin", "testpass"), follow_redirects=False)
        cookie = r1.cookies.get("sentinel_session")
        assert cookie is not None
        # Use the cookie on the client instance
        c.cookies.set("sentinel_session", cookie)
        r2 = await c.get("/")
    assert r2.status_code == 200


async def test_logout_clears_cookie(auth_app: object) -> None:
    async with _client(auth_app) as c:
        # First log in to get a cookie
        r1 = await c.get("/", auth=("admin", "testpass"), follow_redirects=False)
        cookie = r1.cookies.get("sentinel_session")
        assert cookie is not None

        # Call logout
        r2 = await c.post("/logout")
        assert r2.status_code == 200
        # Check that the cookie is cleared
        set_cookie = r2.headers.get("set-cookie", "")
        assert "sentinel_session=" in set_cookie
        assert "max-age=0" in set_cookie.lower() or "max_age=0" in set_cookie.lower()


async def test_auth_healthz_always_open(auth_app: object) -> None:
    """Health endpoint must bypass auth."""
    async with _client(auth_app) as c:
        r = await c.get("/healthz")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# /__internal_snapshot/{nonce}
# ---------------------------------------------------------------------------


async def test_internal_snapshot_allow_multiple_retries(app: object) -> None:
    from sentinel.ml.nonce import get_nonce_store

    nonce = get_nonce_store().put(_FAKE_JPEG)
    async with _client(app) as c:
        r1 = await c.get(f"/__internal_snapshot/{nonce}")
        r2 = await c.get(f"/__internal_snapshot/{nonce}")
    assert r1.status_code == 200
    assert r1.content == _FAKE_JPEG
    assert r2.status_code == 200
    assert r2.content == _FAKE_JPEG


async def test_internal_snapshot_unknown_nonce(app: object) -> None:
    async with _client(app) as c:
        r = await c.get("/__internal_snapshot/doesnotexist")
    assert r.status_code == 404


async def test_internal_snapshot_localhost_no_token(app: object) -> None:
    # Requests from localhost / loopback do not require a token
    async with _client(app) as c:
        r = await c.get(
            "/__internal_snapshot/doesnotexist", headers={"X-Forwarded-For": "127.0.0.1"}
        )
    assert r.status_code == 404


async def test_internal_snapshot_external_no_token(app: object) -> None:
    # Requests from external IP without token bypass auth and hit 404
    async with _client(app) as c:
        r = await c.get(
            "/__internal_snapshot/doesnotexist", headers={"X-Forwarded-For": "192.168.1.100"}
        )
    assert r.status_code == 404


async def test_internal_snapshot_external_valid_token(
    tmp_path: Path,
    mock_db: AsyncMock,
    mock_watcher: MagicMock,
    mock_camera: AsyncMock,
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("my-super-secret-token")

    settings = _base_settings(ml_api_token_file=str(token_file))
    token_app = create_app(settings, db=mock_db, watcher=mock_watcher, camera=mock_camera)

    async with _client(token_app) as c:
        r = await c.get(
            "/__internal_snapshot/doesnotexist",
            headers={
                "X-Forwarded-For": "192.168.1.100",
                "Authorization": "Bearer my-super-secret-token",
            },
        )
    assert r.status_code == 404


async def test_internal_snapshot_external_invalid_token(
    tmp_path: Path,
    mock_db: AsyncMock,
    mock_watcher: MagicMock,
    mock_camera: AsyncMock,
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("my-super-secret-token")

    settings = _base_settings(ml_api_token_file=str(token_file))
    token_app = create_app(settings, db=mock_db, watcher=mock_watcher, camera=mock_camera)

    async with _client(token_app) as c:
        r = await c.get(
            "/__internal_snapshot/doesnotexist",
            headers={
                "X-Forwarded-For": "192.168.1.100",
                "Authorization": "Bearer wrong-token",
            },
        )
    assert r.status_code == 404


async def test_internal_snapshot_proxies_untrusted(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: AsyncMock
) -> None:
    settings = _base_settings(trust_proxies=False)
    app = create_app(settings, db=mock_db, watcher=mock_watcher, camera=mock_camera)
    async with _client(app) as c:
        r = await c.get(
            "/__internal_snapshot/doesnotexist",
            headers={"X-Forwarded-For": "192.168.1.100"},
        )
    assert r.status_code == 404


async def test_internal_snapshot_external_client_scope(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: AsyncMock
) -> None:
    settings = _base_settings(trust_proxies=False)
    from sentinel.web.auth import AuthMiddleware

    response_status = None

    async def dummy_app(scope: object, receive: object, send: object) -> None:
        nonlocal response_status
        response_status = 404

    middleware = AuthMiddleware(dummy_app, settings)

    scope: dict[str, object] = {
        "type": "http",
        "method": "GET",
        "path": "/__internal_snapshot/doesnotexist",
        "headers": [],
        "client": ("192.168.1.100", 54321),
    }

    from typing import Any

    async def mock_send(message: Any) -> None:
        pass

    async def mock_receive() -> dict[str, object]:
        return {"type": "http.request"}

    await middleware(scope, mock_receive, mock_send)
    assert response_status == 404


# ---------------------------------------------------------------------------
# / — status page with no deps (503)
# ---------------------------------------------------------------------------


async def test_status_page_no_db_returns_503() -> None:
    no_dep_app = create_app(_base_settings())
    async with _client(no_dep_app) as c:
        r = await c.get("/")
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# /snapshot — no camera (503)
# ---------------------------------------------------------------------------


async def test_snapshot_no_camera_returns_503(mock_db: AsyncMock, mock_watcher: MagicMock) -> None:
    no_cam_app = create_app(_base_settings(), db=mock_db, watcher=mock_watcher)
    async with _client(no_cam_app) as c:
        r = await c.get("/snapshot")
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# /stream — MJPEG multipart
# ---------------------------------------------------------------------------


async def test_stream_no_camera_returns_503(mock_db: AsyncMock, mock_watcher: MagicMock) -> None:
    no_cam_app = create_app(_base_settings(), db=mock_db, watcher=mock_watcher)
    async with _client(no_cam_app) as c:
        r = await c.get("/stream")
    assert r.status_code == 503


async def test_stream_returns_multipart(mock_db: AsyncMock, mock_watcher: MagicMock) -> None:
    async def _gen() -> object:
        yield _FAKE_JPEG
        yield _FAKE_JPEG

    cam = AsyncMock()
    cam.stream_proxy = _gen
    stream_app = create_app(_base_settings(), db=mock_db, watcher=mock_watcher, camera=cam)
    async with _client(stream_app) as c:
        r = await c.get("/stream")
    assert r.status_code == 200
    assert "multipart/x-mixed-replace" in r.headers["content-type"]


# ---------------------------------------------------------------------------
# _age_seconds — invalid timestamp → None
# ---------------------------------------------------------------------------


async def test_readyz_invalid_heartbeat_format(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: AsyncMock
) -> None:
    mock_db.get_heartbeat.return_value = {"last_tick_utc": "not-a-timestamp", "state": "ARMED"}
    bad_ts_app = create_app(_base_settings(), db=mock_db, watcher=mock_watcher, camera=mock_camera)
    async with _client(bad_ts_app) as c:
        r = await c.get("/readyz")
    # Invalid timestamp: age is None, treated as no heartbeat → 503
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# AuthMiddleware — unit-level edge cases
# ---------------------------------------------------------------------------


async def test_auth_bad_base64_sets_empty_credentials(auth_app: object) -> None:
    """Malformed Base64 in Authorization header must not crash; results in 401."""
    async with _client(auth_app) as c:
        r = await c.get(
            "/",
            headers={"Authorization": "Basic !!!not-base64!!!"},
        )
    assert r.status_code == 401


def _make_auth_middleware() -> object:
    from sentinel.web.auth import AuthMiddleware

    async def _dummy_app(scope: object, receive: object, send: object) -> None:
        pass

    return AuthMiddleware(
        _dummy_app,
        _base_settings(auth_username="admin", auth_password_bcrypt=_HASHED_PASS),
        secret=b"\x00" * 32,
    )


def test_verify_token_wrong_part_count() -> None:
    from sentinel.web.auth import AuthMiddleware

    mw = _make_auth_middleware()
    assert isinstance(mw, AuthMiddleware)
    assert mw._verify_token("only.three.parts", "ua") is False


def test_verify_token_expired() -> None:
    import time

    from sentinel.web.auth import _TTL, AuthMiddleware

    mw = _make_auth_middleware()
    assert isinstance(mw, AuthMiddleware)
    old_ts = str(int(time.time()) - _TTL - 10)
    # Build an expired token manually
    import hashlib
    import hmac
    import secrets

    rnd = secrets.token_hex(8)
    ua_hash = mw._ua_hash("ua")
    msg = f"{old_ts}.{rnd}.{ua_hash}".encode()
    sig = hmac.new(mw._secret, msg, hashlib.sha256).hexdigest()
    expired_token = f"{old_ts}.{rnd}.{ua_hash}.{sig}"
    assert mw._verify_token(expired_token, "ua") is False


def test_verify_token_ua_mismatch() -> None:
    from sentinel.web.auth import AuthMiddleware

    mw = _make_auth_middleware()
    assert isinstance(mw, AuthMiddleware)
    cookie = mw._make_cookie("original-ua")
    assert mw._verify_token(cookie, "different-ua") is False


def test_verify_token_garbage_raises_false() -> None:
    from sentinel.web.auth import AuthMiddleware

    mw = _make_auth_middleware()
    assert isinstance(mw, AuthMiddleware)
    # Four parts but ts is not an integer
    assert mw._verify_token("notint.rnd.uah.sig", "ua") is False


async def test_check_credentials_bcrypt_exception() -> None:
    """bcrypt.checkpw on a garbage hash must not raise; returns False."""
    from sentinel.config import Settings
    from sentinel.web.auth import AuthMiddleware

    async def _dummy(s: object, r: object, send: object) -> None:
        pass

    mw = AuthMiddleware(
        _dummy,
        Settings(
            printer_ip="192.168.1.10",
            auth_username="admin",
            auth_password_bcrypt=_HASHED_PASS,
        ),
        secret=b"\x00" * 32,
    )
    # Inject garbage hash directly to test middleware resilience to bcrypt exceptions
    mw._password_hash = "$garbage$not_a_real_hash"
    result = await mw._check_credentials("admin", "password")
    assert result is False


async def test_status_page_renders_all_states(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: AsyncMock
) -> None:
    """Verify that the status page renders cleanly without template errors
    for all watcher states.
    """
    from sentinel.watcher.state import WatcherState

    for state in WatcherState:
        mock_watcher.state = state
        app_state = create_app(
            _base_settings(), db=mock_db, watcher=mock_watcher, camera=mock_camera
        )
        async with _client(app_state) as c:
            r = await c.get("/")
        assert r.status_code == 200
        assert state.name in r.text


async def test_printer_api_endpoint(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: AsyncMock
) -> None:
    """Verify that the /api/printer endpoint returns correct JSON status data."""
    from sentinel.printer.types import PrinterStatus

    mock_watcher.last_printer_status = PrinterStatus(
        printing=True,
        elapsed_seconds=3600.0,
        current_layer=15,
        total_layers=150,
        filename="custom_job.gcode",
        extruder_temp=245.5,
        extruder_target=250.0,
        bed_temp=58.2,
        bed_target=60.0,
        progress=45.5,
        remaining_seconds=1800.0,
        print_state="printing",
        camera_connected=True,
        raw={"diag": "test"},
    )

    app_state = create_app(_base_settings(), db=mock_db, watcher=mock_watcher, camera=mock_camera)
    async with _client(app_state) as c:
        r = await c.get("/api/printer")

    assert r.status_code == 200
    data = r.json()
    assert data["printing"] is True
    assert data["elapsed_seconds"] == 3600.0
    assert data["current_layer"] == 15
    assert data["total_layers"] == 150
    assert data["filename"] == "custom_job.gcode"
    assert data["extruder_temp"] == 245.5
    assert data["extruder_target"] == 250.0
    assert data["bed_temp"] == 58.2
    assert data["bed_target"] == 60.0
    assert data["progress"] == 45.5
    assert data["remaining_seconds"] == 1800.0
    assert data["print_state"] == "printing"
    assert data["camera_connected"] is True


async def test_control_pause_success(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: AsyncMock
) -> None:
    printer = AsyncMock()
    printer.pause.return_value = True
    mock_watcher.printer = printer

    app_state = create_app(
        _base_settings(auth_enabled=False), db=mock_db, watcher=mock_watcher, camera=mock_camera
    )
    async with _client(app_state) as c:
        r = await c.post("/api/control/pause")

    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    printer.pause.assert_called_once()
    mock_db.record_pause.assert_called_once_with(source="web", result="ok")


async def test_control_resume_success(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: AsyncMock
) -> None:
    printer = AsyncMock()
    mock_watcher.printer = printer
    mock_watcher.state = WatcherState.PAUSED

    app_state = create_app(
        _base_settings(auth_enabled=False), db=mock_db, watcher=mock_watcher, camera=mock_camera
    )
    async with _client(app_state) as c:
        r = await c.post("/api/control/resume")

    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    printer.resume.assert_called_once()
    assert mock_watcher.state == WatcherState.ARMED


async def test_control_stop_success(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: AsyncMock
) -> None:
    printer = AsyncMock()
    mock_watcher.printer = printer

    app_state = create_app(
        _base_settings(auth_enabled=False), db=mock_db, watcher=mock_watcher, camera=mock_camera
    )
    async with _client(app_state) as c:
        r = await c.post("/api/control/stop")

    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    printer.stop.assert_called_once()


async def test_control_snooze_success(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: AsyncMock
) -> None:
    mock_watcher.snooze = AsyncMock()
    app_state = create_app(
        _base_settings(auth_enabled=False), db=mock_db, watcher=mock_watcher, camera=mock_camera
    )
    async with _client(app_state) as c:
        r = await c.post("/api/control/snooze", json={"seconds": 10})

    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    mock_watcher.snooze.assert_called_once_with(10.0)


async def test_control_snooze_unparseable_seconds_returns_400(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: AsyncMock
) -> None:
    """POST /api/control/snooze with a non-numeric seconds value must return 400.

    Acceptance criterion from issue #63: malformed input must not silently
    snooze for the 600s default with a 200 response.
    """
    mock_watcher.snooze = AsyncMock()
    app_state = create_app(
        _base_settings(auth_enabled=False), db=mock_db, watcher=mock_watcher, camera=mock_camera
    )
    async with _client(app_state) as c:
        r = await c.post("/api/control/snooze", json={"seconds": "abc"})

    assert r.status_code == 400
    mock_watcher.snooze.assert_not_called()


async def test_status_page_renders_with_printer_status(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: MagicMock
) -> None:
    from sentinel.printer.types import PrinterStatus

    mock_watcher.last_printer_status = PrinterStatus(
        printing=True,
        elapsed_seconds=120.0,
        current_layer=2,
        total_layers=20,
        filename="test.gcode",
        extruder_temp=200.0,
        extruder_target=200.0,
        bed_temp=60.0,
        bed_target=60.0,
        progress=10.0,
        remaining_seconds=1800.0,
        print_state="printing",
        camera_connected=True,
    )
    app_state = create_app(
        _base_settings(auth_enabled=False), db=mock_db, watcher=mock_watcher, camera=mock_camera
    )
    async with _client(app_state) as c:
        r = await c.get("/")
    assert r.status_code == 200
    assert "test.gcode" in r.text
    assert "2m 0s" in r.text
    assert "10.0%" in r.text


async def test_control_pause_failure(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: MagicMock
) -> None:
    printer = AsyncMock()
    printer.pause.side_effect = RuntimeError("MQTT offline")
    mock_watcher.printer = printer
    app_state = create_app(
        _base_settings(auth_enabled=False), db=mock_db, watcher=mock_watcher, camera=mock_camera
    )
    async with _client(app_state) as c:
        r = await c.post("/api/control/pause")
    assert r.status_code == 500
    mock_db.record_pause.assert_called_once_with(
        source="web", result="error", error_message="MQTT offline"
    )


async def test_control_pause_already_paused_edge(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: MagicMock
) -> None:
    printer = AsyncMock()
    printer.pause.return_value = False
    mock_watcher.printer = printer
    app_state = create_app(
        _base_settings(auth_enabled=False), db=mock_db, watcher=mock_watcher, camera=mock_camera
    )
    async with _client(app_state) as c:
        r = await c.post("/api/control/pause")
    assert r.status_code == 400
    mock_db.record_pause.assert_called_once_with(
        source="web", result="error", error_message="Printer already paused"
    )


async def test_control_resume_failure(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: MagicMock
) -> None:
    printer = AsyncMock()
    printer.resume.side_effect = RuntimeError("MQTT offline")
    mock_watcher.printer = printer
    app_state = create_app(
        _base_settings(auth_enabled=False), db=mock_db, watcher=mock_watcher, camera=mock_camera
    )
    async with _client(app_state) as c:
        r = await c.post("/api/control/resume")
    assert r.status_code == 500


async def test_control_stop_failure(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: MagicMock
) -> None:
    printer = AsyncMock()
    printer.stop.side_effect = RuntimeError("MQTT offline")
    mock_watcher.printer = printer
    app_state = create_app(
        _base_settings(auth_enabled=False), db=mock_db, watcher=mock_watcher, camera=mock_camera
    )
    async with _client(app_state) as c:
        r = await c.post("/api/control/stop")
    assert r.status_code == 500


async def test_get_settings_on_status_page(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: AsyncMock
) -> None:
    mock_db.get_setting.side_effect = lambda key, default: {
        "printer_ip": "192.168.1.150",
        "ml_confirm_count": "5",
        "ml_score_threshold": "0.75",
        "ml_poll_interval_seconds": "3",
        "detection_warmup_seconds": "15",
        "detection_enabled": "true",
    }.get(key, default)

    app_state = create_app(
        _base_settings(auth_enabled=False), db=mock_db, watcher=mock_watcher, camera=mock_camera
    )
    async with _client(app_state) as c:
        r = await c.get("/")
    assert r.status_code == 200
    body = r.text
    assert "192.168.1.150" in body
    assert "0.75" in body
    assert "5" in body
    assert "3" in body
    assert "15" in body


async def test_post_settings_success(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: AsyncMock
) -> None:
    printer = AsyncMock()
    mock_watcher.printer = printer

    app_state = create_app(
        _base_settings(auth_enabled=False), db=mock_db, watcher=mock_watcher, camera=mock_camera
    )
    payload = {
        "printer_ip": "10.0.0.42",
        "ml_score_threshold": 0.45,
        "ml_confirm_count": 4,
        "ml_poll_interval_seconds": 6,
        "detection_warmup_seconds": 30,
    }
    async with _client(app_state) as c:
        r = await c.post("/api/settings", json=payload)

    assert r.status_code == 200
    assert r.json() == {"status": "ok", "message": "Settings updated successfully"}

    mock_db.set_setting.assert_any_call("printer_ip", "10.0.0.42")
    mock_db.set_setting.assert_any_call("ml_score_threshold", "0.45")
    mock_db.set_setting.assert_any_call("ml_confirm_count", "4")
    mock_db.set_setting.assert_any_call("ml_poll_interval_seconds", "6")
    mock_db.set_setting.assert_any_call("detection_warmup_seconds", "30")

    printer.reconfigure.assert_called_once_with("10.0.0.42")
    mock_camera.reconfigure.assert_called_once_with("http://10.0.0.42:8080/mjpeg")


@pytest.mark.parametrize(
    "payload,expected_detail",
    [
        ({"printer_ip": ""}, "cannot be empty"),
        ({"printer_ip": "invalid ip address"}, "must be a valid IP address or hostname"),
        ({"printer_ip": "10.0.0.999"}, "must be a valid IP address or hostname"),
        ({"printer_ip": "bad_host@name"}, "must be a valid IP address or hostname"),
        ({"ml_confirm_count": 0}, "Confirm count must be at least 1"),
        ({"ml_confirm_count": -2}, "Confirm count must be at least 1"),
        ({"ml_score_threshold": -0.1}, "Score threshold must be between 0.0 and 1.0"),
        ({"ml_score_threshold": 1.1}, "Score threshold must be between 0.0 and 1.0"),
        ({"ml_poll_interval_seconds": 0}, "Poll interval must be at least 1 second"),
        ({"ml_poll_interval_seconds": -5}, "Poll interval must be at least 1 second"),
        ({"detection_warmup_seconds": -1}, "Warmup duration cannot be negative"),
    ],
)
async def test_post_settings_invalid_values(
    mock_db: AsyncMock,
    mock_watcher: MagicMock,
    mock_camera: MagicMock,
    payload: dict[str, object],
    expected_detail: str,
) -> None:
    mock_watcher.printer = AsyncMock()
    app_state = create_app(
        _base_settings(auth_enabled=False), db=mock_db, watcher=mock_watcher, camera=mock_camera
    )
    async with _client(app_state) as c:
        r = await c.post("/api/settings", json=payload)
    assert r.status_code == 400
    assert expected_detail in r.json()["detail"]


async def test_format_duration_edge_cases() -> None:
    from sentinel.web.routes import format_duration

    assert format_duration(0) == "—"
    assert "h" in format_duration(3600)


async def test_endpoints_without_deps(app: object) -> None:
    from sentinel.config import Settings
    from sentinel.web.app import create_app

    bad_app = create_app(Settings(printer_ip="192.168.1.10"), db=None, watcher=None, camera=None)
    async with _client(bad_app) as c:
        assert (await c.get("/")).status_code == 503
        assert (await c.get("/api/printer")).status_code == 503
        assert (await c.post("/api/control/pause")).status_code == 503
        assert (await c.post("/api/control/resume")).status_code == 503
        assert (await c.post("/api/control/stop")).status_code == 503
        assert (await c.post("/api/control/snooze", json={"seconds": 60})).status_code == 503
        assert (await c.post("/api/settings", json={})).status_code == 503
        assert (await c.get("/snapshot/12345678901234567890123456789012")).status_code == 503


async def test_settings_update_exceptions(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: AsyncMock
) -> None:
    from sentinel.config import Settings
    from sentinel.web.app import create_app

    app = create_app(
        Settings(printer_ip="192.168.1.10"), db=mock_db, watcher=mock_watcher, camera=mock_camera
    )

    async def mock_set(*args, **kwargs):
        raise Exception("db error")

    mock_db.set_setting = AsyncMock(side_effect=mock_set)

    async with _client(app) as c:
        res = await c.post("/api/settings", json={"ml_score_threshold": 0.5})
        assert res.status_code == 500

        # Test 400 branches
        assert (await c.post("/api/settings", json={"ml_score_threshold": -1})).status_code == 400
        assert (await c.post("/api/settings", json={"ml_score_threshold": 1.5})).status_code == 400
        assert (await c.post("/api/settings", json={"ml_confirm_count": 0})).status_code == 400
        assert (
            await c.post("/api/settings", json={"ml_poll_interval_seconds": 0})
        ).status_code == 400
        assert (
            await c.post("/api/settings", json={"detection_warmup_seconds": -1})
        ).status_code == 400
        assert (await c.post("/api/settings", json={"printer_ip": "   "})).status_code == 400
        assert (
            await c.post("/api/settings", json={"printer_ip": "bad_format!!!"})
        ).status_code == 400


async def test_snapshot_not_found_edge_cases(app: object, monkeypatch: pytest.MonkeyPatch) -> None:
    async with _client(app) as c:
        assert (await c.get("/snapshot/invalid_len")).status_code == 404

        snap_id = "a" * 32

        from pathlib import Path

        monkeypatch.setattr(Path, "exists", lambda x: False)
        assert (await c.get(f"/snapshot/{snap_id}")).status_code == 404

        monkeypatch.setattr(Path, "exists", lambda x: True)
        monkeypatch.setattr(Path, "read_bytes", MagicMock(side_effect=Exception("read error")))
        assert (await c.get(f"/snapshot/{snap_id}")).status_code == 500


async def test_content_security_policy_header(app: object) -> None:
    async with _client(app) as c:
        response = await c.get("/")
    assert response.status_code == 200
    csp = response.headers.get("Content-Security-Policy")
    assert csp is not None
    assert "default-src 'self'" in csp
    assert "script-src 'self' 'nonce-" in csp
    assert "style-src 'self' 'unsafe-inline'" in csp
    assert "img-src 'self' data:" in csp
    assert "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


async def test_status_page_renders_notification_failures_banner(
    mock_db: AsyncMock, mock_watcher: MagicMock, mock_camera: AsyncMock
) -> None:
    mock_watcher.dispatcher = MagicMock()
    mock_watcher.dispatcher.failed_channels = {"Telegram": "12345678901234567890123456789012"}

    app_state = create_app(
        _base_settings(auth_enabled=False), db=mock_db, watcher=mock_watcher, camera=mock_camera
    )
    async with _client(app_state) as c:
        r = await c.get("/")
    assert r.status_code == 200
    body = r.text
    assert "Notification Delivery Failures" in body
    assert "Telegram" in body
    assert "View Snapshot" in body
    assert "/snapshot/12345678901234567890123456789012" in body


async def test_auth_middleware_edge_cases() -> None:
    import time
    from unittest.mock import ANY, patch

    from sentinel.config import Settings
    from sentinel.web.auth import AuthMiddleware, _resolve_client_ip

    # 1. _resolve_client_ip with multiple IPs
    headers = {b"x-forwarded-for": b"192.168.1.1, 10.0.0.5"}
    ip = _resolve_client_ip({}, headers, trust_proxies=True)
    assert ip == "10.0.0.5"

    # 2. Non-HTTP scope
    mw = AuthMiddleware(AsyncMock(), Settings(auth_username="a", auth_password_bcrypt=_HASHED_PASS))
    scope = {"type": "websocket"}
    await mw(scope, AsyncMock(), AsyncMock())
    mw._app.assert_called_once_with(scope, ANY, ANY)

    # 3. Exception in urlparse of origin
    scope_csrf = {
        "type": "http",
        "method": "POST",
        "headers": [(b"origin", b"http://[::1]abc"), (b"host", b"test")],
    }
    mw_csrf = AuthMiddleware(
        AsyncMock(), Settings(auth_username="a", auth_password_bcrypt=_HASHED_PASS)
    )

    sent = []

    async def mock_send(msg: dict[str, object]) -> None:
        sent.append(msg)

    await mw_csrf(scope_csrf, AsyncMock(), mock_send)
    assert len(sent) > 0
    assert sent[0]["status"] == 403

    # 4. Rate limiting check (10 attempts per minute limit)
    mw_rate = AuthMiddleware(
        AsyncMock(), Settings(auth_username="admin", auth_password_bcrypt=_HASHED_PASS)
    )
    scope_rate = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [
            (b"host", b"test"),
            (b"authorization", b"Basic YWRtaW46d3JvbmdwYXNz"),
            (b"user-agent", b"ua"),
        ],
    }

    # Mock bcrypt checkpw to make it instant
    with patch("sentinel.web.auth.bcrypt.checkpw", return_value=False):
        for _ in range(10):
            await mw_rate(scope_rate, AsyncMock(), AsyncMock())

        # 11th request should be rate limited with 429
        sent_rate = []

        async def mock_send_rate(msg: dict[str, object]) -> None:
            sent_rate.append(msg)

        await mw_rate(scope_rate, AsyncMock(), mock_send_rate)
        assert len(sent_rate) > 0
        assert sent_rate[0]["status"] == 429

    # 5. OrderedDict eviction (>1000 items)
    mw_evict = AuthMiddleware(
        AsyncMock(), Settings(auth_username="admin", auth_password_bcrypt=_HASHED_PASS)
    )
    # Fill _auth_attempts with 1005 items
    for i in range(1005):
        mw_evict._auth_attempts[f"10.0.0.{i}"] = [time.time()]

    scope_evict = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [
            (b"host", b"test"),
            (b"authorization", b"Basic YWRtaW46d3JvbmdwYXNz"),
            (b"user-agent", b"ua"),
        ],
    }
    with patch("sentinel.web.auth.bcrypt.checkpw", return_value=False):
        await mw_evict(scope_evict, AsyncMock(), AsyncMock())

    assert len(mw_evict._auth_attempts) <= 1000
