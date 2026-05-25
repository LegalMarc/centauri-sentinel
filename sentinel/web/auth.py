"""Basic-auth + signed session cookie middleware.

If auth_enabled=False the middleware is a no-op pass-through.
If auth_enabled=True:
  - A valid ``sentinel_session`` cookie (HMAC-SHA256, 1 h TTL) passes through.
  - Otherwise challenge with WWW-Authenticate: Basic.
  - Successful Basic credentials set the cookie and redirect to the same URL.

Internal routes (/__internal_snapshot/*) are always exempt.

Cookie format: ``{ts}.{rnd}.{ua_hash}.{sig}`` where:
  - rnd      = per-issuance random hex (makes each cookie unique)
  - ua_hash  = first 16 hex chars of HMAC(secret, User-Agent); binds the
               cookie to the issuing browser so a captured cookie cannot be
               replayed from a different User-Agent
  - sig      = HMAC-SHA256(secret, "{ts}.{rnd}.{ua_hash}")

The HMAC secret is loaded from the DB at startup (persisted by ``_run()``),
so sessions survive process restarts.  If no secret is found (first run or
no DB), a fresh random secret is generated — callers should persist it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import TYPE_CHECKING

import bcrypt
from starlette.responses import Response

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from sentinel.config import Settings

_COOKIE_NAME = "sentinel_session"
_TTL = 3600  # 1 hour

# Dummy bcrypt hash used when username is wrong so we always spend bcrypt time
# regardless of whether the username exists (prevents timing oracle).
_DUMMY_HASH = bcrypt.hashpw(b"__sentinel_dummy__", bcrypt.gensalt()).decode()


class AuthMiddleware:
    """ASGI middleware that gates all routes behind Basic auth + cookie session."""

    def __init__(self, app: ASGIApp, settings: Settings, secret: bytes | None = None) -> None:
        self._app = app
        self._enabled = settings.auth_enabled
        self._username = settings.auth_username or ""
        self._password_hash = settings.auth_password_bcrypt or ""
        # secret must be provided by caller (loaded from DB); fall back to
        # ephemeral random only if no DB is available (e.g. tests).
        self._secret = secret if secret is not None else os.urandom(32)

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
        user_agent = headers.get(b"user-agent", b"").decode()

        if self._valid_cookie(cookie_header, user_agent):
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
                cookie = self._make_cookie(user_agent)
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
        # Constant-time username comparison prevents user-enumeration via timing.
        username_ok = hmac.compare_digest(
            username.encode("utf-8"), self._username.encode("utf-8")
        )
        # Always run bcrypt even on a bad username so timing is indistinguishable.
        hash_to_check = (
            self._password_hash if (username_ok and self._password_hash) else _DUMMY_HASH
        )
        try:
            pw_ok = bcrypt.checkpw(password.encode("utf-8"), hash_to_check.encode("utf-8"))
        except Exception:
            pw_ok = False
        return username_ok and pw_ok

    def _ua_hash(self, user_agent: str) -> str:
        """Return first 16 hex chars of HMAC(secret, user_agent)."""
        return hmac.new(
            self._secret, user_agent.encode("utf-8"), hashlib.sha256
        ).hexdigest()[:16]

    def _make_cookie(self, user_agent: str) -> str:
        ts = str(int(time.time()))
        rnd = secrets.token_hex(8)
        ua_hash = self._ua_hash(user_agent)
        msg = f"{ts}.{rnd}.{ua_hash}".encode()
        sig = hmac.new(self._secret, msg, hashlib.sha256).hexdigest()
        return f"{ts}.{rnd}.{ua_hash}.{sig}"

    def _valid_cookie(self, cookie_header: str, user_agent: str) -> bool:
        for part in cookie_header.split(";"):
            name, _, value = part.strip().partition("=")
            if name.strip() == _COOKIE_NAME:
                return self._verify_token(value.strip(), user_agent)
        return False

    def _verify_token(self, token: str, user_agent: str) -> bool:
        try:
            parts = token.split(".", 3)
            if len(parts) != 4:
                return False
            ts_str, rnd, ua_hash, sig = parts
            ts = int(ts_str)
            if time.time() - ts > _TTL:
                return False
            if not hmac.compare_digest(ua_hash, self._ua_hash(user_agent)):
                return False
            msg = f"{ts_str}.{rnd}.{ua_hash}".encode()
            expected = hmac.new(self._secret, msg, hashlib.sha256).hexdigest()
            return hmac.compare_digest(sig, expected)
        except Exception:
            return False
