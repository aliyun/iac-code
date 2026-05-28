# src/iac_code/utils/signals.py
"""Cross-platform signal handler installation."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import Callable
from typing import NoReturn


def install_signal_handler(
    loop: asyncio.AbstractEventLoop,
    sig: signal.Signals,
    handler: Callable[[], None],
) -> Callable[[], None]:
    """Install a signal handler compatible with Unix and Windows.

    On Unix, uses loop.add_signal_handler(). On Windows (ProactorEventLoop),
    falls back to signal.signal().

    Returns:
        A callable that removes the handler when invoked.
    """
    try:
        loop.add_signal_handler(sig, handler)

        def remove() -> None:
            with contextlib.suppress(RuntimeError):
                loop.remove_signal_handler(sig)

        return remove
    except (RuntimeError, NotImplementedError):
        previous = signal.getsignal(sig)
        signal.signal(sig, lambda signum, frame: handler())

        def remove() -> None:
            with contextlib.suppress(RuntimeError, OSError, ValueError):
                signal.signal(sig, previous)

        return remove


def reraise_default(signum: int) -> NoReturn:
    """Re-raise a signal using the OS default action.

    Unix: set the handler back to SIG_DFL and send the signal to ourselves
    via os.kill(getpid(), signum).

    Windows: the SIG_DFL + os.kill trick only works for SIGTERM (mapped
    to TerminateProcess) and bypasses any further cleanup. Exit cleanly
    with the conventional 128 + signum code instead.
    """
    if sys.platform == "win32":
        sys.exit(128 + signum)
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
    # Fallback: SIG_DFL normally terminates the process above, but if it
    # doesn't (e.g. the signal is ignored), exit with the conventional code.
    sys.exit(128 + signum)
