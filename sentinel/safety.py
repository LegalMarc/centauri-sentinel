"""Startup safety checks — runs before uvicorn binds.

Raises RuntimeError to abort startup on unsafe configuration.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentinel.config import Settings

logger = logging.getLogger(__name__)


def check_external_bind(settings: Settings) -> None:
    """Refuse or warn when binding on a public interface.

    Rules:
    - bind_host == "127.0.0.1" → always safe, no-op.
    - bind_host != "127.0.0.1" and auth disabled and external_bind_allowed=False → abort.
    - bind_host != "127.0.0.1" and (auth enabled or external_bind_allowed=True) → warn.
    """
    if settings.bind_host == "127.0.0.1":
        return

    if not settings.auth_enabled and not settings.external_bind_allowed:
        import os

        is_container = (
            os.path.exists("/.dockerenv")
            or os.path.exists("/run/.containerenv")
            or "CONTAINER" in os.environ
        )
        if settings.bind_host == "0.0.0.0" and is_container:
            logger.warning(
                "Container environment detected: allowing bind on 0.0.0.0 without authentication."
            )
        else:
            msg = (
                f"Refusing to bind on {settings.bind_host}:{settings.bind_port} without auth. "
                "Set AUTH_USERNAME or set EXTERNAL_BIND_ALLOWED=true to override."
            )
            raise RuntimeError(msg)

    logger.warning(
        "Binding on external interface %s:%s — ensure network access is restricted.",
        settings.bind_host,
        settings.bind_port,
    )
