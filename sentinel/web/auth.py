"""Basic-auth + signed session cookie middleware.

If auth_enabled=False the middleware is a no-op pass-through.
If auth_enabled=True:
  - A valid ``sentinel_session`` cookie (HMAC-SHA256, 1 h TTL) passes through.
  - Otherwise challenge with WWW-Authenticate: Basic.
  - Successful Basic credentials set the cookie and redirect to the same URL.

Internal routes (/__internal_snapshot/*) are always exempt.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import TYPE_CHECKING

import bcrypt
from starlette.responses import Response

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from sentinel.config import Settings

_COOKIE_NAME = "sentinel_session"
_TTL = 3600  # 1 hour


class AuthMiddleware:
    """ASGI middleware that gates all routes behind Basic auth + cookie session."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self._app = app
        self._enabled = settings.auth_enabled
        self._username = settings.auth_username or ""
        self._password_hash = settings.auth_password_bcrypt or ""
        self._secret = os.urandom(32)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._enabled or scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if path.startswith("/__internal_snapshot/") or path == "/healthz":
            await self._app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        cookie_header = headers.get(b"cookie", b"").decode()

        if self._valid_cookie(cookie_header):
            await self._app(scope, receive, send)
            return

        auth_header = headers.get(b"authorization", b"").decode()
        if auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode()
                username, _, password = decoded.partition(":")
            except Exception:
                username = password = ""

            if self._check_credentials(username, password):
                cookie = self._make_cookie()
                response = Response(
                    status_code=302,
                    headers={
                        "Location": path,
                        "Set-Cookie": (
                            f"{_COOKIE_NAME}={cookie}; Path=/; HttpOnly; "
                            f"SameSite=Lax; Max-Age={_TTL}"
                        ),
                    },
                )
                await response(scope, receive, send)
                return

        response = Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="centauri-sentinel"'},
            content="Unauthorized",
        )
        await response(scope, receive, send)

    def _check_credentials(self, username: str, password: str) -> bool:
        if username != self._username:
            return False
        if not self._password_hash:
            return False
        try:
            return bcrypt.checkpw(password.encode(), self._password_hash.encode())
        except Exception:
            return False

    def _make_cookie(self) -> str:
        ts = str(int(time.time()))
        sig = hmac.new(self._secret, ts.encode(), hashlib.sha256).hexdigest()
        return f"{ts}.{sig}"

    def _valid_cookie(self, cookie_header: str) -> bool:
        for part in cookie_header.split(";"):
            name, _, value = part.strip().partition("=")
            if name.strip() == _COOKIE_NAME:
                return self._verify_token(value.strip())
        return False

    def _verify_token(self, token: str) -> bool:
        try:
            ts_str, _, sig = token.partition(".")
            ts = int(ts_str)
            if time.time() - ts > _TTL:
                return False
            expected = hmac.new(self._secret, ts_str.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(sig, expected)
        except Exception:
            return False
