"""Tests for sentinel/server.py: uvicorn signal handling that never re-raises."""

from __future__ import annotations

import signal
import threading
from typing import TYPE_CHECKING

import pytest
import uvicorn

from sentinel.server import GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS, SentinelServer
from sentinel.watcher.loop import _PAUSE_PUBLISH_CANCEL_WAIT_SECONDS

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import FrameType


async def _app(scope: object, receive: object, send: object) -> None:  # pragma: no cover
    return None


@pytest.fixture
def recorder() -> Iterator[list[int]]:
    """Stand-in for the process's previous SIGTERM handler.

    Stock uvicorn would re-raise the captured signal into it on exit, so any call
    here after the context closes means the override regressed.
    """
    seen: list[int] = []

    def previous(sig: int, frame: FrameType | None) -> None:
        seen.append(sig)

    original = signal.signal(signal.SIGTERM, previous)
    try:
        yield seen
    finally:
        signal.signal(signal.SIGTERM, original)


def test_sigterm_sets_should_exit_without_reraising(recorder: list[int]) -> None:
    server = SentinelServer(uvicorn.Config(_app))

    with server.capture_signals():
        assert signal.getsignal(signal.SIGTERM) == server.handle_exit
        signal.raise_signal(signal.SIGTERM)
        assert server.should_exit is True
        assert server.force_exit is False

    assert recorder == []  # not re-raised into the previous handler
    assert signal.getsignal(signal.SIGTERM) is not server.handle_exit  # restored


def test_second_signal_forces_exit(recorder: list[int]) -> None:
    server = SentinelServer(uvicorn.Config(_app))

    with server.capture_signals():
        signal.raise_signal(signal.SIGTERM)
        signal.raise_signal(signal.SIGTERM)
        assert server.force_exit is True

    assert recorder == []


def test_capture_signals_is_a_noop_off_the_main_thread() -> None:
    server = SentinelServer(uvicorn.Config(_app))
    before = signal.getsignal(signal.SIGTERM)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            with server.capture_signals():
                pass
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert errors == []
    assert signal.getsignal(signal.SIGTERM) == before


def test_graceful_shutdown_timeout_is_bounded() -> None:
    # Uvicorn's wait plus the watcher's pause-publish wait must fit the 30 s
    # compose stop_grace_period.
    assert 0 < GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS + _PAUSE_PUBLISH_CANCEL_WAIT_SECONDS < 30
