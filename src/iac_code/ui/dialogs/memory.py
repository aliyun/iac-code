"""Memory command dialog."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rich.console import Group, RenderableType
from rich.text import Text

from iac_code.i18n import _
from iac_code.ui.core.key_event import KeyEvent

MemoryAction = str


@dataclass
class _MemoryOption:
    label: str
    value: MemoryAction
    description: str


class MemoryDialog:
    """Claude-style memory selector with a focusable auto-memory toggle."""

    def __init__(
        self,
        *,
        project_path: Path,
        user_path: Path,
        auto_memory_dir: Path,
        auto_memory_enabled: bool,
        initial_focus_action: MemoryAction | None = None,
        on_toggle: Callable[[bool], None] | None = None,
    ) -> None:
        self.project_path = project_path
        self.user_path = user_path
        self.auto_memory_dir = auto_memory_dir
        self.auto_memory_enabled = auto_memory_enabled
        self._on_toggle = on_toggle
        self.focused_index = self._focus_index_for_action(initial_focus_action)
        self.result: MemoryAction | None = None
        self.done = False

    def run(self) -> MemoryAction | None:
        """Run the dialog in raw terminal mode."""
        from rich.console import Console

        from iac_code.ui.core.in_place_render import InPlaceRenderer
        from iac_code.ui.core.raw_input import RawInputCapture

        renderer = InPlaceRenderer(Console())
        try:
            with RawInputCapture() as cap:
                while not self.done:
                    renderer.render(self.render())
                    key_event = cap.read_key(timeout=0.1)
                    if key_event is not None:
                        self.handle_key(key_event)
        finally:
            renderer.clear()
        return self.result

    def render(self) -> RenderableType:
        return Group(*(Text(line) for line in self.render_lines()))

    def render_lines(self) -> list[str]:
        lines = [
            "  " + _("Memory"),
            "",
            self._format_row(
                _("Auto-memory: {state}").format(state=_("on") if self.auto_memory_enabled else _("off")), 0
            ),
            "",
        ]
        for index, option in enumerate(self._options(), start=1):
            label = "{index}. {label}".format(index=index, label=option.label)
            padding = max(2, 28 - _display_width(label))
            lines.append(self._format_row(label + (" " * padding) + option.description, index))
        lines.extend(["", "  " + _("Enter to confirm · Esc to cancel")])
        return lines

    def handle_key(self, key_event: KeyEvent) -> bool:
        key = key_event.key
        ctrl = key_event.ctrl
        options = self._options()
        if key == "up" or (ctrl and key == "p"):
            self.focused_index = max(0, self.focused_index - 1)
            return True
        if key == "down" or (ctrl and key == "n"):
            self.focused_index = min(len(options), self.focused_index + 1)
            return True
        if key == "enter":
            if self.focused_index == 0:
                self.auto_memory_enabled = not self.auto_memory_enabled
                self.focused_index = min(self.focused_index, len(self._options()))
                if self._on_toggle is not None:
                    self._on_toggle(self.auto_memory_enabled)
                return True
            self.result = options[self.focused_index - 1].value
            self.done = True
            return True
        if key == "escape":
            self.result = None
            self.done = True
            return True
        return False

    def _format_row(self, content: str, index: int) -> str:
        marker = "❯" if self.focused_index == index else " "
        return "  {marker} {content}".format(marker=marker, content=content)

    def _options(self) -> list[_MemoryOption]:
        options = [
            _MemoryOption(
                label=_("Project memory"),
                value="project",
                description=_("Saved in {path}").format(path=self.project_path),
            ),
            _MemoryOption(
                label=_("User memory"),
                value="user",
                description=_("Saved in {path}").format(path=self.user_path),
            ),
        ]
        if self.auto_memory_enabled:
            options.append(
                _MemoryOption(
                    label=_("Open auto-memory folder"),
                    value="folder",
                    description=str(self.auto_memory_dir),
                )
            )
        return options

    def _focus_index_for_action(self, action: MemoryAction | None) -> int:
        if action is None:
            return 0
        for index, option in enumerate(self._options(), start=1):
            if option.value == action:
                return index
        return 0


def _display_width(text: str) -> int:
    from rich.cells import cell_len

    return cell_len(text)
