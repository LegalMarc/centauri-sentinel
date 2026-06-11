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

import asyncio
import base64
import contextlib
import hashlib
import hmac
import ipaddress
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


def _resolve_client_ip(
    scope: Scope, headers: dict[bytes, bytes], trust_proxies: bool = False
) -> str:
    """Return the real client IP, respecting X-Forwarded-For if trust_proxies is True."""
    if trust_proxies:
        x_forwarded_for = headers.get(b"x-forwarded-for")
        if x_forwarded_for:
            # The rightmost IP is the one added by our trusted reverse proxy.
            return x_forwarded_for.decode().split(",")[-1].strip()
    client = scope.get("client")
    return client[0] if client else "0.0.0.0"


class AuthMiddleware:
    """ASGI middleware that gates all routes behind Basic auth + cookie session."""

    def __init__(self, app: ASGIApp, settings: Settings, secret: bytes | None = None) -> None:
        self._app = app
        self._settings = settings
        self._enabled = settings.auth_enabled
        self._username = settings.auth_username or ""
        self._password_hash = settings.auth_password_bcrypt or ""
        # secret must be provided by caller (loaded from DB); fall back to
        # ephemeral random only if no DB is available (e.g. tests).
        self._secret = secret if secret is not None else os.urandom(32)
        # Store auth attempts by IP: [timestamps...] (max 10 per minute)
        # Bounded by OrderedDict to prevent memory leak (OOM) from many IPs
        import collections

        self._auth_attempts: collections.OrderedDict[str, list[float]] = collections.OrderedDict()
        self._auth_cookie_secure = settings.auth_cookie_secure

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))

        # Internal routes are always exempt — checked first, before host/CSRF
        # guards, so that the ML callback (Host: sentinel:8000) is never
        # blocked by DNS-rebinding protection.  The endpoint is independently
        # secured by the per-process internal_token query param + TTL'd nonce.
        path: str = scope.get("path", "")
        if path.startswith("/__internal_snapshot/"):
            await self._app(scope, receive, send)
            return

        host_header = headers.get(b"host", b"").decode().split(":")[0]

        is_private_ip = False
        with contextlib.suppress(ValueError):
            is_private_ip = ipaddress.ip_address(host_header).is_private

        if (
            not self._settings.external_bind_allowed
            and host_header
            not in (
                "localhost",
                "127.0.0.1",
                "::1",
                "testserver",
                "test",
                self._settings.bind_host,
            )
            and not is_private_ip
        ):
            response = Response(
                status_code=403, content="DNS Rebinding Protection: Host not allowed"
            )
            await response(scope, receive, send)
            return

        if scope.get("method") in ("POST", "PUT", "DELETE", "PATCH"):
            origin = headers.get(b"origin")
            host = headers.get(b"host", b"")
            if origin:
                from urllib.parse import urlparse

                try:
                    parsed_origin = urlparse(origin.decode())
                    origin_netloc = parsed_origin.netloc
                except Exception:
                    origin_netloc = ""

                if not origin_netloc or origin_netloc != host.decode():
                    response = Response(status_code=403, content="CSRF Protection: Origin mismatch")
                    await response(scope, receive, send)
                    return
            else:
                referer = headers.get(b"referer")
                if referer and host:
                    from urllib.parse import urlparse

                    parsed = urlparse(referer.decode())
                    if parsed.netloc != host.decode():
                        response = Response(
                            status_code=403, content="CSRF Protection: Referer mismatch"
                        )
                        await response(scope, receive, send)
                        return
                else:
                    response = Response(
                        status_code=403, content="CSRF Protection: Missing Origin and Referer"
                    )
                    await response(scope, receive, send)
                    return

        if not self._enabled:
            # Ticket 1: strictly block any client IP that is not localhost when auth is disabled.
            client_ip = _resolve_client_ip(scope, headers, self._settings.trust_proxies)
            if client_ip not in ("127.0.0.1", "::1", "localhost", "testclient"):
                response = Response(
                    status_code=403,
                    content="Auth is disabled. Access restricted to localhost.",
                )
                await response(scope, receive, send)
                return
            await self._app(scope, receive, send)
            return

        if path in ("/healthz", "/readyz"):
            await self._app(scope, receive, send)
            return

        cookie_header = headers.get(b"cookie", b"").decode()
        user_agent = headers.get(b"user-agent", b"").decode()

        if self._valid_cookie(cookie_header, user_agent):
            await self._app(scope, receive, send)
            return

        auth_header = headers.get(b"authorization", b"").decode()
        if auth_header.startswith("Basic "):
            client_ip = _resolve_client_ip(scope, headers, self._settings.trust_proxies)
            now = time.time()
            attempts = [t for t in self._auth_attempts.get(client_ip, []) if now - t < 60]
            if len(attempts) >= 10:
                self._auth_attempts[client_ip] = attempts
                self._auth_attempts.move_to_end(client_ip)
                response = Response(status_code=429, content="Too Many Requests")
                await response(scope, receive, send)
                return
            attempts.append(now)
            self._auth_attempts[client_ip] = attempts
            self._auth_attempts.move_to_end(client_ip)
            while len(self._auth_attempts) > 1000:
                self._auth_attempts.popitem(last=False)

            try:
                decoded = base64.b64decode(auth_header[6:]).decode()
                username, _, password = decoded.partition(":")
            except Exception:
                username = password = ""

            if await self._check_credentials(username, password):
                cookie = self._make_cookie(user_agent)

                if self._auth_cookie_secure == "always":
                    is_secure = True
                elif self._auth_cookie_secure == "never":
                    is_secure = False
                else:  # "auto"
                    headers_dict = {k.lower(): v for k, v in headers.items()}
                    proto = headers_dict.get(b"x-forwarded-proto", b"").decode().lower()
                    scheme = scope.get("scheme", "http").lower()
                    is_secure = scheme == "https" or proto == "https"
                secure_flag = "; Secure" if is_secure else ""

                query_string = scope.get("query_string", b"").decode()

                # Prevent open redirects: ensure path starts with a single slash,
                # and doesn't use scheme-relative URLs (//) or backslashes (\).
                safe_path = "/"
                if (
                    path.startswith("/")
                    and not path.startswith("//")
                    and not path.startswith("/\\")
                ):
                    safe_path = path

                redirect_url = f"{safe_path}?{query_string}" if query_string else safe_path

                response = Response(
                    status_code=302,
                    headers={
                        "Location": redirect_url,
                        "Set-Cookie": (
                            f"{_COOKIE_NAME}={cookie}; Path=/; HttpOnly; "
                            f"SameSite=Strict{secure_flag}; Max-Age={_TTL}"
                        ),
                    },
                )
                await response(scope, receive, send)
                return
            else:
                await asyncio.sleep(0.5)

        response = Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="centauri-sentinel"'},
            content="Unauthorized",
        )
        await response(scope, receive, send)

    async def _check_credentials(self, username: str, password: str) -> bool:
        # Constant-time username comparison prevents user-enumeration via timing.
        username_ok = hmac.compare_digest(username.encode("utf-8"), self._username.encode("utf-8"))
        # Fix timing oracle: always hash against the actual password hash to maintain
        # identical bcrypt cost/time, regardless of whether the username is correct.
        hash_to_check = self._password_hash if self._password_hash else _DUMMY_HASH
        try:
            pw_ok = await asyncio.to_thread(
                bcrypt.checkpw, password.encode("utf-8"), hash_to_check.encode("utf-8")
            )
        except Exception:
            pw_ok = False
        return username_ok and pw_ok

    def _ua_hash(self, user_agent: str) -> str:
        """Return first 16 hex chars of HMAC(secret, user_agent)."""
        return hmac.new(self._secret, user_agent.encode("utf-8"), hashlib.sha256).hexdigest()[:16]

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
            if name.strip() == _COOKIE_NAME and self._verify_token(value.strip(), user_agent):
                return True
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
