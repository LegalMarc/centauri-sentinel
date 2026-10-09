"""Server-side login sessions that last until the user logs out.

The cookie carries a random 256-bit token. Only its SHA-256 hash is stored
(in memory and in the ``auth_sessions`` table), so a leaked database cannot be
replayed as a cookie. A session stays valid until one of:

  - the user logs out (the row is deleted, so the cookie stops working on every
    replica of it, not just in the browser that clicked the button);
  - the configured credentials change (each row records a fingerprint of the
    username and password hash, and rows with a stale fingerprint are dropped
    at startup, so rotating the password signs everyone out);
  - the session goes unused for longer than the browser cookie lifetime
    (``COOKIE_MAX_AGE``), at which point the browser has already discarded it.

Browsers cap cookie lifetime (Chrome at 400 days), so "never expires" is
implemented by re-issuing the cookie with a fresh ``Max-Age`` whenever a
session is used and its last refresh is older than ``REFRESH_INTERVAL``.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentinel.db.repo import Database

logger = logging.getLogger(__name__)

COOKIE_NAME = "sentinel_session"
# 400 days: the longest cookie lifetime Chrome honours (RFC 6265bis cap).
COOKIE_MAX_AGE = 400 * 24 * 3600
# Re-issue the cookie (and persist the refresh time) at most once a day per session.
REFRESH_INTERVAL = 24 * 3600
# Upper bound on live sessions. Only a holder of valid credentials can create
# one, so this is a guard against a misbehaving client, not an attacker.
MAX_SESSIONS = 200


def credential_fingerprint(username: str, password_hash: str) -> str:
    """Fingerprint of the configured credentials; changes when either changes."""
    return hashlib.sha256(f"{username}\0{password_hash}".encode()).hexdigest()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class _Session:
    refreshed_at: int


class SessionStore:
    """In-memory session index with write-through persistence to SQLite.

    Lookups are served from memory so authenticating a request never touches
    the database. ``db=None`` gives a memory-only store (tests, no-DB runs).
    """

    def __init__(self, credential_fp: str, db: Database | None = None) -> None:
        self._fp = credential_fp
        self._db = db
        self._sessions: dict[str, _Session] = {}

    async def load(self) -> None:
        """Load persisted sessions, dropping stale-credential and abandoned ones."""
        if self._db is None:
            return
        cutoff = int(time.time()) - COOKIE_MAX_AGE
        removed = await self._db.prune_auth_sessions(self._fp, cutoff)
        if removed:
            logger.info("Dropped %d stale login session(s)", removed)
        rows = await self._db.list_auth_sessions()
        self._sessions = {token_hash: _Session(refreshed_at) for token_hash, refreshed_at in rows}

    async def create(self) -> str:
        """Create a session and return the raw cookie token."""
        token = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)
        now = int(time.time())
        if self._db is not None:
            await self._db.insert_auth_session(token_hash, self._fp, now)
        self._sessions[token_hash] = _Session(now)
        await self._evict_overflow()
        return token

    def is_valid(self, token: str) -> bool:
        return bool(token) and _hash_token(token) in self._sessions

    def needs_refresh(self, token: str) -> bool:
        session = self._sessions.get(_hash_token(token))
        return session is not None and time.time() - session.refreshed_at >= REFRESH_INTERVAL

    async def refresh(self, token: str) -> None:
        """Record that the cookie for *token* was re-issued now."""
        token_hash = _hash_token(token)
        session = self._sessions.get(token_hash)
        if session is None:
            return
        now = int(time.time())
        session.refreshed_at = now
        if self._db is not None:
            try:
                await self._db.touch_auth_session(token_hash, now)
            except Exception:
                # Non-fatal: the in-memory value is current and the next
                # refresh retries. Worst case after a restart is one extra refresh.
                logger.warning("Failed to persist session refresh", exc_info=True)

    async def revoke(self, token: str) -> None:
        """Invalidate *token*. Unknown tokens are a no-op."""
        token_hash = _hash_token(token)
        if self._db is not None:
            # Delete from the DB first: if it fails the session stays valid in
            # both places rather than reviving on the next restart.
            await self._db.delete_auth_session(token_hash)
        self._sessions.pop(token_hash, None)

    async def _evict_overflow(self) -> None:
        while len(self._sessions) > MAX_SESSIONS:
            oldest = min(self._sessions, key=lambda h: self._sessions[h].refreshed_at)
            # Pop before awaiting so a concurrent create() cannot pick the same victim.
            self._sessions.pop(oldest, None)
            if self._db is not None:
                await self._db.delete_auth_session(oldest)
