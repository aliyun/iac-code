"""ModelPicker dialog with effort level support.

Thinking-mode capability is defined per-model in
``iac_code.providers.thinking``. This module re-exports the familiar
``EffortLevel`` / ``EFFORT_SYMBOLS`` names so callers stay compatible while
the underlying configuration is shared with the provider layer.
"""

from __future__ import annotations

from typing import Callable

from rich.console import Group, RenderableType
from rich.text import Text

from iac_code.providers.thinking import (
    EFFORT_ORDER as _EFFORT_ORDER_SHARED,
)
from iac_code.providers.thinking import (
    EFFORT_SYMBOLS,
    EffortLevel,
    get_thinking_spec,
)
from iac_code.ui.core.key_event import KeyEvent

_EFFORT_ORDER = list(_EFFORT_ORDER_SHARED)


class ModelPicker:
    """A picker dialog for selecting a model and optionally adjusting effort level.

    Navigation:
        ↑/↓   move focus (skipping group headers).
        ←/→   cycle effort level for the focused model (no-op if unsupported).
        Enter confirm selection.
        Escape cancel.
    """

    def __init__(
        self,
        initial_model: str,
        configured_providers: list[str],
        on_select: Callable[[str, EffortLevel | None], None],
        on_cancel: Callable[[], None],
        keybinding_manager: object | None = None,
    ) -> None:
        self._initial_model = initial_model
        self._configured_providers = configured_providers
        self._on_select = on_select
        self._on_cancel = on_cancel
        self._km = keybinding_manager

        # Build flat item list; each model item carries {"model": ..., "provider_key": ...}
        self._items: list[dict] = self._build_items()

        # Initialize effort levels from defaults for each (provider, model) pair
        # in the visible items. Models without effort default to None and are
        # never used downstream.
        self._efforts: dict[tuple[str, str], EffortLevel] = {}
        for item in self._items:
            if "model" not in item:
                continue
            spec = get_thinking_spec(item["provider_key"], item["model"])
            if spec.default_effort is not None:
                self._efforts[(item["provider_key"], item["model"])] = spec.default_effort

        # Set initial focus on initial_model (or first selectable)
        self._focused_index: int = 0
        self._set_initial_focus()

        self._done: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> tuple[str, EffortLevel | None] | None:
        """Blocking run via Dialog. Returns (model, effort) or None on cancel."""
        from iac_code.ui.components.dialog import Dialog
        from iac_code.ui.keybindings.manager import KeybindingManager

        km: KeybindingManager = self._km if isinstance(self._km, KeybindingManager) else KeybindingManager()

        result_holder: list[tuple[str, EffortLevel | None]] = []
        cancelled = [False]

        def _on_select(model: str, effort: EffortLevel | None) -> None:
            result_holder.append((model, effort))

        def _on_cancel() -> None:
            cancelled[0] = True

        orig_on_select = self._on_select
        orig_on_cancel = self._on_cancel
        self._on_select = _on_select
        self._on_cancel = _on_cancel

        dialog = Dialog(
            title="Select Model",
            keybinding_manager=km,
            on_cancel=_on_cancel,
            footer_hints=[
                ("↑↓", "navigate"),
                ("←→", "effort"),
                ("Enter", "select"),
                ("Esc", "cancel"),
            ],
        )

        dialog.run(
            body_builder=self.render,
            key_handler=self.handle_key,
        )

        self._on_select = orig_on_select
        self._on_cancel = orig_on_cancel

        if cancelled[0]:
            return None
        return result_holder[0] if result_holder else None

    def handle_key(self, key_event: KeyEvent) -> bool:
        """Handle a key event. Returns True if consumed."""
        key = key_event.key

        if key == "up":
            self._move_focus(-1)
            return True

        if key == "down":
            self._move_focus(1)
            return True

        if key == "left":
            pair = self._focused_pair()
            if pair is not None:
                self._cycle_effort(pair, -1)
            return True

        if key == "right":
            pair = self._focused_pair()
            if pair is not None:
                self._cycle_effort(pair, 1)
            return True

        if key == "enter":
            pair = self._focused_pair()
            if pair is not None:
                provider_key, model = pair
                spec = get_thinking_spec(provider_key, model)
                effort = self._efforts.get(pair) if spec.supports_effort else None
                self._on_select(model, effort)
                self._done = True
            return True

        if key == "escape":
            self._on_cancel()
            self._done = True
            return True

        return False

    def render(self) -> RenderableType:
        """Render the model picker list."""
        lines: list[RenderableType] = []
        for i, item in enumerate(self._items):
            if "header" in item:
                text = Text()
                text.append(item["header"], style="bold")
                lines.append(text)
            else:
                is_focused = i == self._focused_index
                lines.append(self._render_model_line(item, is_focused))
        return Group(*lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_items(self) -> list[dict]:
        """Build flat list of header and model items for configured providers."""
        from iac_code.commands.auth import PROVIDERS

        items: list[dict] = []
        for provider in PROVIDERS:
            key = str(provider["key_name"])
            if key not in self._configured_providers:
                continue
            items.append({"header": str(provider["display_name"])})
            for model in list(provider["models"]):
                items.append({"model": model, "provider_key": key})
        return items

    def _set_initial_focus(self) -> None:
        """Set focus to initial_model, or first selectable item."""
        for i, item in enumerate(self._items):
            if item.get("model") == self._initial_model:
                self._focused_index = i
                return
        # Fall back to first selectable
        for i, item in enumerate(self._items):
            if "model" in item:
                self._focused_index = i
                return

    def _focused_pair(self) -> tuple[str, str] | None:
        """Return (provider_key, model) for the focused item, or None."""
        item = self._items[self._focused_index] if self._items else None
        if item is None or "model" not in item:
            return None
        return item["provider_key"], item["model"]

    def _focused_model(self) -> str | None:
        """Return the model name at the current focused index, or None."""
        pair = self._focused_pair()
        return pair[1] if pair is not None else None

    def _move_focus(self, direction: int) -> None:
        """Move focus by direction (+1 or -1), skipping headers."""
        n = len(self._items)
        if n == 0:
            return
        current = self._focused_index
        step = 1 if direction > 0 else -1
        idx = current + step
        while 0 <= idx < n:
            if "model" in self._items[idx]:
                self._focused_index = idx
                return
            idx += step
        # No selectable found — stay at current

    def _cycle_effort(self, pair: tuple[str, str], direction: int) -> None:
        """Cycle effort level for a (provider_key, model) pair by direction (+1 or -1).

        Cycles through the model's explicit allowed list, so families with
        gaps (e.g. DeepSeek → ``[HIGH, MAX]``) skip unsupported levels.
        """
        provider_key, model = pair
        spec = get_thinking_spec(provider_key, model)
        if not spec.supports_effort:
            return

        allowed = spec.allowed_efforts
        current = self._efforts.get(pair, spec.default_effort)
        try:
            current_idx = allowed.index(current)
        except ValueError:
            current_idx = allowed.index(spec.default_effort)
        new_idx = max(0, min(len(allowed) - 1, current_idx + direction))
        self._efforts[pair] = allowed[new_idx]

    def _render_model_line(self, item: dict, is_focused: bool) -> Text:
        """Render a single model line."""
        model = item["model"]
        provider_key = item["provider_key"]
        text = Text()

        # Focus indicator
        if is_focused:
            text.append("> ", style="bold cyan")
        else:
            text.append("  ")

        # Model name
        style = "bold" if is_focused else ""
        text.append(model, style=style)

        # Current marker
        if model == self._initial_model:
            text.append(" (current)", style="dim green")

        # Effort symbol for effort-capable models
        spec = get_thinking_spec(provider_key, model)
        if spec.supports_effort and spec.default_effort is not None:
            effort = self._efforts.get((provider_key, model), spec.default_effort)
            symbol = EFFORT_SYMBOLS[effort]
            text.append(f" {symbol}", style="yellow")

        return text
