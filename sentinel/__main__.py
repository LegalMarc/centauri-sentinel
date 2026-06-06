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
    from sentinel.notify.dispatcher import NotificationDispatcher, Notifier
    from sentinel.notify.ntfy import NtfyNotifier
    from sentinel.notify.telegram import TelegramNotifier
    from sentinel.printer.client import PrinterClient
    from sentinel.safety import check_external_bind
    from sentinel.watcher.loop import WatcherLoop
    from sentinel.web.app import create_app

    if args.host:
        os.environ["BIND_HOST"] = args.host
    if args.port:
        os.environ["BIND_PORT"] = str(args.port)

    import logging.config

    get_settings.cache_clear()
    settings = get_settings()

    # Configure logging formatter
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "json": {
                    "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
                    "fmt": "%(asctime)s %(levelname)s %(name)s %(message)s",
                },
                "text": {
                    "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": settings.log_format,
                },
            },
            "root": {
                "handlers": ["console"],
                "level": settings.log_level,
            },
        }
    )

    if args.host:
        settings.bind_host = args.host
    if args.port:
        settings.bind_port = args.port
    host = settings.bind_host
    port = settings.bind_port

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
        from sentinel.network import validate_printer_ip

        try:
            settings.printer_ip = validate_printer_ip(db_printer_ip)
        except ValueError as exc:
            logger.error("Stored printer_ip failed SSRF validation: %s", exc)
            # fallback to the config default
            pass

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

    dispatcher = NotificationDispatcher(notifiers)

    watcher = WatcherLoop(settings, printer, camera, ml, db, dispatcher)

    app = create_app(settings, db=db, watcher=watcher, camera=camera, auth_secret=auth_secret)

    config = uvicorn.Config(app, host=host, port=port, log_level=settings.log_level.lower())
    server = uvicorn.Server(config)

    bot: BotRunner | None = None
    if telegram is not None:
        handler = BotCommandHandler(settings, printer, camera, db, watcher, telegram)
        bot = BotRunner(settings, handler, dispatcher)
        app.state.bot = bot
        await bot.start()

    watcher_task: asyncio.Task[None] = asyncio.create_task(watcher.run_forever(), name="watcher")

    try:
        await server.serve()
    finally:
        watcher_task.cancel()
        await asyncio.gather(watcher_task, return_exceptions=True)
        if bot is not None:
            await bot.stop()
        # Clean up client resources concurrently to avoid slow shutdown
        cleanup_tasks = []
        if hasattr(printer, "close"):
            cleanup_tasks.append(printer.close())
        if hasattr(ml, "close"):
            cleanup_tasks.append(ml.close())
        if hasattr(camera, "close"):
            cleanup_tasks.append(camera.close())
        for notifier in notifiers:
            if hasattr(notifier, "close"):
                cleanup_tasks.append(notifier.close())

        if cleanup_tasks:
            results = await asyncio.gather(*cleanup_tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, Exception):
                    logger.exception("Failed to close a resource cleanly: %s", res)

        await db.checkpoint()
        await db.close()


if __name__ == "__main__":
    main()
