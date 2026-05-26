"""Telegram bot long-polling runner.

Lifecycle:
  await runner.start()   — initialize Application and begin polling
  await runner.stop()    — drain pending updates and shut down cleanly

Disabled gracefully when ``telegram_enabled`` is False.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sentinel.bot.commands import BotCommandHandler
    from sentinel.config import Settings

logger = logging.getLogger(__name__)


class BotRunner:
    """Manages the python-telegram-bot Application lifecycle."""

    def __init__(self, settings: Settings, handler: BotCommandHandler) -> None:
        self._enabled = settings.telegram_enabled
        self._token = settings.telegram_bot_token or ""
        self._handler = handler
        self._app: Any = None  # telegram.ext.Application at runtime

    async def start(self) -> None:
        if not self._enabled:
            logger.debug("Telegram disabled — bot runner not starting")
            return

        from telegram.ext import Application, CallbackQueryHandler, CommandHandler

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

        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        self._app = app
        logger.info("Telegram bot polling started")

    async def stop(self) -> None:
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
        finally:
            self._app = None
            logger.info("Telegram bot stopped")
