"""Application state management."""

from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

from iac_code.types.permissions import PermissionDecision, PermissionMode

_PERMISSION_CACHE_MAX_SIZE = 128


def lookup_permission(
    cache: OrderedDict[str, PermissionDecision] | None,
    tool_name: str,
) -> PermissionDecision | None:
    """Look up a sticky permission decision and mark it as most-recent.

    Returns None when the cache is missing or the tool has no recorded
    decision. When found, the entry is moved to the end so LRU eviction
    preserves recency-of-use.
    """
    if cache is None:
        return None
    decision = cache.get(tool_name)
    if decision is None:
        return None
    cache.move_to_end(tool_name)
    return decision


def record_permission(
    cache: OrderedDict[str, PermissionDecision] | None,
    tool_name: str,
    decision: PermissionDecision,
) -> None:
    """Record a sticky permission decision, enforcing the LRU size cap."""
    if cache is None:
        return
    cache[tool_name] = decision
    cache.move_to_end(tool_name)
    _evict_lru(cache, _PERMISSION_CACHE_MAX_SIZE)


def _evict_lru(
    cache: OrderedDict[str, PermissionDecision],
    max_size: int,
) -> None:
    """Drop oldest entries until ``cache`` fits within ``max_size``."""
    while len(cache) > max_size:
        cache.popitem(last=False)


@dataclass
class AppState:
    """Global application state."""

    model: str = ""
    cwd: str = field(default_factory=os.getcwd)
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    messages: list = field(default_factory=list)  # list[Message]
    is_busy: bool = False
    always_allow_rules: OrderedDict[str, PermissionDecision] = field(default_factory=OrderedDict)
    spinner_text: str = ""
    context_usage_percent: float = 0.0
    effort_level: Any | None = None  # EffortLevel enum or None (avoid circular import)


class AppStateStore:
    """State store.

    Provides get/set/subscribe interfaces, UI components listen to state changes via subscribe.
    """

    def __init__(self, initial_state: AppState | None = None) -> None:
        self._state = initial_state or AppState()
        self._listeners: list[Callable[[AppState], None]] = []

    def get_state(self) -> AppState:
        """Get current state"""
        return self._state

    def set_state(self, updater: Callable[[AppState], AppState] | None = None, **kwargs) -> None:
        """Update state

        Two usage patterns:
        1. store.set_state(lambda s: dataclasses.replace(s, is_busy=True))
        2. store.set_state(is_busy=True)  # Shortcut
        """
        import dataclasses

        if updater is not None:
            self._state = updater(self._state)
        elif kwargs:
            self._state = dataclasses.replace(self._state, **kwargs)
        self._notify()

    def subscribe(self, listener: Callable[[AppState], None]) -> Callable[[], None]:
        """Subscribe to state changes, return unsubscribe function"""
        self._listeners.append(listener)

        def unsubscribe():
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def _notify(self) -> None:
        """Notify all listeners"""
        for listener in self._listeners:
            listener(self._state)


__all__ = [
    "AppState",
    "AppStateStore",
    "_PERMISSION_CACHE_MAX_SIZE",
    "lookup_permission",
    "record_permission",
]
