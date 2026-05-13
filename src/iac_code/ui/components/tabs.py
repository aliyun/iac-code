"""Tabs component for switching between named content panes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, cast

from rich.console import Group, RenderableType
from rich.rule import Rule
from rich.text import Text

from iac_code.ui.core.key_event import KeyEvent


@dataclass
class Tab:
    """Definition of a single tab."""

    id: str
    title: str
    content: RenderableType | Callable[[], RenderableType]


class Tabs:
    """A tab-bar component.

    Renders as:
        [Selected] | Other | Other
        ────────────────────────────
        <content of selected tab>

    Navigation:
        ← / → move between tabs without wrapping at edges.
    """

    def __init__(
        self,
        tabs: list[Tab],
        default_tab: str | None = None,
        on_tab_change: Callable[[str], None] | None = None,
        keybinding_manager: object | None = None,
    ) -> None:
        self._tabs = tabs
        self._on_tab_change = on_tab_change
        self._keybinding_manager = keybinding_manager

        if default_tab is not None:
            self._selected = default_tab
        elif tabs:
            self._selected = tabs[0].id
        else:
            self._selected = ""

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def selected_tab(self) -> str:
        return self._selected

    # ------------------------------------------------------------------
    # Key handling
    # ------------------------------------------------------------------

    def handle_key(self, key_event: KeyEvent) -> bool:
        """Handle left/right arrow keys to switch tabs.

        Returns True if the key was consumed, False otherwise.
        """
        key = key_event.key

        if key not in ("left", "right"):
            return False

        ids = [t.id for t in self._tabs]
        if not ids:
            return True  # consumed even if nothing to do

        try:
            idx = ids.index(self._selected)
        except ValueError:
            return True

        if key == "right":
            new_idx = min(idx + 1, len(ids) - 1)
        else:
            new_idx = max(idx - 1, 0)

        if new_idx != idx:
            self._selected = ids[new_idx]
            if self._on_tab_change is not None:
                self._on_tab_change(self._selected)

        return True

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self) -> RenderableType:
        """Render the tab bar, rule, and active tab content."""
        tab_bar = self._render_tab_bar()
        rule = Rule(style="dim")
        content = self._get_content()
        return Group(tab_bar, rule, content)

    def _render_tab_bar(self) -> Text:
        """Build the tab header line."""
        text = Text()
        for i, tab in enumerate(self._tabs):
            if i > 0:
                text.append(" | ", style="dim")
            if tab.id == self._selected:
                text.append(f"[{tab.title}]", style="bold cyan")
            else:
                text.append(tab.title, style="dim")
        return text

    def _get_content(self) -> RenderableType:
        """Return the content for the currently selected tab."""
        for tab in self._tabs:
            if tab.id == self._selected:
                if callable(tab.content):
                    content_factory = cast(Callable[[], RenderableType], tab.content)
                    return content_factory()
                return tab.content
        return Text("")
