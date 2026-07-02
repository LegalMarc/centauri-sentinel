"""Tests for sentinel/safety.py — 100% branch coverage required."""

from __future__ import annotations

import logging

import pytest

from sentinel.config import Settings
from sentinel.safety import check_external_bind


def test_localhost_always_safe() -> None:
    s = Settings(bind_host="127.0.0.1")
    check_external_bind(s)  # must not raise


def test_external_no_auth_no_override_raises() -> None:
    s = Settings(bind_host="0.0.0.0", external_bind_allowed=False)
    with pytest.raises(RuntimeError, match="Refusing to bind"):
        check_external_bind(s)


def test_external_with_auth_warns(caplog: pytest.LogCaptureFixture) -> None:
    s = Settings(
        bind_host="0.0.0.0",
        auth_username="admin",
        auth_password_bcrypt="$2b$12$dummyhash",
        external_bind_allowed=False,
    )
    with caplog.at_level(logging.WARNING, logger="sentinel.safety"):
        check_external_bind(s)
    assert "external interface" in caplog.text


def test_external_with_override_warns(caplog: pytest.LogCaptureFixture) -> None:
    s = Settings(bind_host="0.0.0.0", external_bind_allowed=True)
    with caplog.at_level(logging.WARNING, logger="sentinel.safety"):
        check_external_bind(s)
    assert "external interface" in caplog.text


def test_external_with_auth_and_override_warns(caplog: pytest.LogCaptureFixture) -> None:
    s = Settings(
        bind_host="0.0.0.0",
        auth_username="admin",
        auth_password_bcrypt="$2b$12$dummyhash",
        external_bind_allowed=True,
    )
    with caplog.at_level(logging.WARNING, logger="sentinel.safety"):
        check_external_bind(s)
    assert "external interface" in caplog.text


def test_error_message_includes_host_and_port() -> None:
    s = Settings(bind_host="192.168.1.5", bind_port=9000, external_bind_allowed=False)
    with pytest.raises(RuntimeError, match=r"192\.168\.1\.5:9000"):
        check_external_bind(s)
