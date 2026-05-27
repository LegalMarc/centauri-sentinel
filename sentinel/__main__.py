"""Entry point: python -m sentinel."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

logger = logging.getLogger(__name__)


def main() -> None:
    from sentinel import __version__

    parser = argparse.ArgumentParser(
        prog="sentinel",
        description="centauri-sentinel: failure detection for the Elegoo Centauri Carbon 2",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Start the sentinel watcher and web server")
    run_parser.add_argument(
        "--host",
        default=None,
        help="Bind host (overrides BIND_HOST env var)",
    )
    run_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port (overrides BIND_PORT env var)",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "run":
        asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> None:
    import os

    import uvicorn

    from sentinel.bot.commands import BotCommandHandler
    from sentinel.bot.runner import BotRunner
    from sentinel.camera.mjpeg import MjpegGrabber
    from sentinel.config import get_settings
    from sentinel.db.repo import Database
    from sentinel.ml.client import MlClient
    from sentinel.notify.ntfy import NtfyNotifier
    from sentinel.notify.telegram import TelegramNotifier
    from sentinel.printer.client import PrinterClient
    from sentinel.safety import check_external_bind
    from sentinel.watcher.loop import Notifier, WatcherLoop
    from sentinel.web.app import create_app

    settings = get_settings()
    host = args.host or settings.bind_host
    port = args.port or settings.bind_port

    check_external_bind(settings)

    db = Database(settings.db_path)
    await db.connect()

    # Persist auth cookie secret across restarts so sessions survive reloads.
    auth_secret = await db.get_auth_secret()
    if auth_secret is None:
        auth_secret = os.urandom(32)
        await db.set_auth_secret(auth_secret)

    # Initialise detection_enabled on first run; preserve the operator's
    # intent on subsequent starts by reading from DB rather than config.
    if await db.get_setting("detection_enabled") is None:
        await db.set_setting(
            "detection_enabled",
            "true" if settings.detection_enabled_default else "false",
        )

    db_printer_ip = await db.get_setting("printer_ip")
    if db_printer_ip:
        settings.printer_ip = db_printer_ip

    camera = MjpegGrabber(settings)
    printer = PrinterClient(settings)
    ml = MlClient(settings)

    notifiers: list[Notifier] = []
    telegram: TelegramNotifier | None = None
    if settings.telegram_enabled:
        try:
            telegram = TelegramNotifier(settings)
            notifiers.append(telegram)
        except ValueError:
            raise  # configuration error (e.g. empty TELEGRAM_USER_IDS) — abort startup
        except Exception:
            logger.exception("Telegram notifier failed to initialise — notifications disabled")
    if settings.ntfy_enabled:
        notifiers.append(NtfyNotifier(settings))

    watcher = WatcherLoop(settings, printer, camera, ml, db, notifiers)

    app = create_app(settings, db=db, watcher=watcher, camera=camera, auth_secret=auth_secret)

    config = uvicorn.Config(app, host=host, port=port, log_level=settings.log_level.lower())
    server = uvicorn.Server(config)

    bot: BotRunner | None = None
    if telegram is not None:
        handler = BotCommandHandler(settings, printer, camera, db, watcher, telegram)
        bot = BotRunner(settings, handler)
        try:
            await bot.start()
        except Exception:
            logger.exception("Telegram bot failed to start — bot commands disabled")
            bot = None

    watcher_task: asyncio.Task[None] = asyncio.create_task(watcher.run_forever(), name="watcher")

    try:
        await server.serve()
    finally:
        watcher_task.cancel()
        await asyncio.gather(watcher_task, return_exceptions=True)
        if bot is not None:
            await bot.stop()
        # Clean up the persistent MQTT status listener
        if hasattr(printer, "close"):
            await printer.close()
        await db.close()


if __name__ == "__main__":
    main()
