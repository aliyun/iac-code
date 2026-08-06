"""Loopback listener creation for the native-hosted Desktop sidecar."""

from __future__ import annotations

import errno
import socket
import sys
import time
from dataclasses import dataclass


class DesktopPortError(OSError):
    code = "port_in_use"


class DesktopPortDrainingError(DesktopPortError):
    code = "port_draining"


@dataclass(frozen=True)
class BoundLoopbackListener:
    socket: socket.socket
    requested_port: int
    port: int


def _new_loopback_socket() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.set_inheritable(False)
    if sys.platform == "win32":
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            listener.setsockopt(socket.SOL_SOCKET, exclusive, 1)
    else:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    return listener


def _listener_exists(port: int, *, timeout: float = 0.2) -> bool | None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        return probe.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return None
    finally:
        probe.close()


def bind_loopback_listener(
    requested_port: int,
    *,
    windows_drain_timeout: float = 2.0,
) -> BoundLoopbackListener:
    """Bind a non-inheritable loopback listener using the documented platform flags."""
    if requested_port < 0 or requested_port > 65535:
        raise ValueError("requested port must be in the range 0..65535")
    deadline = time.monotonic() + max(0.0, windows_drain_timeout)
    delay = 0.1
    last_error: OSError | None = None
    while True:
        listener = _new_loopback_socket()
        try:
            listener.bind(("127.0.0.1", requested_port))
            listener.listen(socket.SOMAXCONN)
            return BoundLoopbackListener(listener, requested_port, int(listener.getsockname()[1]))
        except OSError as exc:
            listener.close()
            last_error = exc
            if sys.platform != "win32" or requested_port == 0:
                raise DesktopPortError(exc.errno, str(exc)) from exc
            if exc.errno not in {errno.EACCES, errno.EADDRINUSE, 10013, 10048}:
                raise DesktopPortError(exc.errno, str(exc)) from exc
            listening = _listener_exists(requested_port)
            if listening is not False:
                raise DesktopPortError(exc.errno, str(exc)) from exc
            if time.monotonic() >= deadline:
                raise DesktopPortDrainingError(exc.errno, str(exc)) from exc
            time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            delay = min(delay * 2, 0.5)
    raise DesktopPortError(str(last_error))  # pragma: no cover
