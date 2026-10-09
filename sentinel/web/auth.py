"""Login gate for the dashboard: form login, persistent sessions, Basic auth for scripts.

If auth_enabled=False the middleware only enforces the localhost restriction.
If auth_enabled=True:
  - A valid ``sentinel_session`` cookie passes through. Sessions are
    server-side (sentinel/web/sessions.py) and last until the user logs out;
    the cookie is re-issued on use so the browser never ages it out.
  - Browsers without a session are redirected to the /login form.
  - Non-browser clients may send HTTP Basic credentials on each request.
    That authenticates the single request and creates no session; credentials
    that passed bcrypt are cached briefly (by digest) to spare repeat checks.
  - /login answers 404 when auth is disabled.
  - POST /logout revokes the session server-side and clears the cookie.

Internal routes (/__internal_snapshot/*) are always exempt.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import hashlib
import hmac
import html
import ipaddress
import logging
import time
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote
from urllib.parse import urlparse as _urlparse
from urllib.parse import urlsplit as _urlsplit_parse

import bcrypt
from starlette.responses import Response

from sentinel.web.sessions import (
    COOKIE_MAX_AGE,
    COOKIE_NAME,
    SessionStore,
    credential_fingerprint,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from sentinel.config import Settings

logger = logging.getLogger(__name__)

# Hard cap on the /login form body. This middleware wraps LimitUploadSizeMiddleware
# (it is registered later, so it is the outer layer) and handles /login itself,
# so the 1 MB app-level limit never sees the login body. A real login form is a
# few hundred bytes; anything larger is an unauthenticated memory-exhaustion attempt.
_MAX_LOGIN_BODY_BYTES = 64 * 1024

# Failed-credential rate limit: attempts per client IP per window.
_RATE_LIMIT_ATTEMPTS = 10
_RATE_LIMIT_WINDOW_S = 60.0
_RATE_LIMIT_MAX_IPS = 1000

# Successful Basic credentials are remembered briefly so a script polling the API
# does not pay a bcrypt check (~250 ms of CPU) on every request.
_BASIC_CACHE_TTL_S = 60.0
_BASIC_CACHE_MAX = 64


class _BodyTooLargeError(Exception):
    """Raised by _read_body when the request body exceeds _MAX_LOGIN_BODY_BYTES."""


# Dummy bcrypt hash used when username is wrong so we always spend bcrypt time
# regardless of whether the username exists (prevents timing oracle).
_DUMMY_HASH = bcrypt.hashpw(b"__sentinel_dummy__", bcrypt.gensalt()).decode()

_LOGIN_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in — Centauri Sentinel</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
     background:#0f172a;color:#e2e8f0;min-height:100vh;
     display:flex;align-items:center;justify-content:center}
.card{background:#1e293b;border:1px solid #334155;border-radius:12px;
      padding:2rem;width:100%;max-width:360px}
h1{font-size:1.25rem;font-weight:600;margin-bottom:1.5rem;
   text-align:center;color:#f1f5f9}
label{display:block;font-size:.875rem;color:#94a3b8;margin-bottom:.375rem}
input[type=text],input[type=password]{width:100%;padding:.625rem .75rem;
  border:1px solid #475569;border-radius:6px;background:#0f172a;
  color:#f1f5f9;font-size:.9375rem;outline:none;transition:border-color .15s}
input:focus{border-color:#3b82f6}
.field{margin-bottom:1rem}
button{width:100%;padding:.625rem;background:#3b82f6;color:#fff;
       border:none;border-radius:6px;font-size:.9375rem;font-weight:500;
       cursor:pointer;margin-top:.5rem;transition:background .15s}
button:hover{background:#2563eb}
.error{background:#450a0a;border:1px solid #b91c1c;color:#fca5a5;
       border-radius:6px;padding:.625rem .75rem;margin-bottom:1rem;
       font-size:.875rem}
</style>
</head>
<body>
<div class="card">
  <h1>Centauri Sentinel</h1>
  __ERROR_HTML__
  <form method="post" action="/login">
    <input type="hidden" name="next" value="__NEXT_URL__">
    <div class="field">
      <label for="u">Username</label>
      <input type="text" id="u" name="username" autocomplete="username" autofocus required>
    </div>
    <div class="field">
      <label for="p">Password</label>
      <input type="password" id="p" name="password" autocomplete="current-password" required>
    </div>
    <button type="submit">Sign in</button>
  </form>
</div>
</body>
</html>"""


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


def _safe_next(raw: str) -> str:
    """Return *raw* if it is a same-origin path, else "/" (prevents open redirects).

    Control characters and spaces are rejected too: browsers strip tab, CR and LF
    from URLs (WHATWG), so ``/\t/evil.example`` would resolve to ``//evil.example``.
    Non-ASCII is rejected as well: the value goes into a Location header, which
    is Latin-1 encoded, so it would turn a successful login into a 500.
    """
    if (
        raw.startswith("/")
        and not raw.startswith("//")
        and "\\" not in raw
        and all(c.isascii() and ord(c) > 0x20 and ord(c) != 0x7F for c in raw)
    ):
        return raw
    return "/"


class AuthMiddleware:
    """ASGI middleware that gates all routes behind a login session or Basic auth."""

    def __init__(
        self, app: ASGIApp, settings: Settings, sessions: SessionStore | None = None
    ) -> None:
        self._app = app
        self._settings = settings
        self._enabled = settings.auth_enabled
        self._username = settings.auth_username or ""
        self._password_hash = settings.auth_password_bcrypt or ""
        # The caller passes a DB-backed store loaded at startup; the memory-only
        # fallback serves tests and runs without a database.
        self._sessions = sessions or SessionStore(
            credential_fingerprint(self._username, self._password_hash)
        )
        # Recent credential attempts per client IP, LRU-bounded so a flood of
        # source addresses cannot grow it without limit.
        self._auth_attempts: collections.OrderedDict[str, list[float]] = collections.OrderedDict()
        self._auth_cookie_secure = settings.auth_cookie_secure
        # SHA-256 of the raw Authorization header -> expiry time, for Basic
        # credentials that already passed bcrypt. Only the digest is kept.
        self._basic_cache: collections.OrderedDict[bytes, float] = collections.OrderedDict()

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

        _raw_host = headers.get(b"host", b"").decode()
        # urlsplit correctly handles bracketed IPv6 literals like [::1]:8000,
        # returning hostname="::1" (brackets stripped, no port appended).
        # Plain hostnames and IPv4 addresses are unchanged.
        _split = _urlsplit_parse(f"//{_raw_host}")
        host_header: str = _split.hostname or _raw_host

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

        # /login is exempt from CSRF and auth checks (it IS the auth endpoint).
        # DNS-rebinding protection above still applies. With auth off there is
        # nothing to sign in to, and the dummy credentials must not mint sessions.
        if path == "/login" and scope.get("method") in ("GET", "POST"):
            if not self._enabled:
                await Response(status_code=404, content="Not Found")(scope, receive, send)
                return
            await self._handle_login(scope, receive, send, headers)
            return

        csrf_error = self._csrf_error(scope, headers)
        if csrf_error is not None:
            response = Response(status_code=403, content=csrf_error)
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

        token = self._session_token(headers)

        if path == "/logout" and scope.get("method") == "POST":
            await self._handle_logout(scope, receive, send, headers, token)
            return

        if token and self._sessions.is_valid(token):
            if self._sessions.needs_refresh(token):
                await self._sessions.refresh(token)
                send = self._with_cookie(send, self._session_cookie(scope, headers, token))
            await self._app(scope, receive, send)
            return

        auth_header = headers.get(b"authorization", b"").decode()
        if auth_header.startswith("Basic "):
            cache_key = hashlib.sha256(auth_header.encode()).digest()
            if self._basic_cache_hit(cache_key):
                await self._app(scope, receive, send)
                return
            client_ip = _resolve_client_ip(scope, headers, self._settings.trust_proxies)
            if self._rate_limited(client_ip):
                response = Response(status_code=429, content="Too Many Requests")
                await response(scope, receive, send)
                return
            try:
                decoded = base64.b64decode(auth_header[6:]).decode()
                username, _, password = decoded.partition(":")
            except Exception:
                username = password = ""
            if await self._check_credentials(username, password):
                self._auth_attempts.pop(client_ip, None)
                self._basic_cache_store(cache_key)
                await self._app(scope, receive, send)
                return
            await asyncio.sleep(0.5)

        # Redirect browsers to the login form; return 401 for API clients.
        accept = headers.get(b"accept", b"").decode()
        if "text/html" in accept:
            qs = scope.get("query_string", b"").decode()
            next_val = f"{path}?{qs}" if qs else path
            response = Response(
                status_code=302,
                headers={"Location": f"/login?next={quote(next_val, safe='/')}"},
            )
        else:
            response = Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="centauri-sentinel"'},
                content="Unauthorized",
            )
        await response(scope, receive, send)

    # ------------------------------------------------------------------
    # /login and /logout
    # ------------------------------------------------------------------

    async def _handle_login(
        self, scope: Scope, receive: Receive, send: Send, headers: dict[bytes, bytes]
    ) -> None:
        if scope.get("method") == "GET":
            qs = scope.get("query_string", b"").decode()
            next_url = parse_qs(qs).get("next", ["/"])[0]
            token = self._session_token(headers)
            if self._enabled and token and self._sessions.is_valid(token):
                # Already signed in: skip the form.
                response = Response(status_code=302, headers={"Location": _safe_next(next_url)})
            else:
                response = Response(content=self._login_page(next_url), media_type="text/html")
            await response(scope, receive, send)
            return

        try:
            body = await self._read_body(receive)
        except _BodyTooLargeError:
            response = Response(status_code=413, content="Payload Too Large")
            await response(scope, receive, send)
            return
        form = parse_qs(body.decode("utf-8", errors="replace"))
        username = form.get("username", [""])[0]
        password = form.get("password", [""])[0]
        safe_next = _safe_next(form.get("next", ["/"])[0])

        client_ip = _resolve_client_ip(scope, headers, self._settings.trust_proxies)
        if self._rate_limited(client_ip):
            response = Response(status_code=429, content="Too Many Requests")
            await response(scope, receive, send)
            return

        if await self._check_credentials(username, password):
            self._auth_attempts.pop(client_ip, None)
            token = await self._sessions.create()
            response = Response(
                status_code=302,
                headers={
                    "Location": safe_next,
                    "Set-Cookie": self._session_cookie(scope, headers, token),
                },
            )
        else:
            await asyncio.sleep(0.5)
            response = Response(
                content=self._login_page(safe_next, error=True),
                media_type="text/html",
            )
        await response(scope, receive, send)

    async def _handle_logout(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        headers: dict[bytes, bytes],
        token: str | None,
    ) -> None:
        if token:
            try:
                await self._sessions.revoke(token)
            except Exception:
                # Fail closed: the session is still valid, so keep the cookie
                # and tell the user instead of pretending they are signed out.
                logger.exception("Logout failed: could not revoke session")
                response = Response(
                    status_code=503,
                    content="Logout failed, please try again.",
                    media_type="text/plain",
                )
                await response(scope, receive, send)
                return
        clear = self._session_cookie(scope, headers, "", max_age=0)
        if "text/html" in headers.get(b"accept", b"").decode():
            # A form submit from the dashboard: land on the login page.
            response = Response(
                status_code=303, headers={"Location": "/login", "Set-Cookie": clear}
            )
        else:
            response = Response(
                content='{"status": "ok"}',
                media_type="application/json",
                headers={"Set-Cookie": clear},
            )
        await response(scope, receive, send)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _csrf_error(scope: Scope, headers: dict[bytes, bytes]) -> str | None:
        """Return a rejection message for a cross-origin state-changing request."""
        if scope.get("method") not in ("POST", "PUT", "DELETE", "PATCH"):
            return None
        host = headers.get(b"host", b"").decode()
        origin = headers.get(b"origin")
        if origin:
            try:
                origin_netloc = _urlparse(origin.decode()).netloc
            except Exception:
                origin_netloc = ""
            if not origin_netloc or origin_netloc != host:
                return "CSRF Protection: Origin mismatch"
            return None
        referer = headers.get(b"referer")
        if referer and host:
            if _urlparse(referer.decode()).netloc != host:
                return "CSRF Protection: Referer mismatch"
            return None
        return "CSRF Protection: Missing Origin and Referer"

    def _rate_limited(self, client_ip: str) -> bool:
        """Record a credential attempt; True if the IP is over the limit.

        The attempt is recorded before the (slow) bcrypt check so concurrent
        guesses cannot all slip under the limit. A successful check clears the
        IP's history, so only failures accumulate.
        """
        now = time.time()
        attempts = [
            t for t in self._auth_attempts.get(client_ip, []) if now - t < _RATE_LIMIT_WINDOW_S
        ]
        limited = len(attempts) >= _RATE_LIMIT_ATTEMPTS
        if not limited:
            attempts.append(now)
        self._auth_attempts[client_ip] = attempts
        self._auth_attempts.move_to_end(client_ip)
        while len(self._auth_attempts) > _RATE_LIMIT_MAX_IPS:
            self._auth_attempts.popitem(last=False)
        return limited

    def _basic_cache_hit(self, key: bytes) -> bool:
        """True if *key* names Basic credentials that passed bcrypt within the TTL."""
        expires = self._basic_cache.get(key)
        if expires is None:
            return False
        if expires <= time.time():
            del self._basic_cache[key]
            return False
        return True

    def _basic_cache_store(self, key: bytes) -> None:
        """Remember a successful Basic credential; oldest entries drop past the cap."""
        self._basic_cache[key] = time.time() + _BASIC_CACHE_TTL_S
        self._basic_cache.move_to_end(key)
        while len(self._basic_cache) > _BASIC_CACHE_MAX:
            self._basic_cache.popitem(last=False)

    @staticmethod
    def _session_token(headers: dict[bytes, bytes]) -> str | None:
        for part in headers.get(b"cookie", b"").decode().split(";"):
            name, _, value = part.strip().partition("=")
            if name.strip() == COOKIE_NAME and value.strip():
                return value.strip()
        return None

    def _session_cookie(
        self,
        scope: Scope,
        headers: dict[bytes, bytes],
        token: str,
        max_age: int = COOKIE_MAX_AGE,
    ) -> str:
        if self._auth_cookie_secure == "always":
            is_secure = True
        elif self._auth_cookie_secure == "never":
            is_secure = False
        else:  # "auto"
            proto = headers.get(b"x-forwarded-proto", b"").decode().lower()
            scheme = scope.get("scheme", "http").lower()
            is_secure = scheme == "https" or proto == "https"
        secure_flag = "; Secure" if is_secure else ""
        return (
            f"{COOKIE_NAME}={token}; Path=/; HttpOnly; "
            f"SameSite=Strict{secure_flag}; Max-Age={max_age}"
        )

    @staticmethod
    def _with_cookie(send: Send, cookie: str) -> Send:
        """Wrap *send* so the response carries an extra Set-Cookie header."""

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = [
                    *message.get("headers", []),
                    (b"set-cookie", cookie.encode("latin-1")),
                ]
            await send(message)

        return send_with_cookie

    @staticmethod
    def _login_page(next_url: str = "/", *, error: bool = False) -> str:
        error_html = '<p class="error">Invalid username or password.</p>' if error else ""
        return _LOGIN_TEMPLATE.replace("__ERROR_HTML__", error_html).replace(
            "__NEXT_URL__", html.escape(next_url, quote=True)
        )

    @staticmethod
    async def _read_body(receive: Receive) -> bytes:
        """Read the request body, bounded by _MAX_LOGIN_BODY_BYTES.

        Stops consuming the stream as soon as the cap is exceeded so a large
        upload is never buffered in memory; raises _BodyTooLargeError.
        """
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            chunk = message.get("body")
            if isinstance(chunk, bytes):
                size += len(chunk)
                if size > _MAX_LOGIN_BODY_BYTES:
                    raise _BodyTooLargeError
                chunks.append(chunk)
            if not message.get("more_body", False):
                break
        return b"".join(chunks)

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
