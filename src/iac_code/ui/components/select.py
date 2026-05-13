"""Enhanced selector component with TextOption and InputOption support."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from rich.console import Group, RenderableType
from rich.text import Text

from iac_code.ui.components.search_box import SearchBox
from iac_code.ui.core.key_event import KeyEvent


@dataclass
class TextOption:
    """A selectable text option."""

    label: str
    value: Any
    description: str = ""
    disabled: bool = False


@dataclass
class InputOption:
    """An option that opens an inline text input when selected."""

    label: str
    value: Any
    placeholder: str = ""
    initial_value: str = ""
    on_change: Callable[[str], None] | None = None


OptionType = TextOption | InputOption


class SelectLayout(Enum):
    COMPACT = "compact"
    EXPANDED = "expanded"
    COMPACT_VERTICAL = "compact_vertical"


@dataclass
class SelectState:
    focused_index: int = 0
    visible_from: int = 0
    visible_to: int = 0
    is_in_input: bool = False
    input_values: dict[Any, str] = field(default_factory=dict)


class Select:
    """An enhanced selector component that supports TextOption and InputOption.

    Navigation:
        ↑/↓/Ctrl+P/Ctrl+N move focus (skipping disabled options).
        PageUp/PageDown moves by visible_count.
        No wrapping at edges.
        Enter selects TextOption or enters edit mode for InputOption.
        Escape cancels (or exits edit mode if in one).
    """

    def __init__(
        self,
        options: list[OptionType],
        default_value: Any = None,
        layout: SelectLayout = SelectLayout.EXPANDED,
        visible_count: int = 10,
        keybinding_manager: object | None = None,
    ) -> None:
        self._options = options
        self._layout = layout
        self._visible_count = visible_count
        self._keybinding_manager = keybinding_manager

        self.state = SelectState()

        # Callbacks set externally or by run()
        self._on_select: Callable[[Any], None] | None = None
        self._on_cancel: Callable[[], None] | None = None
        self._done: bool = False
        self._result: Any = None

        # Active search box for InputOption editing
        self._active_search_box: SearchBox | None = None

        # Set initial focus based on default_value
        if default_value is not None:
            for i, opt in enumerate(options):
                if opt.value == default_value:
                    self.state.focused_index = i
                    break

        # Initialize viewport
        self._update_viewport()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> Any | None:
        """Blocking mode: enter raw input and loop until selection or cancel."""
        from rich.console import Console

        from iac_code.ui.core.in_place_render import InPlaceRenderer
        from iac_code.ui.core.raw_input import RawInputCapture

        renderer = InPlaceRenderer(Console())
        result_holder: list[Any] = []
        cancelled = [False]

        def on_select(value: Any) -> None:
            result_holder.append(value)
            self._done = True

        def on_cancel() -> None:
            cancelled[0] = True
            self._done = True

        self._on_select = on_select
        self._on_cancel = on_cancel

        try:
            with RawInputCapture() as cap:
                while not self._done:
                    renderer.render(self.render())
                    key_event = cap.read_key(timeout=0.1)
                    if key_event is not None:
                        self.handle_key(key_event)
        finally:
            renderer.clear()

        if cancelled[0]:
            return None
        return result_holder[0] if result_holder else None

    def render(self) -> RenderableType:
        """Render the select component."""
        lines: list[RenderableType] = []
        visible_opts = self._options[self.state.visible_from : self.state.visible_to]
        for i, opt in enumerate(visible_opts):
            abs_i = self.state.visible_from + i
            is_focused = abs_i == self.state.focused_index
            lines.append(self._render_option(opt, is_focused, abs_i))
        return Group(*lines)

    def handle_key(self, key_event: KeyEvent) -> bool:
        """Handle a key event. Returns True if consumed."""
        key = key_event.key
        ctrl = key_event.ctrl

        # If we're in input edit mode, delegate to search box
        if self.state.is_in_input and self._active_search_box is not None:
            if key == "escape":
                # Exit edit mode without cancelling
                self.state.is_in_input = False
                self._active_search_box = None
                return True
            if key == "enter":
                # Commit the input value
                opt = self._options[self.state.focused_index]
                self.state.input_values[opt.value] = self._active_search_box.value
                self.state.is_in_input = False
                if self._on_select is not None:
                    self._on_select(opt.value)
                self._active_search_box = None
                return True
            return self._active_search_box.handle_key(key_event)

        # Navigation
        if key == "up" or (ctrl and key == "p"):
            self._move_focus(-1)
            return True

        if key == "down" or (ctrl and key == "n"):
            self._move_focus(1)
            return True

        if key == "pageup":
            self._move_focus(-self._visible_count)
            return True

        if key == "pagedown":
            self._move_focus(self._visible_count)
            return True

        # Selection / cancel
        if key == "enter":
            return self._handle_enter()

        if key == "escape":
            if self._on_cancel is not None:
                self._on_cancel()
            return True

        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _move_focus(self, delta: int) -> None:
        """Move focus by delta steps, skipping disabled options, clamping at edges."""
        n = len(self._options)
        if n == 0:
            return

        current = self.state.focused_index
        step = 1 if delta > 0 else -1
        remaining = abs(delta)

        while remaining > 0:
            next_idx = current + step
            if next_idx < 0 or next_idx >= n:
                break  # No wrapping
            current = next_idx
            opt = self._options[current]
            is_disabled = isinstance(opt, TextOption) and opt.disabled
            if not is_disabled:
                remaining -= 1

        self.state.focused_index = current
        self._update_viewport()

    def _handle_enter(self) -> bool:
        """Handle enter key press."""
        if not self._options:
            return False

        opt = self._options[self.state.focused_index]

        # Don't allow selection of disabled options
        if isinstance(opt, TextOption) and opt.disabled:
            return False

        if isinstance(opt, InputOption):
            # Enter edit mode
            initial = self.state.input_values.get(opt.value, opt.initial_value)
            on_change = opt.on_change
            self._active_search_box = SearchBox(
                placeholder=opt.placeholder,
                initial_value=initial,
                on_change=on_change,
            )
            self.state.is_in_input = True
            return True

        # TextOption: select it
        if self._on_select is not None:
            self._on_select(opt.value)
        return True

    def _update_viewport(self) -> None:
        """Update visible_from and visible_to so focused_index is visible."""
        n = len(self._options)
        count = min(self._visible_count, n)

        if count == 0:
            self.state.visible_from = 0
            self.state.visible_to = 0
            return

        # Clamp focused_index
        fi = max(0, min(self.state.focused_index, n - 1))

        vf = self.state.visible_from
        vt = self.state.visible_to

        # Initialise if not set
        if vt == 0:
            vt = count

        # Scroll down
        if fi >= vt:
            vt = fi + 1
            vf = vt - count

        # Scroll up
        if fi < vf:
            vf = fi
            vt = vf + count

        # Clamp
        vf = max(0, vf)
        vt = min(n, vt)

        self.state.visible_from = vf
        self.state.visible_to = vt

    def _render_option(self, opt: OptionType, is_focused: bool, index: int) -> Text:
        """Render a single option line."""
        text = Text()

        if is_focused:
            text.append("❯ ", style="bold cyan")
        else:
            text.append("  ")

        if isinstance(opt, TextOption):
            style = "dim" if opt.disabled else ("bold" if is_focused else "")
            text.append(opt.label, style=style)
            if opt.description:
                text.append(f"  {opt.description}", style="dim")
        elif isinstance(opt, InputOption):
            text.append(opt.label, style="bold" if is_focused else "")
            # Show current value if we have one
            current_val = self.state.input_values.get(opt.value, opt.initial_value)
            if self.state.is_in_input and is_focused and self._active_search_box is not None:
                text.append(": ")
                text.append_text(self._active_search_box.render())
            elif current_val:
                text.append(f": {current_val}", style="cyan")
            elif opt.placeholder:
                text.append(f": {opt.placeholder}", style="dim")

        return text
