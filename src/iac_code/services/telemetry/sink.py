"""AnalyticsSink — routes events through the privacy gate to the Events pipeline.

Design:
  - Before `activate()`, events are queued in-memory (bounded by maxlen=10k).
  - After `activate()`, events go directly to the EventEmitter.
  - `drain_sync()` / `drain_soon()` flushes the pre-queue once activated.
  - The privacy gate blocks emission when no-telemetry / essential-traffic is on.
"""

from __future__ import annotations

import asyncio
from collections import deque
from threading import Lock
from typing import Any

from loguru import logger

from iac_code.services.telemetry.config import is_telemetry_disabled
from iac_code.services.telemetry.events import EventEmitter


class AnalyticsSink:
    """Privacy-gated event router with pre-activation queue."""

    def __init__(self, emitter: EventEmitter, queue_max: int = 10_000) -> None:
        self._emitter = emitter
        self._queue: deque[tuple[str, dict[str, Any]]] = deque(maxlen=queue_max)
        self._lock = Lock()
        self._active = False

    def log_event(self, event_name: str, metadata: dict[str, Any]) -> None:
        """Queue the event if not yet active, else gate+emit directly."""
        with self._lock:
            if not self._active:
                self._queue.append((event_name, metadata))
                return
        self._dispatch(event_name, metadata)

    def activate(self) -> None:
        """Mark the sink active. Idempotent. Does NOT drain — call drain_*()."""
        with self._lock:
            self._active = True

    def drain_sync(self) -> None:
        """Synchronously flush the pre-activation queue."""
        with self._lock:
            queued = list(self._queue)
            self._queue.clear()
        for name, meta in queued:
            self._dispatch(name, meta)

    def drain_soon(self) -> None:
        """Schedule an async drain on the running loop, if any. Else sync."""
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon(self.drain_sync)
            return
        except RuntimeError:
            pass
        self.drain_sync()

    def _dispatch(self, event_name: str, metadata: dict[str, Any]) -> None:
        logger.info("[event] {} {}", event_name, metadata)
        if is_telemetry_disabled():
            return
        self._emitter.emit(event_name, metadata)
