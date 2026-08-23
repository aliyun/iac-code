"""Streaming idle timeout watchdog."""

from __future__ import annotations

import time
from types import TracebackType

# Upper bound for one uninterrupted thinking phase. Long-tail qwen3.7-max turns
# were observed spending ~537s purely inside thinking, which the idle watchdog
# cannot catch because thinking deltas keep arriving.
DEFAULT_THINKING_PHASE_TIMEOUT = 300.0


class StreamIdleTimeoutError(Exception):
    def __init__(self, idle_timeout: float):
        super().__init__(f"Stream idle for more than {idle_timeout}s")
        self.idle_timeout = idle_timeout


class ThinkingPhaseTimeoutError(Exception):
    def __init__(self, phase_timeout: float, elapsed: float):
        super().__init__(f"Thinking phase ran for {elapsed:.1f}s, exceeding the {phase_timeout}s limit")
        self.phase_timeout = phase_timeout
        self.elapsed = elapsed


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


class ThinkingPhaseWatchdog:
    """Bounds how long a single uninterrupted thinking phase may run.

    ``StreamWatchdog`` only detects silence between events, so a model that keeps
    emitting thinking deltas can stall a turn indefinitely without tripping it.
    This watchdog instead measures wall time from the first thinking delta until
    visible output (text / tool use) arrives, which is when the phase ends.
    """

    def __init__(self, phase_timeout: float = DEFAULT_THINKING_PHASE_TIMEOUT):
        self._phase_timeout = phase_timeout
        self._phase_started: float | None = None

    @property
    def phase_timeout(self) -> float:
        return self._phase_timeout

    def elapsed(self) -> float:
        """Wall time spent in the current thinking phase; 0 when not thinking."""
        if self._phase_started is None:
            return 0.0
        return time.monotonic() - self._phase_started

    def on_thinking(self) -> None:
        """Record a thinking delta and check the phase budget.

        Raises ThinkingPhaseTimeoutError once the current phase exceeds the
        configured limit.
        """
        if self._phase_started is None:
            self._phase_started = time.monotonic()
            return
        elapsed = time.monotonic() - self._phase_started
        if elapsed > self._phase_timeout:
            raise ThinkingPhaseTimeoutError(self._phase_timeout, elapsed)

    def on_output(self) -> None:
        """End the current thinking phase; the next thinking delta starts a new one."""
        self._phase_started = None
