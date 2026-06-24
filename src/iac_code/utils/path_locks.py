from __future__ import annotations

import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class PathLockRegistry:
    """Weak per-path RLock registry that preserves uniqueness for live locks."""

    def __init__(self) -> None:
        self._locks: weakref.WeakValueDictionary[Path, threading.RLock] = weakref.WeakValueDictionary()
        self._guard = threading.Lock()

    @contextmanager
    def lock_for(self, path: str | Path) -> Iterator[threading.RLock]:
        lock = self._get_lock(Path(path))
        with lock:
            yield lock

    def prune(self) -> None:
        with self._guard:
            # Touching WeakValueDictionary materializes pending removals.
            list(self._locks.items())

    def _get_lock(self, path: Path) -> threading.RLock:
        resolved = path.resolve()
        with self._guard:
            lock = self._locks.get(resolved)
            if lock is None:
                lock = threading.RLock()
                self._locks[resolved] = lock
            return lock
