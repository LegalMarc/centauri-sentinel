"""uvicorn server subclass that leaves process shutdown to Sentinel."""

from __future__ import annotations

import contextlib
import signal
import threading
from typing import TYPE_CHECKING

import uvicorn
from uvicorn.server import HANDLED_SIGNALS

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import FrameType

# Open dashboard /stream (MJPEG) connections never end on their own, so uvicorn's
# graceful shutdown would wait forever. Past this, it cancels the stragglers and
# carries on. This plus the 15 s pause-publish wait in the watcher must stay
# inside the compose stop_grace_period (30 s).
GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS = 5


class SentinelServer(uvicorn.Server):
    """uvicorn.Server whose signal handling never re-raises the signal.

    Stock uvicorn restores the default handlers once serve() returns and then
    re-raises every captured signal, so SIGTERM kills the process (exit 143)
    before the watcher has unwound its in-flight pause or the database has been
    checkpointed. Here the first signal only sets ``should_exit``; a second one
    sets ``force_exit``, which makes uvicorn cancel remaining connections and tasks
    instead of waiting for them. It does not shorten the wait on an open
    ``/stream``: uvicorn (Python 3.12) still awaits ``wait_closed()``, and
    ``GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS`` is what bounds that. Exiting stays with
    ``_run``, which runs its shutdown sequence to the end.

    Outside Docker, once serve() returns SIGINT reverts to asyncio.Runner's
    handler, so a second Ctrl+C during the watcher's 15 s unwind cancels main and
    skips ``bot.stop()`` and the client closes (``db.checkpoint()`` still runs).
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        # Signals can only be handled from the main thread.
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        originals = {sig: signal.signal(sig, self.handle_exit) for sig in HANDLED_SIGNALS}
        try:
            yield
        finally:
            for sig, handler in originals.items():
                signal.signal(sig, handler)

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if self.should_exit:
            self.force_exit = True
        self.should_exit = True
