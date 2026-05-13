"""Streaming idle timeout watchdog."""

from __future__ import annotations

import time
from types import TracebackType


class StreamIdleTimeoutError(Exception):
    def __init__(self, idle_timeout: float):
        super().__init__(f"Stream idle for more than {idle_timeout}s")
        self.idle_timeout = idle_timeout


class StreamWatchdog:
    def __init__(self, idle_timeout: float = 90.0):
        self._idle_timeout = idle_timeout
        self._last_ping: float = 0.0
        self._running = False

    def start(self) -> None:
        self._last_ping = time.monotonic()
        self._running = True

    def stop(self) -> None:
        self._running = False

    def ping(self) -> None:
        """Record activity and check for idle timeout.

        Raises StreamIdleTimeoutError if the time since the last ping
        exceeds the idle timeout threshold.
        """
        now = time.monotonic()
        if self._running and self._last_ping > 0:
            if now - self._last_ping > self._idle_timeout:
                raise StreamIdleTimeoutError(self._idle_timeout)
        self._last_ping = now

    async def __aenter__(self) -> StreamWatchdog:
        self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None
    ) -> None:
        self.stop()
