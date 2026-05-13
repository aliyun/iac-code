"""HistorySearch dialog — search conversation history with Ctrl+R."""

from __future__ import annotations

from typing import Any, Callable

from rich.panel import Panel

from iac_code.i18n import _
from iac_code.ui.components.fuzzy_picker import FuzzyPicker, PickerItem


class HistorySearch:
    """Dialog for searching conversation history.

    Shows user messages most-recent-first; selecting one returns the full text.
    """

    def __init__(
        self,
        messages: list[dict[str, Any]],
        on_select: Callable[[str], None],
        on_cancel: Callable[[], None],
        keybinding_manager: object | None = None,
    ) -> None:
        self._messages = messages
        self._on_select = on_select
        self._on_cancel = on_cancel
        self._km = keybinding_manager
        self._result: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> str | None:
        """Open the history search picker and return selected message text or None."""
        items = self._build_items()
        result_holder: list[str] = []
        cancelled = [False]

        def _on_select(item: PickerItem) -> None:
            content: str = item.metadata
            result_holder.append(content)
            self._on_select(content)

        def _on_cancel() -> None:
            cancelled[0] = True
            self._on_cancel()

        picker = FuzzyPicker(
            items=items,
            on_select=_on_select,
            on_cancel=_on_cancel,
            title=_("Search Conversation History"),
            placeholder=_("Type to search..."),
            empty_message=_("No conversation history"),
            render_preview=self._render_preview,
            keybinding_manager=self._km,
        )
        picker.run()

        return result_holder[0] if result_holder else None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_items(self) -> list[PickerItem]:
        """Extract user messages, most recent first, as PickerItems."""
        items: list[PickerItem] = []
        user_messages = [m for m in self._messages if m.get("role") == "user"]
        # Most recent first
        for i, msg in enumerate(reversed(user_messages)):
            content = msg.get("content", "")
            if isinstance(content, list):
                # Handle structured content blocks
                text_parts = [block.get("text", "") if isinstance(block, dict) else str(block) for block in content]
                content = " ".join(text_parts)
            content = str(content)
            items.append(
                PickerItem(
                    key=f"history-{i}",
                    display=content[:80],
                    metadata=content,
                    filter_text=content,
                )
            )
        return items

    def _render_preview(self, item: PickerItem) -> Panel:
        """Render full message text in a panel."""
        from rich.text import Text

        content: str = item.metadata
        return Panel(
            Text(content),
            title=_("Message Preview"),
            border_style="dim",
        )
