"""Tests for the Windows msvcrt reader loop helper.

The helper bridges msvcrt key events into an asyncio queue from a daemon
thread; if the event loop is closed during shutdown,
``loop.call_soon_threadsafe`` raises ``RuntimeError`` and the thread must
exit cleanly without leaking the trace to stderr.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest


class _ScriptedMsvcrt:
    """msvcrt stand-in that delivers a fixed byte sequence then idles."""

    def __init__(self, byte_stream: list[int]):
        self._buffer = list(byte_stream)
        self._pos = 0

    def kbhit(self) -> bool:
        return self._pos < len(self._buffer)

    def getch(self) -> bytes:
        b = self._buffer[self._pos]
        self._pos += 1
        return bytes([b])


def _run_in_thread(target, *args, timeout=2.0) -> threading.Thread:
    """Run ``target(*args)`` in a daemon thread, return after it terminates
    (or raise if it does not exit within ``timeout``)."""
    t = threading.Thread(target=target, args=args, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        raise RuntimeError("reader thread did not exit within timeout")
    return t


class TestWindowsMsvcrtReaderLoop:
    @pytest.mark.asyncio
    async def test_forwards_bytes_to_queue(self):
        """Happy path: each byte produced by msvcrt.getch ends up on the queue."""
        from iac_code.ui.renderer import _windows_msvcrt_reader_loop

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[int] = asyncio.Queue()
        cancel_event = threading.Event()

        fake = _ScriptedMsvcrt([0x0F, 0x1B])  # Ctrl+O, Esc
        thread = threading.Thread(
            target=_windows_msvcrt_reader_loop,
            args=(fake, loop, queue, cancel_event),
            daemon=True,
        )
        thread.start()

        # Two bytes must arrive on the queue.
        b1 = await asyncio.wait_for(queue.get(), timeout=1.0)
        b2 = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert (b1, b2) == (0x0F, 0x1B)

        # Signal shutdown and join.
        cancel_event.set()
        thread.join(timeout=1.0)
        assert not thread.is_alive()

    def test_runtime_error_on_closed_loop_exits_thread(self):
        """If ``call_soon_threadsafe`` raises RuntimeError (loop closed),
        the thread must return immediately — not loop forever, not propagate."""
        from iac_code.ui.renderer import _windows_msvcrt_reader_loop

        fake_loop = MagicMock()
        fake_loop.call_soon_threadsafe.side_effect = RuntimeError("Event loop is closed")

        # The fake msvcrt has bytes ready; without the guard, the thread would
        # spin forever on the same RuntimeError. With the guard it exits on
        # the very first byte.
        fake = _ScriptedMsvcrt([0x41, 0x42, 0x43])
        cancel_event = threading.Event()
        queue: asyncio.Queue = asyncio.Queue()

        # ``_run_in_thread`` raises if the thread does not exit within 2s.
        _run_in_thread(_windows_msvcrt_reader_loop, fake, fake_loop, queue, cancel_event)

        # Only the first byte was attempted (then RuntimeError → return).
        assert fake_loop.call_soon_threadsafe.call_count == 1

    def test_cancel_event_terminates_idle_loop(self):
        """When kbhit() always returns False, setting the cancel_event must
        wake the wait() and exit the loop."""
        from iac_code.ui.renderer import _windows_msvcrt_reader_loop

        fake = _ScriptedMsvcrt([])  # never any bytes
        loop = MagicMock()
        cancel_event = threading.Event()
        queue: asyncio.Queue = asyncio.Queue()

        thread = threading.Thread(
            target=_windows_msvcrt_reader_loop,
            args=(fake, loop, queue, cancel_event),
            daemon=True,
        )
        thread.start()

        # Give the thread a chance to enter the wait, then cancel.
        cancel_event.set()
        thread.join(timeout=1.0)
        assert not thread.is_alive()
        # No bytes were available, so call_soon_threadsafe must never be called.
        loop.call_soon_threadsafe.assert_not_called()
