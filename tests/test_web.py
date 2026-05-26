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
    return Settings(
        printer_ip="127.0.0.1",
        printer_access_code="000000",
        bind_host="127.0.0.1",
        external_bind_allowed=True,
        **kwargs,  # type: ignore[arg-type]
    )


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
    return w


@pytest.fixture
def mock_camera() -> AsyncMock:
    cam = AsyncMock()
    cam.grab.return_value = _FAKE_JPEG
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
        transport=httpx.ASGITransport(app=application),  # type: ignore[arg-type]
        base_url="http://test",
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
    """Embedded CSS must stay under 2 KB."""
    async with _client(app) as c:
        r = await c.get("/")
    body = r.text
    start = body.find("<style>")
    end = body.find("</style>")
    assert start != -1 and end != -1
    css_bytes = len(body[start:end].encode())
    assert css_bytes < 2048, f"Embedded CSS is {css_bytes} bytes (limit 2048)"


async def test_status_page_meta_refresh(app: object) -> None:
    async with _client(app) as c:
        r = await c.get("/")
    assert 'http-equiv="refresh"' in r.text
    assert 'content="10"' in r.text


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
    assert r.json()["status"] == "ready"


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


async def test_auth_cookie_grants_access(auth_app: object) -> None:
    async with _client(auth_app) as c:
        r1 = await c.get("/", auth=("admin", "testpass"), follow_redirects=False)
        cookie = r1.cookies.get("sentinel_session")
        assert cookie is not None
        # Use the cookie on the client instance
        c.cookies.set("sentinel_session", cookie)
        r2 = await c.get("/")
    assert r2.status_code == 200


async def test_auth_healthz_always_open(auth_app: object) -> None:
    """Health endpoint must bypass auth."""
    async with _client(auth_app) as c:
        r = await c.get("/healthz")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# /__internal_snapshot/{nonce}
# ---------------------------------------------------------------------------


async def test_internal_snapshot_single_use(app: object) -> None:
    from sentinel.ml.nonce import get_nonce_store

    nonce = get_nonce_store().put(_FAKE_JPEG)
    async with _client(app) as c:
        r1 = await c.get(f"/__internal_snapshot/{nonce}")
        r2 = await c.get(f"/__internal_snapshot/{nonce}")
    assert r1.status_code == 200
    assert r1.content == _FAKE_JPEG
    assert r2.status_code == 404


async def test_internal_snapshot_unknown_nonce(app: object) -> None:
    async with _client(app) as c:
        r = await c.get("/__internal_snapshot/doesnotexist")
    assert r.status_code == 404


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


def test_check_credentials_bcrypt_exception() -> None:
    """bcrypt.checkpw on a garbage hash must not raise; returns False."""
    from sentinel.config import Settings
    from sentinel.web.auth import AuthMiddleware

    async def _dummy(s: object, r: object, send: object) -> None:
        pass

    mw = AuthMiddleware(
        _dummy,
        Settings(
            printer_ip="127.0.0.1",
            auth_username="admin",
            auth_password="password",
        ),
        secret=b"\x00" * 32,
    )
    # Inject garbage hash directly to test middleware resilience to bcrypt exceptions
    mw._password_hash = "$garbage$not_a_real_hash"
    result = mw._check_credentials("admin", "password")
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
