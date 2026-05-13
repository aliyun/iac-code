"""QuickOpen dialog — open files by fuzzy name search (Ctrl+P)."""

from __future__ import annotations

import os
from typing import Callable

from rich.panel import Panel
from rich.syntax import Syntax

from iac_code.i18n import _
from iac_code.ui.components.fuzzy_picker import FuzzyPicker, PickerItem
from iac_code.ui.suggestions.file_provider import _should_exclude_dir


class QuickOpen:
    """Dialog for opening files by fuzzy-searching the file tree.

    Selecting a file returns the absolute path and inserts ``@relative_path``
    into the input.
    """

    def __init__(
        self,
        root_dir: str,
        on_select: Callable[[str], None],
        on_cancel: Callable[[], None],
        keybinding_manager: object | None = None,
    ) -> None:
        self._root_dir = os.path.abspath(root_dir)
        self._on_select = on_select
        self._on_cancel = on_cancel
        self._km = keybinding_manager

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> str | None:
        """Open the quick-open picker and return selected file path or None."""
        items = self._build_items()
        result_holder: list[str] = []
        cancelled = [False]

        def _on_select(item: PickerItem) -> None:
            abs_path: str = item.metadata
            result_holder.append(abs_path)
            self._on_select(f"@{item.display}")

        def _on_cancel() -> None:
            cancelled[0] = True
            self._on_cancel()

        picker = FuzzyPicker(
            items=items,
            on_select=_on_select,
            on_cancel=_on_cancel,
            title=_("Open File"),
            placeholder=_("Type to search files..."),
            empty_message=_("No matching files"),
            render_preview=self._render_preview,
            keybinding_manager=self._km,
        )
        picker.run()

        return result_holder[0] if result_holder else None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_items(self) -> list[PickerItem]:
        """Walk the directory tree and build PickerItems for all files."""
        items: list[PickerItem] = []
        for dirpath, dirnames, filenames in os.walk(self._root_dir):
            # Prune excluded dirs in-place
            dirnames[:] = [d for d in dirnames if not _should_exclude_dir(d)]
            for fname in filenames:
                abs_path = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(abs_path, self._root_dir)
                items.append(
                    PickerItem(
                        key=f"file:{rel_path}",
                        display=rel_path,
                        metadata=abs_path,
                        filter_text=rel_path,
                    )
                )
        return items

    def _render_preview(self, item: PickerItem) -> Panel:
        """Render first 20 lines of the file with syntax highlighting."""
        abs_path: str = item.metadata
        ext = os.path.splitext(abs_path)[1].lstrip(".")

        try:
            with open(abs_path, encoding="utf-8", errors="replace") as fh:
                lines = []
                for i, line in enumerate(fh):
                    if i >= 20:
                        break
                    lines.append(line)
            content = "".join(lines)
        except OSError:
            content = ""

        syntax = Syntax(content, ext or "text", line_numbers=True)
        return Panel(syntax, title=item.display, border_style="dim")
