"""Modal dialog container component."""

from __future__ import annotations

from typing import Callable

from rich.console import Console, RenderableType
from rich.panel import Panel
from rich.text import Text

from iac_code.ui.keybindings.manager import KeyBinding, KeybindingManager


class Dialog:
    """A modal dialog container that renders content inside a framed panel.

    Two usage modes:

    1. :meth:`show` — render frame + body once (e.g. inside an existing loop).
    2. :meth:`run` — manage the full event loop, including in-place rendering
       and dialog-context keybindings. Renders in the main buffer with
       erase-and-redraw between frames so nothing leaks into scrollback.
    """

    def __init__(
        self,
        title: str,
        keybinding_manager: KeybindingManager,
        on_cancel: Callable[[], None],
        subtitle: str | None = None,
        footer_hints: list[tuple[str, str]] | None = None,
        border_style: str = "blue",
    ) -> None:
        self._title = title
        self._km = keybinding_manager
        self._on_cancel = on_cancel
        self._subtitle = subtitle
        self._footer_hints = footer_hints or []
        self._border_style = border_style
        self._closed = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show(self, body: RenderableType) -> None:
        """Render the dialog frame and body to the console once."""
        Console().print(self._build_frame(body))

    def run(
        self,
        body_builder: Callable[[], RenderableType],
        key_handler: Callable | None = None,
    ) -> None:
        """Run an in-place event loop until the dialog is closed.

        Flow:

        1. Push "dialog" context to KeybindingManager.
        2. Register Escape and Ctrl+C as cancel.
        3. Loop: ``body_builder() → render → read_key → key_handler → km.resolve``
        4. On exit: erase the rendered frame, pop context, unregister.
        """
        from iac_code.ui.core.in_place_render import InPlaceRenderer
        from iac_code.ui.core.raw_input import RawInputCapture

        renderer = InPlaceRenderer(Console())
        self._km.push_context("dialog")

        def _cancel() -> bool:
            self._on_cancel()
            self.close()
            return True

        unregister_escape = self._km.register(
            KeyBinding(key="escape", action="dialog_cancel", context="dialog", handler=_cancel)
        )
        unregister_ctrl_c = self._km.register(
            KeyBinding(key="ctrl+c", action="dialog_cancel", context="dialog", handler=_cancel)
        )

        try:
            with RawInputCapture() as cap:
                while not self._closed:
                    body = body_builder()
                    renderer.render(self._build_frame(body))
                    key_event = cap.read_key(timeout=0.1)
                    if key_event is None:
                        continue
                    consumed = False
                    if key_handler is not None:
                        consumed = key_handler(key_event)
                    if not consumed:
                        self._km.resolve(key_event)
        finally:
            renderer.clear()
            unregister_escape()
            unregister_ctrl_c()
            self._km.pop_context("dialog")

    def close(self) -> None:
        """Mark the dialog closed; the next loop iteration exits."""
        self._closed = True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_panel_body(self, body: RenderableType) -> RenderableType:
        """Assemble subtitle + body + footer into a single renderable."""
        from rich.console import Group

        parts: list[RenderableType] = []

        if self._subtitle:
            parts.append(Text(self._subtitle, style="dim"))

        parts.append(body)

        if self._footer_hints:
            footer = self._build_footer()
            parts.append(footer)

        if len(parts) == 1:
            return parts[0]
        return Group(*parts)

    def _build_frame(self, body: RenderableType) -> Panel:
        """Build the full Panel with title, body, and footer."""
        panel_body = self._build_panel_body(body)
        title_text = Text(self._title, style="bold")
        return Panel(panel_body, title=title_text, border_style=self._border_style)

    def _build_footer(self) -> Text:
        """Build footer hints line."""
        text = Text()
        for i, (key_display, action) in enumerate(self._footer_hints):
            if i > 0:
                text.append("  ")
            text.append(key_display, style="bold cyan")
            text.append(f" {action}", style="dim")
        return text
