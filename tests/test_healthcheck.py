"""Tests for sentinel/healthcheck.py.

Covers the main() probe: 200 → exit(0), non-200 → exit(1), exception → exit(1).
"""

from __future__ import annotations

import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from sentinel.healthcheck import main


def _make_response(status: int) -> MagicMock:
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.status = status
    return resp


# ---------------------------------------------------------------------------
# Exit 0 on HTTP 200
# ---------------------------------------------------------------------------


def test_healthcheck_exits_0_on_200() -> None:
    resp = _make_response(200)
    with (
        patch("sentinel.healthcheck.urllib.request.urlopen", return_value=resp),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()
    assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# Exit 1 on non-200
# ---------------------------------------------------------------------------


def test_healthcheck_exits_1_on_503() -> None:
    resp = _make_response(503)
    with (
        patch("sentinel.healthcheck.urllib.request.urlopen", return_value=resp),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Exit 1 on connection error
# ---------------------------------------------------------------------------


def test_healthcheck_exits_1_on_connection_error() -> None:
    with (
        patch(
            "sentinel.healthcheck.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Exit 1 on timeout
# ---------------------------------------------------------------------------


def test_healthcheck_exits_1_on_timeout() -> None:
    with (
        patch(
            "sentinel.healthcheck.urllib.request.urlopen",
            side_effect=TimeoutError("timed out"),
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Respects BIND_PORT env var
# ---------------------------------------------------------------------------


def test_healthcheck_uses_bind_port_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIND_PORT", "9999")
    resp = _make_response(200)
    captured_url: list[str] = []

    def _fake_urlopen(url: str, timeout: int) -> MagicMock:
        captured_url.append(url)
        return resp

    with (
        patch("sentinel.healthcheck.urllib.request.urlopen", side_effect=_fake_urlopen),
        pytest.raises(SystemExit),
    ):
        main()

    assert "9999" in captured_url[0]
