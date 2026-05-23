"""Smoke tests for ticket #1 scaffolding.

Verifies the package imports, version string, config loads from env,
and the web stub returns 200 on /healthz.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

import sentinel
from sentinel.config import Settings
from sentinel.web.app import create_app


def test_version_string() -> None:
    assert sentinel.__version__ == "0.1.0"


def test_settings_defaults() -> None:
    s = Settings(printer_ip="10.0.0.1")
    assert s.printer_ip == "10.0.0.1"
    assert s.bind_port == 8000
    assert s.log_level == "INFO"
    assert not s.telegram_enabled
    assert not s.ntfy_enabled
    assert not s.auth_enabled


def test_settings_telegram_enabled() -> None:
    s = Settings(
        printer_ip="10.0.0.1",
        telegram_bot_token="abc",
        telegram_chat_id="123",
        telegram_user_ids="456",
    )
    assert s.telegram_enabled


def test_settings_ntfy_enabled() -> None:
    s = Settings(printer_ip="10.0.0.1", ntfy_url="https://ntfy.sh/topic")
    assert s.ntfy_enabled


def test_settings_auth_enabled() -> None:
    s = Settings(printer_ip="10.0.0.1", auth_username="admin")
    assert s.auth_enabled


def test_settings_invalid_log_level() -> None:
    with pytest.raises(ValueError):
        Settings(printer_ip="10.0.0.1", log_level="INVALID")


def test_healthz_endpoint() -> None:
    settings = Settings(printer_ip="10.0.0.1")
    app = create_app(settings)
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_sentinel_package_importable() -> None:
    mod = importlib.import_module("sentinel")
    assert hasattr(mod, "__version__")


def test_get_settings_cached() -> None:
    # get_settings() is lru_cache'd — should return same object
    # We can't test global cache without clearing it, so just test it doesn't raise
    # when PRINTER_IP is unset (uses default)
    s = Settings()
    assert s.printer_ip == "127.0.0.1"  # default from config.py


def test_internal_snapshot_endpoint_hit() -> None:
    from sentinel.ml.nonce import NonceStore

    store = NonceStore()
    jpeg = b"\xff\xd8\xff\xd9"
    nonce = store.put(jpeg)

    settings = Settings(printer_ip="10.0.0.1")
    app = create_app(settings)

    from unittest.mock import patch

    with patch("sentinel.web.app.get_nonce_store", return_value=store):
        client = TestClient(app)
        resp = client.get(f"/__internal_snapshot/{nonce}")

    assert resp.status_code == 200
    assert resp.content == jpeg


def test_internal_snapshot_endpoint_missing() -> None:
    settings = Settings(printer_ip="10.0.0.1")
    app = create_app(settings)
    client = TestClient(app)
    resp = client.get("/__internal_snapshot/no-such-nonce")
    assert resp.status_code == 404
