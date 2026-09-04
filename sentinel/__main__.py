"""Entry point: python -m sentinel."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
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

    hash_parser = subparsers.add_parser(
        "hash-password",
        help="Generate a bcrypt hash for the dashboard password (AUTH_PASSWORD_BCRYPT)",
    )
    hash_parser.add_argument(
        "--password",
        default=None,
        help=(
            "Password to hash. Omit to be prompted securely. "
            "WARNING: passing it here exposes it in your shell history / process list."
        ),
    )
    hash_parser.add_argument(
        "--file",
        default=None,
        help="Write the hash to this file (chmod 600) for use with AUTH_PASSWORD_BCRYPT_FILE",
    )
    hash_parser.add_argument(
        "--rounds",
        type=int,
        default=12,
        help="bcrypt cost factor (default: 12)",
    )

    cmd_parser = subparsers.add_parser(
        "printer-cmd",
        help=(
            "Hardware check of the MQTT control path: register with the printer, send one "
            "command, confirm the ack, then watch print_status.state for the effect"
        ),
    )
    cmd_parser.add_argument(
        "action",
        choices=("status", "pause", "resume"),
        help="status = read one status push; pause/resume = send the command and verify",
    )
    cmd_parser.add_argument(
        "--watch-seconds",
        type=float,
        default=20.0,
        help="How long to poll status after the command for the expected state (default: 20)",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "run":
        asyncio.run(_run(args))
    elif args.command == "hash-password":
        _hash_password(args)
    elif args.command == "printer-cmd":
        sys.exit(asyncio.run(_printer_cmd(args)))


async def _printer_cmd(args: argparse.Namespace) -> int:
    """Supervised end-to-end check of register → command → ack → state change.

    The unit tests prove the client speaks the documented protocol; only this
    talks to a real printer. Returns 0 when the printer both acks the command
    and reports the expected state within --watch-seconds, 1 otherwise. Stop is
    deliberately not offered here: it cancels the print irrecoverably.
    """
    import time

    from sentinel.config import get_settings
    from sentinel.printer.client import PrinterClient

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    settings = get_settings()
    printer = PrinterClient(settings)
    expected = {"pause": "paused", "resume": "printing"}.get(args.action)
    try:
        status = await printer.status()
        print(
            f"status: state={status.print_state!r} printing={status.printing} "
            f"file={status.filename!r} elapsed={status.elapsed_seconds:.0f}s "
            f"serial={printer._serial_number}"
        )
        if expected is None:
            return 0
        if not status.printing:
            print(f"refusing to send {args.action}: printer is not printing")
            return 1

        t0 = time.monotonic()
        if args.action == "pause":
            await printer.pause()
        else:
            await printer.resume()
        print(f"{args.action}: registered, sent, ack received in {time.monotonic() - t0:.2f}s")

        deadline = time.monotonic() + args.watch_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            status = await printer.status()
            print(f"  t+{time.monotonic() - t0:4.1f}s state={status.print_state!r}")
            if status.print_state == expected:
                print(f"OK: printer reports {expected!r}")
                return 0
        print(
            f"FAIL: ack received but printer never reported {expected!r} within "
            f"{args.watch_seconds:.0f}s — the command is accepted but not honoured"
        )
        return 1
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return 1
    finally:
        await printer.close()


def _hash_password(args: argparse.Namespace) -> None:
    """Generate a bcrypt hash for the dashboard password and print setup hints.

    Removes the two friction points of configuring auth: generating the hash,
    and the Docker Compose ``.env`` ``$``-escaping footgun.  With ``--file`` the
    hash is written to a file (the recommended ``AUTH_PASSWORD_BCRYPT_FILE``
    path, which needs no escaping); otherwise both the raw hash and a
    pre-escaped ``.env`` line are printed.
    """
    import getpass

    import bcrypt

    password = args.password
    if password is None:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Confirm password: "):
            print("Passwords do not match.", file=sys.stderr)
            sys.exit(1)
    if not password:
        print("Password must not be empty.", file=sys.stderr)
        sys.exit(1)

    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=args.rounds)).decode()

    if args.file:
        from pathlib import Path

        path = Path(args.file)
        path.write_text(hashed + "\n", encoding="utf-8")
        with contextlib.suppress(OSError):
            path.chmod(0o600)  # best-effort on filesystems without POSIX perms
        print(f"Wrote bcrypt hash to {path}")
        print(f"Now set  AUTH_PASSWORD_BCRYPT_FILE={path}  and mount the file into the container.")
        return

    escaped = hashed.replace("$", "$$")
    print("bcrypt hash generated.\n")
    print("Recommended — store it in a file (no escaping, hidden from `docker inspect`):")
    print(f"    {hashed}")
    print("  Save that to e.g. ./secrets/auth_hash, then set:")
    print("    AUTH_PASSWORD_BCRYPT_FILE=/run/secrets/auth_hash")
    print("  (tip: re-run with --file <path> to write the file for you)\n")
    print("Alternative — inline in a Docker Compose .env / Coolify env var.")
    print("  Compose interpolates `$`, so use this PRE-ESCAPED form:")
    print(f"    AUTH_PASSWORD_BCRYPT={escaped}")


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

    # Everything from here on is wrapped in try/finally so that db.checkpoint()
    # and db.close() always run on the way out — including when notifier or ML
    # client construction below raises (e.g. ValueError for a misconfigured
    # ntfy/Telegram/ML URL), which previously aborted startup before the
    # (former, narrower) try/finally around the run loop was ever entered.
    try:
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

        import secrets

        internal_token = secrets.token_urlsafe(32)
        ml.internal_token = internal_token

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

        app = create_app(
            settings,
            db=db,
            watcher=watcher,
            camera=camera,
            auth_secret=auth_secret,
            internal_token=internal_token,
        )

        config = uvicorn.Config(app, host=host, port=port, log_level=settings.log_level.lower())
        server = uvicorn.Server(config)

        bot: BotRunner | None = None
        if telegram is not None:
            handler = BotCommandHandler(settings, printer, camera, db, watcher, telegram)
            bot = BotRunner(settings, handler, dispatcher)
            app.state.bot = bot
            await bot.start()

        watcher_task: asyncio.Task[None] = asyncio.create_task(
            watcher.run_forever(), name="watcher"
        )
        server_task: asyncio.Task[None] = asyncio.create_task(server.serve(), name="server")
        app.state.watcher_task = watcher_task

        try:
            done, pending = await asyncio.wait(
                [watcher_task, server_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
        finally:
            watcher_task.cancel()
            server_task.cancel()
            await asyncio.gather(watcher_task, server_task, return_exceptions=True)
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
    finally:
        await db.checkpoint()
        await db.close()


if __name__ == "__main__":
    main()
