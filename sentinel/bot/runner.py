"""Telegram bot long-polling runner.

Lifecycle:
  await runner.start()   — initialize Application and begin polling
  await runner.stop()    — drain pending updates and shut down cleanly

Disabled gracefully when ``telegram_enabled`` is False.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncio

    from sentinel.bot.commands import BotCommandHandler
    from sentinel.config import Settings

logger = logging.getLogger(__name__)


class BotRunner:
    """Manages the python-telegram-bot Application lifecycle with a supervisor."""

    def __init__(
        self,
        settings: Settings,
        handler: BotCommandHandler,
        dispatcher: Any = None,
    ) -> None:
        self._enabled = settings.telegram_enabled
        self._token = settings.telegram_bot_token or ""
        self._handler = handler
        self._dispatcher = dispatcher
        self._app: Any = None  # telegram.ext.Application at runtime
        self.crash_count = 0
        self._consecutive_failures = 0
        self._supervisor_task: asyncio.Task[None] | None = None
        self._running = False

    def is_running(self) -> bool:
        """Check if the bot updater is active and polling."""
        return self._app is not None and self._app.updater is not None and self._app.updater.running

    async def start(self) -> None:
        if not self._enabled:
            logger.debug("Telegram disabled — bot runner not starting")
            return

        self._running = True
        try:
            await self._real_start()
        except Exception as exc:
            logger.exception("Failed to start Telegram bot on initial attempt: %s", exc)
            self._consecutive_failures = 1
            self.crash_count = 1

        import asyncio

        self._supervisor_task = asyncio.create_task(self._supervisor_loop())
        logger.info("Telegram bot supervisor started")

    async def _real_start(self) -> None:
        from telegram.ext import (
            Application,
            CallbackQueryHandler,
            CommandHandler,
            MessageHandler,
            filters,
        )

        app: Any = Application.builder().token(self._token).build()

        h = self._handler
        app.add_handler(CommandHandler("help", h.cmd_help))
        app.add_handler(CommandHandler("status", h.cmd_status))
        app.add_handler(CommandHandler("snapshot", h.cmd_snapshot))
        app.add_handler(CommandHandler("pause", h.cmd_pause))
        app.add_handler(CommandHandler("resume", h.cmd_resume))
        app.add_handler(CommandHandler("stop", h.cmd_stop))
        app.add_handler(CommandHandler("confirm", h.cmd_confirm))
        app.add_handler(CommandHandler("enable", h.cmd_enable))
        app.add_handler(CommandHandler("disable", h.cmd_disable))
        app.add_handler(CallbackQueryHandler(h.handle_callback))

        # Reply keyboard TUI message handlers
        app.add_handler(MessageHandler(filters.Text(["📊 Status"]), h.cmd_status))
        app.add_handler(MessageHandler(filters.Text(["📸 Snapshot"]), h.cmd_snapshot))
        app.add_handler(MessageHandler(filters.Text(["⏸️ Pause"]), h.cmd_pause))
        app.add_handler(MessageHandler(filters.Text(["▶️ Resume"]), h.cmd_resume))
        app.add_handler(MessageHandler(filters.Text(["⏹️ Stop"]), h.cmd_stop))

        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        self._app = app
        logger.info("Telegram bot polling started")

    async def _supervisor_loop(self) -> None:
        import asyncio

        healthy_seconds = 0
        was_running = self.is_running()

        while self._running:
            if not self.is_running():
                # We are crashed or not started yet!
                self.crash_count += 1
                self._consecutive_failures += 1
                healthy_seconds = 0

                logger.error(
                    "Telegram bot is not running (crash count: %d, consecutive: %d)",
                    self.crash_count,
                    self._consecutive_failures,
                )

                if was_running and self._dispatcher:
                    self._dispatcher.dispatch_text(
                        "⚠️ Telegram bot runner crashed "
                        f"(Total crashes: {self.crash_count}). Restarting..."
                    )

                was_running = False

                # Exponential backoff capped at 5 minutes (300s)
                backoff = min(300, 2 ** (self._consecutive_failures - 1) * 2)
                logger.info("Waiting %ds before restarting Telegram bot...", backoff)

                # Check self._running while sleeping in small steps to react quickly to shutdown
                for _ in range(backoff):
                    if not self._running:
                        break
                    await asyncio.sleep(1)

                if not self._running:
                    break

                try:
                    await self._real_start()
                    logger.info("Telegram bot restarted successfully")
                    if self._dispatcher and self.crash_count > 1:
                        self._dispatcher.dispatch_text(
                            "✅ Telegram bot runner recovered successfully."
                        )
                    was_running = True
                except Exception as exc:
                    logger.exception("Failed to start/restart Telegram bot: %s", exc)
                    # Clean up partial start states
                    await self._real_stop()
            else:
                # Bot is running! Monitor it
                was_running = True
                await asyncio.sleep(5)
                healthy_seconds += 5
                if healthy_seconds >= 60:
                    # Reset consecutive failures once we have been stable for 60 seconds
                    self._consecutive_failures = 0

    async def _real_stop(self) -> None:
        if self._app is None:
            return
        import asyncio

        # Cancel any pending background tasks (e.g. snooze tasks)
        self._handler.cancel_background_tasks()

        app: Any = self._app
        try:
            async with asyncio.timeout(5.0):
                if app.updater is not None and app.updater.running:
                    await app.updater.stop()
                await app.stop()
                await app.shutdown()
        except TimeoutError:
            logger.warning("Telegram bot shutdown timed out after 5.0s")
        except Exception as exc:
            logger.warning("Telegram bot shutdown encountered an error: %s", exc)
        finally:
            self._app = None
            logger.info("Telegram bot stopped")

    async def stop(self) -> None:
        self._running = False
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            import asyncio

            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor_task
            self._supervisor_task = None

        await self._real_stop()
