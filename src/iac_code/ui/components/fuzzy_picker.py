"""Fuzzy search selection component."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, cast

from rich.console import Group, RenderableType
from rich.text import Text

from iac_code.i18n import _
from iac_code.ui.components.search_box import SearchBox
from iac_code.ui.core.key_event import KeyEvent


@dataclass
class PickerItem:
    """An item that can be displayed and selected in the FuzzyPicker."""

    key: str
    display: str
    description: str = ""
    metadata: Any = None
    filter_text: str = ""

    def __post_init__(self) -> None:
        if not self.filter_text:
            self.filter_text = self.display


def fuzzy_match(query: str, text: str) -> float | None:
    """Subsequence matching with scoring.

    Returns None if no match.
    Scoring:
        +1 per matched character
        +0.5 * consecutive_run for consecutive matches
        +2.0 for prefix match
        +1.5 for word boundary match
    """
    if not query:
        return 0.0

    query_lower = query.lower()
    text_lower = text.lower()

    # Quick rejection: all query chars must exist in text
    for ch in query_lower:
        if ch not in text_lower:
            return None

    # Greedy subsequence matching with scoring
    score = 0.0
    ti = 0  # text index
    qi = 0  # query index
    consecutive = 0
    last_ti = -1
    prefix_bonus_given = False

    while qi < len(query_lower) and ti < len(text_lower):
        if text_lower[ti] == query_lower[qi]:
            score += 1.0

            # Prefix bonus: first query char matches at position 0
            if ti == 0 and qi == 0 and not prefix_bonus_given:
                score += 2.0
                prefix_bonus_given = True

            # Word boundary bonus: text char is at start or preceded by space/separator
            if ti == 0 or text_lower[ti - 1] in (" ", "_", "-", "/", "."):
                score += 1.5

            # Consecutive bonus
            if last_ti == ti - 1:
                consecutive += 1
                score += 0.5 * consecutive
            else:
                consecutive = 0

            last_ti = ti
            qi += 1
        ti += 1

    if qi < len(query_lower):
        # Did not match all query characters
        return None

    return score


class FuzzyPicker:
    """A fuzzy-search selection component.

    Combines a SearchBox with a filtered, scrollable list of items.
    Items can be a static list (filtered in-memory via fuzzy_match) or
    a callable that returns items dynamically (for async/server search).
    """

    def __init__(
        self,
        items: list[PickerItem] | Callable[[str], list[PickerItem]],
        on_select: Callable[[PickerItem], None],
        on_cancel: Callable[[], None] | None = None,
        title: str = "",
        placeholder: str = "",
        render_preview: Callable[[PickerItem], RenderableType] | None = None,
        visible_count: int = 10,
        debounce_ms: int = 0,
        empty_message: str = "",
        tab_action: Callable[[], None] | None = None,
        keybinding_manager: object | None = None,
    ) -> None:
        self._items = items
        self._on_select = on_select
        self._on_cancel = on_cancel
        self._title = title
        self._render_preview = render_preview
        self._visible_count = visible_count
        self._debounce_ms = debounce_ms
        self._empty_message = empty_message or _("No matches found")
        self._tab_action = tab_action
        self._km = keybinding_manager

        self._search_box = SearchBox(
            placeholder=placeholder,
            on_change=self._on_query_change,
        )
        self._filtered_items: list[PickerItem] = []
        self._focused_index: int = 0
        self._visible_from: int = 0
        self._done: bool = False
        self._result: PickerItem | None = None

        # Initial population
        self._update_filter("")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> PickerItem | None:
        """Blocking mode: run an event loop and return selected item or None."""
        from rich.cells import cell_len
        from rich.console import Console

        from iac_code.ui.core.in_place_render import InPlaceRenderer
        from iac_code.ui.core.raw_input import RawInputCapture

        renderer = InPlaceRenderer(Console())
        self._done = False
        self._result = None

        def cursor_pos() -> tuple[int, int]:
            # Search box is the first rendered row; ``"> "`` is 2 cells.
            sb = self._search_box
            col = 2 if not sb.value else 2 + cell_len(sb.value[: sb.cursor])
            return (0, col)

        try:
            with RawInputCapture() as cap:
                while not self._done:
                    renderer.render(self.render(), cursor_to=cursor_pos())
                    key_event = cap.read_key(timeout=0.1)
                    if key_event is not None:
                        self.handle_key(key_event)
        finally:
            renderer.clear()

        return self._result

    def handle_key(self, key_event: KeyEvent) -> bool:
        """Handle a key event. Returns True if consumed."""
        key = key_event.key
        ctrl = key_event.ctrl

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

        if key == "enter":
            if self._filtered_items:
                item = self._filtered_items[self._focused_index]
                self._result = item
                self._done = True
                self._on_select(item)
            return True

        if key == "escape":
            self._done = True
            if self._on_cancel is not None:
                self._on_cancel()
            return True

        if key == "tab" and self._tab_action is not None:
            self._tab_action()
            return True

        # Delegate to search box
        consumed = self._search_box.handle_key(key_event)
        return consumed

    def render(self) -> RenderableType:
        """Render search box + item list + match count + optional preview."""
        parts: list[RenderableType] = []

        # Search box
        parts.append(self._search_box.render())

        # Item list
        if not self._filtered_items:
            parts.append(Text(self._empty_message, style="dim"))
        else:
            visible = self._filtered_items[self._visible_from : self._visible_from + self._visible_count]
            for i, item in enumerate(visible):
                abs_i = self._visible_from + i
                is_focused = abs_i == self._focused_index
                parts.append(self._render_item(item, is_focused))

        # Match count
        parts.append(Text(self._get_match_count_text(), style="dim"))

        # Preview panel
        if (
            self._render_preview is not None
            and self._filtered_items
            and 0 <= self._focused_index < len(self._filtered_items)
        ):
            preview = self._render_preview(self._filtered_items[self._focused_index])
            parts.append(preview)

        return Group(*parts)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _on_query_change(self, query: str) -> None:
        self._update_filter(query)

    def _update_filter(self, query: str) -> None:
        """Update _filtered_items based on query. Reset focus to 0."""
        if callable(self._items):
            # Dynamic: call the function directly
            item_factory = cast(Callable[[str], list[PickerItem]], self._items)
            self._filtered_items = item_factory(query)
        else:
            # Static list: apply fuzzy matching
            static_items = self._items
            if not query:
                self._filtered_items = list(static_items)
            else:
                scored: list[tuple[float, PickerItem]] = []
                for item in static_items:
                    score = fuzzy_match(query, item.filter_text)
                    if score is not None:
                        scored.append((score, item))
                # Sort by score descending
                scored.sort(key=lambda x: x[0], reverse=True)
                self._filtered_items = [item for _, item in scored]

        self._focused_index = 0
        self._visible_from = 0

    def _move_focus(self, delta: int) -> None:
        """Move focus by delta, clamping at edges."""
        n = len(self._filtered_items)
        if n == 0:
            return
        new_idx = max(0, min(self._focused_index + delta, n - 1))
        self._focused_index = new_idx
        self._update_scroll()

    def _update_scroll(self) -> None:
        """Adjust visible_from so focused item is in view."""
        if self._focused_index < self._visible_from:
            self._visible_from = self._focused_index
        elif self._focused_index >= self._visible_from + self._visible_count:
            self._visible_from = self._focused_index - self._visible_count + 1

    def _get_match_count_text(self) -> str:
        total = len(self._items) if isinstance(self._items, list) else len(self._filtered_items)
        matched = len(self._filtered_items)
        if isinstance(self._items, list):
            return f"{matched}/{total} matches"
        return f"{matched} results"

    def _render_item(self, item: PickerItem, is_focused: bool) -> Text:
        text = Text()
        if is_focused:
            text.append("❯ ", style="bold cyan")
        else:
            text.append("  ")
        text.append(item.display, style="bold" if is_focused else "")
        if item.description:
            text.append(f"  {item.description}", style="dim")
        return text
