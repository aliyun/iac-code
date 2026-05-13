"""Context-aware keybinding management system."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

from iac_code.ui.core.key_event import KeyEvent


def _format_key_display(key_id: str) -> str:
    """Format a key identifier for display.

    Examples:
        "ctrl+r" -> "Ctrl+R"
        "up" -> "Up"
        "ctrl+alt+x" -> "Ctrl+Alt+X"
        "escape" -> "Escape"
    """
    parts = key_id.split("+")
    return "+".join(p.capitalize() for p in parts)


@dataclass
class KeyBinding:
    """A single keybinding with its handler and context."""

    key: str  # e.g. "ctrl+r", "escape", "up"
    action: str  # e.g. "open_history_search", "cancel"
    context: str  # e.g. "global", "dialog", "select"
    handler: Callable[[], bool]  # Returns True if event consumed


class KeybindingManager:
    """Manages a stack of contexts with associated keybindings.

    Resolution walks from the highest-priority context (top of stack) downward.
    If a handler returns True the event is consumed and resolution stops.
    If a handler returns False the event bubbles to the next context.
    """

    def __init__(self) -> None:
        # context -> list of KeyBinding
        self._bindings: dict[str, list[KeyBinding]] = defaultdict(list)
        # ordered stack; index 0 = lowest priority, last = highest priority
        self._context_stack: list[str] = []

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, binding: KeyBinding) -> Callable[[], None]:
        """Register a keybinding and return an unregister function."""
        self._bindings[binding.context].append(binding)

        def _unregister() -> None:
            try:
                self._bindings[binding.context].remove(binding)
            except ValueError:
                pass

        return _unregister

    def unregister_context(self, context: str) -> None:
        """Remove all keybindings for the given context."""
        self._bindings[context] = []

    # ------------------------------------------------------------------
    # Context stack
    # ------------------------------------------------------------------

    def push_context(self, context: str) -> None:
        """Push a context onto the stack (highest priority)."""
        self._context_stack.append(context)

    def pop_context(self, context: str) -> None:
        """Remove the most recent occurrence of *context* from the stack."""
        for i in range(len(self._context_stack) - 1, -1, -1):
            if self._context_stack[i] == context:
                self._context_stack.pop(i)
                return

    @property
    def active_contexts(self) -> list[str]:
        """Return the context stack; index 0 = lowest priority."""
        return list(self._context_stack)

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve(self, key_event: KeyEvent) -> bool:
        """Attempt to resolve a key event through the context stack.

        Searches from highest priority (top of stack) downward.
        Returns True if any handler consumed the event.
        """
        key_id = key_event.key_id
        # iterate from highest to lowest priority
        for context in reversed(self._context_stack):
            for binding in self._bindings.get(context, []):
                if binding.key == key_id:
                    if binding.handler():
                        return True
        return False

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    def get_display_text(self, action: str, context: str) -> str | None:
        """Return formatted display text for a given action in a context.

        Returns None if not found.
        """
        for binding in self._bindings.get(context, []):
            if binding.action == action:
                return _format_key_display(binding.key)
        return None

    def get_hints_for_context(self, context: str) -> list[tuple[str, str]]:
        """Return a list of (display_text, action) tuples for the given context."""
        return [(_format_key_display(b.key), b.action) for b in self._bindings.get(context, [])]
