"""Tests for sentinel/__main__.py CLI overrides."""

from __future__ import annotations

import argparse
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sentinel.__main__ import _run


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
        with patch("sentinel.db.repo.Database", return_value=mock_db), \
             patch("sentinel.camera.mjpeg.MjpegGrabber", return_value=mock_camera), \
             patch("sentinel.printer.client.PrinterClient", return_value=mock_printer), \
             patch("sentinel.ml.client.MlClient", return_value=mock_ml), \
             patch("sentinel.watcher.loop.WatcherLoop", return_value=mock_watcher), \
             patch("sentinel.web.app.create_app"), \
             patch("sentinel.safety.check_external_bind"), \
             patch("uvicorn.Config") as mock_config, \
             patch("uvicorn.Server") as mock_server_class:

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
