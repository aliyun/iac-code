"""Bounded, request-owned deferral of rebuildable A2A projections."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from iac_code.a2a.backup import await_fenced
from iac_code.a2a.pipeline_performance import a2a_extreme_performance_enabled

logger = logging.getLogger(__name__)
_MAX_PROJECTIONS = 64
_MAX_DELAY_SECONDS = 5.0
_current: ContextVar[FirstDisplayPersistence | None] = ContextVar("a2a_first_display_persistence", default=None)
_routes: dict[tuple[int, str, str], FirstDisplayPersistence] = {}
_route_lock = threading.RLock()


class FirstDisplayPersistence:
    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.created_ns = time.monotonic_ns()
        self.active = True
        self._lock = threading.RLock()
        self._flush_lock = asyncio.Lock()
        self._pending: dict[Any, Callable[[], Awaitable[None]]] = {}
        self._bindings: set[tuple[int, str, str]] = set()
        self._timer: asyncio.TimerHandle | None = None
        self._timer_task: asyncio.Task[None] | None = None

    def bind(self, task_id: str, context_id: str, *, namespace: int = 0) -> None:
        if not task_id or not context_id or not self.active:
            return
        key = (namespace, task_id, context_id)
        with _route_lock:
            previous = _routes.get(key)
            # A resident producer may retain the preceding HTTP request's context.
            if previous is None or previous.created_ns <= self.created_ns:
                _routes[key] = self
                self._bindings.add(key)

    def defer(self, key: Any, callback: Callable[[], Awaitable[None]]) -> bool:
        with self._lock:
            if not self.active or (key not in self._pending and len(self._pending) >= _MAX_PROJECTIONS):
                return False
            self._pending[key] = callback
        self.loop.call_soon_threadsafe(self._ensure_timer)
        return True

    def discard(self, key: Any) -> None:
        with self._lock:
            self._pending.pop(key, None)

    def _ensure_timer(self) -> None:
        if self.active and self._pending and self._timer is None:
            self._timer = self.loop.call_later(_MAX_DELAY_SECONDS, self._start_timer_flush)

    def _start_timer_flush(self) -> None:
        self._timer = None
        self._timer_task = self.loop.create_task(self._timed_flush())

    async def _timed_flush(self) -> None:
        try:
            await self.flush()
        except Exception as exc:
            logger.warning("A2A deferred projection flush failed error_type=%s", type(exc).__name__)
        finally:
            self._ensure_timer()

    async def flush(self) -> None:
        async with self._flush_lock:
            with self._lock:
                pending = list(self._pending.items())
            for key, callback in pending:
                await callback()
                with self._lock:
                    if self._pending.get(key) is callback:
                        self._pending.pop(key, None)

    async def finish(self) -> None:
        self.active = False
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        try:
            await await_fenced(self.flush())
        except Exception as exc:
            # These are optional projections after wire delivery. Retain failed
            # work for the final close retry without breaking a delivered answer.
            logger.warning("A2A deferred projection flush failed error_type=%s", type(exc).__name__)

    async def aclose(self) -> None:
        try:
            await self.finish()
            if self._timer_task is not None:
                await await_fenced(self._timer_task)
        finally:
            with _route_lock:
                for key in self._bindings:
                    if _routes.get(key) is self:
                        _routes.pop(key, None)


@contextmanager
def first_display_persistence_scope(*, enabled: bool = True) -> Iterator[FirstDisplayPersistence | None]:
    window = FirstDisplayPersistence() if enabled and a2a_extreme_performance_enabled() else None
    token = _current.set(window)
    try:
        yield window
    finally:
        _current.reset(token)


def bind_first_display_persistence(task_id: str, context_id: str, *, namespace: int = 0) -> None:
    if window := _current.get():
        window.bind(task_id, context_id, namespace=namespace)


def first_display_persistence(task_id: str, context_id: str, *, namespace: int = 0) -> FirstDisplayPersistence | None:
    key = (namespace, task_id, context_id)
    with _route_lock:
        window = _routes.get(key)
    if window is None:
        window = _current.get()
        if window is not None and window._bindings and key not in window._bindings:
            return None
    return window if window is not None and window.active else None
