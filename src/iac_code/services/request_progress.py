"""Request-local runtime stage notifications, independent of timing-log collection."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_listener: ContextVar[Callable[[str], None] | None] = ContextVar("iac_request_progress", default=None)


@contextmanager
def observe_progress(listener: Callable[[str], None]) -> Iterator[None]:
    token = _listener.set(listener)
    try:
        yield
    finally:
        _listener.reset(token)


def notify_progress(stage: str) -> None:
    listener = _listener.get()
    if listener is not None:
        try:
            listener(stage)
        except Exception:
            # Optional display notifications cannot interrupt runtime work.
            pass


def is_progress_metadata(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("progress"), dict)
