"""Alternate-screen transcript viewer for Ctrl+O.

Renders the whole conversation with all tool calls expanded (sub-agent
children fully listed, subagent prompts shown) while keeping tool *results*
compact — no full file dumps. Ctrl+O enters, Ctrl+O/Esc/Ctrl+C exits, no
scrolling. If the content overflows the viewport, the oldest rows are
dropped first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text

from iac_code.i18n import _
from iac_code.ui.core.key_event import KeyEvent
from iac_code.ui.core.raw_input import RawInputCapture
from iac_code.ui.core.screen import ScreenManager

if TYPE_CHECKING:
    from iac_code.ui.renderer import Renderer, _Segment, _ToolCallRecord


class TranscriptView:
    """Modal transcript view rendered in the alternate screen buffer."""

    def __init__(
        self,
        renderer: "Renderer",
        current_segments: "list[_Segment] | None" = None,
    ) -> None:
        self._renderer = renderer
        self._console = renderer.console
        self._screen = ScreenManager(self._console)
        # Segments of the in-progress turn that haven't been archived yet
        # (typically present when Ctrl+O is pressed mid-stream).
        self._current_segments = list(current_segments) if current_segments else []

    # ── Public entry ──────────────────────────────────────────────────

    def run(self) -> None:
        lines = self._render_lines()
        if not lines:
            return

        self._screen.enter_alternate_screen()
        try:
            with RawInputCapture() as cap:
                self._draw(lines)
                while True:
                    event = cap.read_key(timeout=None)
                    if event is None:
                        continue
                    if self._should_exit(event):
                        break
        finally:
            self._screen.leave_alternate_screen()

    # ── Rendering ─────────────────────────────────────────────────────

    def _render_lines(self) -> list[str]:
        """Render every turn and return a list of terminal rows."""
        r = self._renderer
        with self._console.capture() as cap:
            first = True
            for turn in r._message_history:
                if not first:
                    self._console.print()
                first = False
                if turn.role == "user":
                    line = Text()
                    line.append("❯ ", style="bold cyan")
                    line.append(turn.text)
                    self._console.print(line)
                else:
                    self._render_assistant_turn(turn.segments)

            # Un-archived live segments from the currently streaming turn.
            if self._current_segments:
                # Only emit a separator if there's prior history AND the
                # last history turn wasn't already an assistant turn being
                # extended (we'd end up double-spacing otherwise).
                if not first:
                    last = r._message_history[-1] if r._message_history else None
                    if last is None or last.role == "user":
                        self._console.print()
                self._render_assistant_turn(self._current_segments)

        raw = cap.get()
        lines = raw.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        return lines

    def _render_assistant_turn(self, segments: "list[_Segment]") -> None:
        r = self._renderer
        has_content = False
        text_flushed = False
        for seg in segments:
            if seg.kind == "text" and seg.text:
                if has_content:
                    self._console.print()
                for part in r._render_text_block(seg.text, continuation=text_flushed):
                    self._console.print(part)
                text_flushed = True
                has_content = True
            elif seg.kind == "tool" and seg.tool:
                if has_content:
                    self._console.print()
                self._render_tool(seg.tool)
                has_content = True
                text_flushed = False

    def _render_tool(self, rec: "_ToolCallRecord") -> None:
        """Print one tool call: verbose header (all children), compact result.

        For agent-style tools the sub-agent prompt is inserted between the
        tool-name line and the child-tool tree so the reader sees *what was
        asked* before *what ran*.
        """
        r = self._renderer
        # Header with verbose=True so every sub-agent child is listed (not
        # capped at 3) and tool-use detail is fully shown.
        saved = r._verbose
        r._verbose = True
        try:
            header = r._render_tool_header(rec)
        finally:
            r._verbose = saved

        # _render_tool_header returns a single Text with embedded newlines —
        # first line is "● Tool(detail)", the rest are child-tool rows.
        # Split so we can slide the prompt block in between.
        header_lines = header.split("\n")
        if header_lines:
            self._console.print(header_lines[0])
        self._render_subagent_prompt(rec)
        for line in header_lines[1:]:
            self._console.print(line)

        # Result stays compact so we never dump full file contents.
        result_line = r._render_tool_result(rec)
        if result_line:
            self._console.print(result_line)

    def _render_subagent_prompt(self, rec: "_ToolCallRecord") -> None:
        """For agent-style tools, print the prompt handed to the subagent."""
        prompt = ""
        if isinstance(rec.tool_input, dict):
            raw = rec.tool_input.get("prompt")
            if isinstance(raw, str):
                prompt = raw.strip()
        if not prompt:
            return
        label = Text()
        label.append("  ⎿  ", style="dim")
        label.append(_("Prompt:"), style="bold dim")
        self._console.print(label)
        for raw_line in prompt.splitlines() or [""]:
            row = Text("     ", style="dim")
            row.append(raw_line, style="dim")
            self._console.print(row)

    # ── Drawing ───────────────────────────────────────────────────────

    def _draw(self, lines: list[str]) -> None:
        _cols, rows = self._screen.get_size()
        # Last row is the footer; leave one blank row above it as a spacer.
        content_rows = max(1, rows - 2)
        visible = lines[-content_rows:] if len(lines) > content_rows else lines

        out = self._console.file
        out.write("\x1b[H\x1b[2J")
        for line in visible:
            out.write(line)
            out.write("\r\n")
        for _i in range(content_rows - len(visible)):
            out.write("\r\n")
        # Spacer row before the footer.
        out.write("\r\n")
        out.write(self._footer(rows))
        out.flush()

    def _footer(self, rows: int) -> str:
        hint = _("Showing transcript · ctrl+o to toggle")
        # `\x1b[K` clears the rest of the row so no left-over characters
        # remain after the hint (simpler + CJK-safe than padding with spaces,
        # which len() measures wrong for wide glyphs).
        return f"\x1b[{rows};1H\x1b[2K\x1b[2m{hint}\x1b[0m"

    # ── Input ─────────────────────────────────────────────────────────

    def _should_exit(self, event: KeyEvent) -> bool:
        if event.ctrl and event.key in ("o", "c"):
            return True
        if event.key == "escape":
            return True
        return False
