"""Tests for sentinel/bot/runner.py.

BotRunner wraps the python-telegram-bot Application lifecycle.
All PTB objects are mocked so no network connection is needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from sentinel.bot.runner import BotRunner
from sentinel.config import Settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SETTINGS_NO_TG = Settings(printer_ip="10.0.0.1")
_SETTINGS_TG = Settings(
    printer_ip="10.0.0.1",
    telegram_bot_token="123:token",
    telegram_chat_id="-100",
    telegram_user_ids="42",
)


def _make_handler() -> MagicMock:
    h = MagicMock()
    for cmd in (
        "cmd_help",
        "cmd_status",
        "cmd_snapshot",
        "cmd_pause",
        "cmd_resume",
        "cmd_stop",
        "cmd_confirm",
        "cmd_enable",
        "cmd_disable",
        "handle_callback",
    ):
        setattr(h, cmd, AsyncMock())
    return h


def _make_ptb_app() -> MagicMock:
    """Return a minimal mock of a PTB Application."""
    updater = MagicMock()
    updater.running = True
    updater.stop = AsyncMock()
    updater.start_polling = AsyncMock()

    app = MagicMock()
    app.updater = updater
    app.add_handler = MagicMock()
    app.initialize = AsyncMock()
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.shutdown = AsyncMock()

    builder = MagicMock()
    builder.token.return_value = builder
    builder.build.return_value = app
    return app, builder


# ---------------------------------------------------------------------------
# Disabled bot — no-op
# ---------------------------------------------------------------------------


async def test_bot_runner_disabled_start_is_noop() -> None:
    handler = _make_handler()
    runner = BotRunner(_SETTINGS_NO_TG, handler)
    # Should not raise, should not touch PTB
    await runner.start()
    assert runner._app is None


async def test_bot_runner_disabled_stop_is_noop() -> None:
    handler = _make_handler()
    runner = BotRunner(_SETTINGS_NO_TG, handler)
    await runner.stop()  # _app is None → must be a no-op


# ---------------------------------------------------------------------------
# Enabled bot — lifecycle
# ---------------------------------------------------------------------------


async def test_bot_runner_start_registers_handlers() -> None:
    handler = _make_handler()
    app, builder = _make_ptb_app()

    with (
        patch("telegram.ext.Application") as mock_app_class,
        patch("telegram.ext.CommandHandler"),
        patch("telegram.ext.CallbackQueryHandler"),
    ):
        mock_app_class.builder.return_value = builder
        runner = BotRunner(_SETTINGS_TG, handler)
        await runner.start()

    assert runner._app is app
    app.initialize.assert_called_once()
    app.start.assert_called_once()
    app.updater.start_polling.assert_called_once()


async def test_bot_runner_stop_shuts_down() -> None:
    handler = _make_handler()
    app, builder = _make_ptb_app()

    with (
        patch("telegram.ext.Application") as mock_app_class,
        patch("telegram.ext.CommandHandler"),
        patch("telegram.ext.CallbackQueryHandler"),
    ):
        mock_app_class.builder.return_value = builder
        runner = BotRunner(_SETTINGS_TG, handler)
        await runner.start()
        await runner.stop()

    app.updater.stop.assert_called_once()
    app.stop.assert_called_once()
    app.shutdown.assert_called_once()
    assert runner._app is None


async def test_bot_runner_stop_when_updater_not_running() -> None:
    """stop() must handle updater.running == False gracefully."""
    handler = _make_handler()
    app, builder = _make_ptb_app()
    app.updater.running = False  # updater already stopped

    with (
        patch("telegram.ext.Application") as mock_app_class,
        patch("telegram.ext.CommandHandler"),
        patch("telegram.ext.CallbackQueryHandler"),
    ):
        mock_app_class.builder.return_value = builder
        runner = BotRunner(_SETTINGS_TG, handler)
        await runner.start()
        await runner.stop()

    app.updater.stop.assert_not_called()
    app.stop.assert_called_once()


async def test_bot_runner_stop_when_not_started() -> None:
    """stop() with _app=None (e.g. start failed) must be a no-op."""
    handler = _make_handler()
    runner = BotRunner(_SETTINGS_TG, handler)
    await runner.stop()  # must not raise


async def test_bot_runner_all_handlers_added() -> None:
    """Verify all 9 command handlers + callback query handler are registered."""
    handler = _make_handler()
    app, builder = _make_ptb_app()

    with (
        patch("telegram.ext.Application") as mock_app_class,
        patch("telegram.ext.CommandHandler", side_effect=lambda name, fn: (name, fn)),
        patch("telegram.ext.CallbackQueryHandler", side_effect=lambda fn: fn),
    ):
        mock_app_class.builder.return_value = builder
        runner = BotRunner(_SETTINGS_TG, handler)
        await runner.start()

    # add_handler should be called 10 times (9 commands + 1 callback)
    assert app.add_handler.call_count == 10


async def test_bot_runner_stop_times_out() -> None:
    """stop() must handle TimeoutError during PTB shutdown gracefully."""
    import asyncio

    handler = _make_handler()
    app, builder = _make_ptb_app()

    # Simulate updater.stop hanging or taking a long time
    async def slow_stop() -> None:
        await asyncio.sleep(10.0)

    app.updater.stop = slow_stop

    with (
        patch("telegram.ext.Application") as mock_app_class,
        patch("telegram.ext.CommandHandler"),
        patch("telegram.ext.CallbackQueryHandler"),
    ):
        mock_app_class.builder.return_value = builder
        runner = BotRunner(_SETTINGS_TG, handler)
        await runner.start()

        # We patch the timeout inside the runner to be very short so the test runs fast
        with patch("asyncio.timeout", return_value=asyncio.timeout(0.01)):
            await runner.stop()

    assert runner._app is None
