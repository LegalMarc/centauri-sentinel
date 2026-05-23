"""Entry point: python -m sentinel."""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="sentinel",
        description="centauri-sentinel: failure detection for the Elegoo Centauri Carbon 2",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 0.1.0",
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
        _run(args)


def _run(args: argparse.Namespace) -> None:
    import uvicorn

    from sentinel.config import get_settings
    from sentinel.web.app import create_app

    settings = get_settings()
    host = args.host or settings.bind_host
    port = args.port or settings.bind_port

    app = create_app(settings)
    uvicorn.run(app, host=host, port=port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
