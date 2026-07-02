"""Tests for sentinel/bot/runner.py.

BotRunner wraps the python-telegram-bot Application lifecycle.
All PTB objects are mocked so no network connection is needed.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

from sentinel.bot.runner import BotRunner
from sentinel.config import Settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SETTINGS_NO_TG = Settings(printer_ip="10.0.0.1", printer_access_code="test")
_SETTINGS_TG = Settings(
    printer_ip="10.0.0.1",
    printer_access_code="test",
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


def _make_ptb_app() -> tuple[MagicMock, MagicMock]:
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
    """Verify registration of all command, callback query, and keyboard handlers."""
    handler = _make_handler()
    app, builder = _make_ptb_app()

    with (
        patch("telegram.ext.Application") as mock_app_class,
        patch("telegram.ext.CommandHandler", side_effect=lambda name, fn: (name, fn)),
        patch("telegram.ext.CallbackQueryHandler", side_effect=lambda fn: fn),
        patch("telegram.ext.MessageHandler", side_effect=lambda filters, fn: (filters, fn)),
    ):
        mock_app_class.builder.return_value = builder
        runner = BotRunner(_SETTINGS_TG, handler)
        await runner.start()

    # add_handler should be called 15 times (9 commands + 1 callback + 5 message text filters)
    assert app.add_handler.call_count == 15


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


# ---------------------------------------------------------------------------
# BotRunner Supervisor Tests
# ---------------------------------------------------------------------------


async def test_supervisor_stops_old_app_before_restart() -> None:
    """Supervisor must call _real_stop on the crashed app before creating a new one."""
    import asyncio

    handler = _make_handler()

    # Build two distinct app mocks: the "crashed" one and the "fresh" one.
    crashed_app, _ = _make_ptb_app()
    crashed_app.updater.running = False  # already crashed — is_running() → False

    fresh_app, fresh_builder = _make_ptb_app()
    fresh_app.updater.running = True

    call_log: list[str] = []

    # Wrap crashed_app stop/shutdown to record ordering relative to build()
    original_stop = crashed_app.stop
    original_shutdown = crashed_app.shutdown

    async def tracked_stop(*a: object, **kw: object) -> None:
        call_log.append("old_app.stop")
        await original_stop(*a, **kw)

    async def tracked_shutdown(*a: object, **kw: object) -> None:
        call_log.append("old_app.shutdown")
        await original_shutdown(*a, **kw)

    crashed_app.stop = tracked_stop
    crashed_app.shutdown = tracked_shutdown

    build_call_count = 0

    def tracked_build() -> MagicMock:
        nonlocal build_call_count
        build_call_count += 1
        call_log.append("build")
        return fresh_app

    fresh_builder.build.side_effect = tracked_build

    original_sleep = asyncio.sleep

    async def mock_sleep(delay: float) -> None:
        await original_sleep(0)

    with (
        patch("telegram.ext.Application") as mock_app_class,
        patch("telegram.ext.CommandHandler"),
        patch("telegram.ext.CallbackQueryHandler"),
        patch("asyncio.sleep", side_effect=mock_sleep),
    ):
        mock_app_class.builder.return_value = fresh_builder
        runner = BotRunner(_SETTINGS_TG, handler)

        # Inject a pre-crashed app directly — skip _real_start's builder path.
        runner._app = crashed_app

        # Kick the supervisor loop for one restart cycle then stop.
        runner._running = True
        supervisor = asyncio.create_task(runner._supervisor_loop())

        for _ in range(100):
            await asyncio.sleep(0)
            if build_call_count >= 1:
                break

        runner._running = False
        supervisor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await supervisor

    # The old app must have been stopped before a new one was built.
    assert "old_app.stop" in call_log, f"old_app.stop not called; log={call_log}"
    assert "old_app.shutdown" in call_log, f"old_app.shutdown not called; log={call_log}"
    build_idx = call_log.index("build")
    stop_idx = call_log.index("old_app.stop")
    assert stop_idx < build_idx, f"old_app.stop must happen before build(); log={call_log}"


async def test_bot_runner_supervisor_restart_and_alert() -> None:
    import asyncio

    handler = _make_handler()
    app, builder = _make_ptb_app()
    dispatcher = MagicMock()

    # Initially, the bot is running
    app.updater.running = True

    # Use a mock sleep that yields but does not delay
    original_sleep = asyncio.sleep

    async def mock_sleep(delay: float) -> None:
        await original_sleep(0)

    with (
        patch("telegram.ext.Application") as mock_app_class,
        patch("telegram.ext.CommandHandler"),
        patch("telegram.ext.CallbackQueryHandler"),
        patch("asyncio.sleep", side_effect=mock_sleep),
    ):
        mock_app_class.builder.return_value = builder
        runner = BotRunner(_SETTINGS_TG, handler, dispatcher)

        # Start bot and supervisor
        await runner.start()
        await asyncio.sleep(0.01)

        assert runner.is_running() is True
        assert runner.crash_count == 0

        # Simulate a crash
        app.updater.running = False

        # Wait a few event loop iterations for the supervisor loop to tick
        for _ in range(50):
            await asyncio.sleep(0.001)
            if runner.crash_count > 0:
                break

        assert runner.crash_count >= 1
        await runner.stop()


async def test_bot_runner_supervisor_recovery_alert_on_first_crash() -> None:
    """Regression test: a recovery alert must fire after the bot's FIRST-EVER
    crash+restart cycle (crash_count == 1), not just from the second crash onward.
    """
    import asyncio

    handler = _make_handler()
    app, builder = _make_ptb_app()
    dispatcher = MagicMock()

    app.updater.running = True

    # Real PTB flips updater.running back on when polling (re)starts — mimic that
    # so the simulated restart actually looks "recovered" to is_running().
    async def start_polling_side_effect(*a: object, **kw: object) -> None:
        app.updater.running = True

    app.updater.start_polling = AsyncMock(side_effect=start_polling_side_effect)

    original_sleep = asyncio.sleep

    async def mock_sleep(delay: float) -> None:
        await original_sleep(0)

    with (
        patch("telegram.ext.Application") as mock_app_class,
        patch("telegram.ext.CommandHandler"),
        patch("telegram.ext.CallbackQueryHandler"),
        patch("asyncio.sleep", side_effect=mock_sleep),
    ):
        mock_app_class.builder.return_value = builder
        runner = BotRunner(_SETTINGS_TG, handler, dispatcher)

        await runner.start()
        await asyncio.sleep(0.01)
        assert runner.is_running() is True

        # Simulate the bot's first-ever crash.
        app.updater.running = False

        # Give the supervisor loop enough ticks to detect the crash, back off,
        # and successfully restart.
        for _ in range(500):
            await asyncio.sleep(0.001)
            if runner.crash_count >= 1 and runner.is_running():
                break

        assert runner.crash_count == 1
        assert runner.is_running() is True

        await runner.stop()

    messages = [call.args[0] for call in dispatcher.dispatch_text.call_args_list]
    assert any("crashed" in m for m in messages), f"expected crash alert, got: {messages}"
    assert any("recovered" in m for m in messages), (
        f"expected recovery alert after first crash, got: {messages}"
    )
