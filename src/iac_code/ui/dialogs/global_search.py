"""GlobalSearch dialog — search file contents with ripgrep/grep (Ctrl+F)."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable

from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from iac_code.i18n import _
from iac_code.ui.components.fuzzy_picker import FuzzyPicker, PickerItem


class GlobalSearch:
    """Dialog for searching file contents using ripgrep (or grep fallback).

    Selecting a result returns ``"file_path:line_number"`` and inserts
    ``@relative_path:line_number`` into the input.
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
        """Open the global-search picker and return ``file:line`` or None."""
        result_holder: list[str] = []

        def _on_select(item: PickerItem) -> None:
            key = item.key  # "abs_path:lineno"
            result_holder.append(key)
            self._on_select(f"@{item.display}")

        def _on_cancel() -> None:
            self._on_cancel()

        def _empty_message() -> str:
            return _("No matching content")

        picker = FuzzyPicker(
            items=self._search,
            on_select=_on_select,
            on_cancel=_on_cancel,
            title=_("Search in Files"),
            placeholder=_("Type to search content..."),
            empty_message=_("Enter search query"),
            render_preview=self._render_preview,
            debounce_ms=300,
            keybinding_manager=self._km,
        )
        picker.run()

        return result_holder[0] if result_holder else None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _search(self, query: str) -> list[PickerItem]:
        """Run ripgrep (or grep) and return PickerItems for each match."""
        if not query:
            return []

        try:
            output = self._run_search(query)
        except Exception:
            return []

        return self._parse_results(output)

    def _run_search(self, query: str) -> str:
        """Execute ripgrep or grep and return stdout."""
        if shutil.which("rg"):
            cmd = [
                "rg",
                "--line-number",
                "--no-heading",
                "--color=never",
                "--max-count=100",
                query,
                self._root_dir,
            ]
        else:
            cmd = [
                "grep",
                "-rn",
                "--include=*",
                "--color=never",
                query,
                self._root_dir,
            ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout

    def _parse_results(self, output: str) -> list[PickerItem]:
        """Parse ``filepath:linenum:matched_line`` output into PickerItems."""
        items: list[PickerItem] = []
        seen: set[str] = set()

        for line in output.splitlines():
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            file_path, lineno_str, matched_text = parts[0], parts[1], parts[2]
            if not lineno_str.isdigit():
                continue

            key = f"{file_path}:{lineno_str}"
            if key in seen:
                continue
            seen.add(key)

            rel_path = os.path.relpath(file_path, self._root_dir)
            display = f"{rel_path}:{lineno_str}  {matched_text.strip()}"

            items.append(
                PickerItem(
                    key=key,
                    display=display,
                    metadata={"file_path": file_path, "lineno": int(lineno_str), "text": matched_text},
                    filter_text=display,
                )
            )

        return items

    def _render_preview(self, item: PickerItem) -> Panel:
        """Render matched line ±5 lines with syntax highlighting."""
        meta = item.metadata
        if not isinstance(meta, dict):
            return Panel(Text(""), border_style="dim")

        file_path: str = meta["file_path"]
        lineno: int = meta["lineno"]
        ext = os.path.splitext(file_path)[1].lstrip(".")

        start = max(1, lineno - 5)
        end = lineno + 5

        try:
            with open(file_path, encoding="utf-8", errors="replace") as fh:
                all_lines = fh.readlines()
            snippet_lines = all_lines[start - 1 : end]
            content = "".join(snippet_lines)
        except OSError:
            content = ""

        syntax = Syntax(
            content,
            ext or "text",
            line_numbers=True,
            start_line=start,
            highlight_lines={lineno},
        )
        rel_path = os.path.relpath(file_path, self._root_dir)
        return Panel(syntax, title=f"{rel_path}:{lineno}", border_style="dim")
