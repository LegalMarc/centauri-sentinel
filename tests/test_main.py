"""Tests for sentinel/__main__.py CLI overrides."""

from __future__ import annotations

import argparse
import os
import stat
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sentinel.__main__ import _hash_password, _run

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_main_cli_overrides() -> None:
    args = argparse.Namespace(host="127.0.0.9", port=9999)

    mock_db = AsyncMock()
    mock_db.get_auth_secret.return_value = b"fake_secret"
    mock_db.get_setting.return_value = "true"

    mock_watcher = MagicMock()
    mock_watcher.run_forever = AsyncMock()

    mock_printer = MagicMock()
    mock_printer.close = AsyncMock()

    mock_ml = MagicMock()
    mock_ml.close = AsyncMock()

    mock_camera = MagicMock()
    mock_camera.close = AsyncMock()

    orig_env = dict(os.environ)
    try:
        with (
            patch("sentinel.db.repo.Database", return_value=mock_db),
            patch("sentinel.camera.mjpeg.MjpegGrabber", return_value=mock_camera),
            patch("sentinel.printer.client.PrinterClient", return_value=mock_printer),
            patch("sentinel.ml.client.MlClient", return_value=mock_ml),
            patch("sentinel.watcher.loop.WatcherLoop", return_value=mock_watcher),
            patch("sentinel.web.app.create_app"),
            patch("sentinel.safety.check_external_bind"),
            patch("uvicorn.Config") as mock_config,
            patch("uvicorn.Server") as mock_server_class,
        ):
            mock_server = MagicMock()
            mock_server.serve = AsyncMock()
            mock_server_class.return_value = mock_server

            await _run(args)

            mock_config.assert_called_once()
            call_kwargs = mock_config.call_args[1]
            assert call_kwargs["host"] == "127.0.0.9"
            assert call_kwargs["port"] == 9999
    finally:
        os.environ.clear()
        os.environ.update(orig_env)
        from sentinel.config import get_settings

        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# hash-password CLI command (Pattern 3)
# ---------------------------------------------------------------------------


def test_hash_password_prints_both_forms(capsys: pytest.CaptureFixture[str]) -> None:
    import bcrypt

    args = argparse.Namespace(password="hunter2", file=None, rounds=4)
    _hash_password(args)

    out = capsys.readouterr().out
    # The pre-escaped .env form must be present and correctly escaped.
    assert "AUTH_PASSWORD_BCRYPT=$$2b$$04$$" in out
    assert "AUTH_PASSWORD_BCRYPT_FILE=" in out
    # The raw hash line must verify against the password.
    raw_line = next(line.strip() for line in out.splitlines() if line.strip().startswith("$2b$"))
    assert bcrypt.checkpw(b"hunter2", raw_line.encode())


def test_hash_password_writes_file_with_restricted_perms(tmp_path: Path) -> None:
    import bcrypt

    target = tmp_path / "auth_hash"
    args = argparse.Namespace(password="hunter2", file=str(target), rounds=4)
    _hash_password(args)

    content = target.read_text().strip()
    assert bcrypt.checkpw(b"hunter2", content.encode())
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_hash_password_prompts_when_password_omitted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import bcrypt

    args = argparse.Namespace(password=None, file=None, rounds=4)
    with patch("getpass.getpass", side_effect=["hunter2", "hunter2"]):
        _hash_password(args)

    out = capsys.readouterr().out
    raw_line = next(line.strip() for line in out.splitlines() if line.strip().startswith("$2b$"))
    assert bcrypt.checkpw(b"hunter2", raw_line.encode())


def test_hash_password_mismatch_exits(capsys: pytest.CaptureFixture[str]) -> None:
    args = argparse.Namespace(password=None, file=None, rounds=4)
    with (
        patch("getpass.getpass", side_effect=["hunter2", "different"]),
        pytest.raises(SystemExit) as exc,
    ):
        _hash_password(args)
    assert exc.value.code == 1
    assert "do not match" in capsys.readouterr().err


def test_hash_password_empty_exits(capsys: pytest.CaptureFixture[str]) -> None:
    args = argparse.Namespace(password="", file=None, rounds=4)
    with pytest.raises(SystemExit) as exc:
        _hash_password(args)
    assert exc.value.code == 1
    assert "empty" in capsys.readouterr().err
