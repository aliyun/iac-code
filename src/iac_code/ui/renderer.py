"""Rich-based rendering engine.

Consumes StreamEvent from AgentLoop and renders via Rich Console + Live.
During a streaming turn, all output (text, tool calls, tool results) is
buffered and rendered in a single Live context.  Pressing Ctrl+O toggles
between compact (default) and verbose (expanded) views — the entire turn
is re-rendered on toggle.
"""

from __future__ import annotations

import asyncio
import copy
import os
import sys
import termios
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncGenerator, Awaitable, Callable

from rich._loop import loop_first
from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.live import Live
from rich.markdown import ListItem, Markdown
from rich.rule import Rule
from rich.segment import Segment
from rich.table import Table
from rich.text import Text

from iac_code.i18n import _
from iac_code.services.telemetry import add_metric, log_event
from iac_code.services.telemetry.names import Events, Metrics
from iac_code.tools.cloud.types import translate_status
from iac_code.types.stream_events import (
    CompactionEvent,
    ErrorEvent,
    MessageEndEvent,
    MessageStartEvent,
    PermissionRequestEvent,
    StackInstancesProgressEvent,
    StackProgressEvent,
    StreamEvent,
    SubAgentToolEvent,
    TaskNotificationEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    TombstoneEvent,
    ToolInputDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)
from iac_code.ui.components.select import OptionType, Select, SelectLayout, TextOption
from iac_code.ui.spinner import ShimmerSpinner

if TYPE_CHECKING:
    from iac_code.state.app_state import AppStateStore
    from iac_code.tools.base import ToolRegistry


class _DashListItem(ListItem):
    """ListItem that uses ``-`` instead of ``•`` for unordered bullets."""

    def render_bullet(self, console: Console, options: ConsoleOptions) -> RenderResult:
        render_options = options.update(width=options.max_width - 3)
        lines = console.render_lines(self.elements, render_options, style=self.style)
        bullet_style = console.get_style("markdown.item.bullet", default="none")
        bullet = Segment(" - ", bullet_style)
        padding = Segment(" " * 3, bullet_style)
        new_line = Segment("\n")
        for first, line in loop_first(lines):
            yield bullet if first else padding
            yield from line
            yield new_line


class _DashMarkdown(Markdown):
    """Markdown subclass that renders unordered list bullets as ``-``."""

    elements = {**Markdown.elements, "list_item_open": _DashListItem}


class _CropTop:
    """Rich renderable that crops content from the **top** to fit *max_height*.

    The inner renderable is fully rendered first (preserving all styling such
    as code-block highlighting), then only the bottom *max_height* lines are
    emitted.  This prevents Rich ``Live`` from pushing overflow into the
    terminal scrollback buffer — the root cause of duplicate-content bugs.
    """

    def __init__(self, inner, max_height: int) -> None:
        self._inner = inner
        self._max_height = max_height

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        render_options = options.update(height=None)
        lines = console.render_lines(self._inner, render_options, pad=False)
        if len(lines) > self._max_height:
            lines = lines[-self._max_height :]
        new_line = Segment("\n")
        for line in lines:
            yield from line
            yield new_line


# ── Turn-buffer data structures ─────────────────────────────────────


@dataclass
class _SubAgentChild:
    """A child tool call made by a sub-agent."""

    tool_name: str
    tool_input: dict
    is_done: bool = False
    is_error: bool = False


@dataclass
class _ToolCallRecord:
    """One tool invocation (use + optional result)."""

    tool_name: str
    tool_input: dict
    partial_input: str = ""
    result: str | None = None
    is_error: bool = False
    done: bool = False
    children: list[_SubAgentChild] | None = None
    start_time: float = 0.0
    progress_renderable: Any = None  # For stack progress display


@dataclass
class _Segment:
    """One segment of turn output — markdown text, a tool call, or a
    collapsed thinking-summary line."""

    kind: str  # "text" | "tool" | "thinking_summary"
    text: str = ""
    tool: _ToolCallRecord | None = None
    elapsed_seconds: float = 0.0  # for thinking_summary only


@dataclass
class RenderedTurn:
    """One complete turn of rendered content."""

    role: str  # "user" | "assistant"
    segments: list[_Segment] = field(default_factory=list)
    timestamp: float = 0.0
    text: str = ""  # For user turns, the raw input text


class Renderer:
    """Bridge between stream events and terminal output."""

    def __init__(
        self,
        console: Console,
        tool_registry: "ToolRegistry",
        status_callback: Callable[[], str] | None = None,
        app_state_store: "AppStateStore | None" = None,
    ) -> None:
        self.console = console
        self._tool_registry = tool_registry
        self._status_callback = status_callback
        self._verbose = False
        self._text_flushed = False  # tracks whether current text block was partially flushed
        self._message_history: list[RenderedTurn] = []
        # Set by _key_listener after the transcript view closes mid-stream so
        # the main event loop discards the stale Live/refresh_task and
        # rebuilds them before rendering the next event.
        self._stream_invalidated = False
        self._last_streaming_errors: list[str] = []
        # Optional AppStateStore so permission prompts can consult/update the
        # shared LRU cache at AppState.always_allow_rules. None in pure-unit
        # contexts where no session state is wired up.
        self._app_state_store = app_state_store

    # ── Footer (shown inside Live during streaming) ─────────────────

    def _build_footer(self) -> Group:
        """Build the persistent footer: separator + disabled input + status."""
        status_text = self._status_callback() if self._status_callback else ""
        status = Text(status_text, style="dim", justify="right")
        return Group(
            Rule(style="dim"),
            Text("❯ ", style="dim"),
            status,
        )

    def _with_footer(self, content) -> Group:
        """Wrap content with the persistent footer below it."""
        return Group(content, self._build_footer())

    # ── Static output (goes to scrollback) ──────────────────────────

    def print_user_message(self, text: str) -> None:
        t = Text()
        t.append("❯ ", style="bold cyan")
        t.append(text)
        self.console.print(t)

    def print_command_result(self, command: str, result: str) -> None:
        t = Text()
        t.append("  └ ", style="dim")
        t.append(result)
        self.console.print(t)

    def print_system_message(self, text: str, style: str = "yellow") -> None:
        self.console.print(Text(text, style=style))

    async def run_with_spinner(self, awaitable: Awaitable[Any], label: str) -> Any:
        """Show a transient spinner with ``label`` while ``awaitable`` runs.

        Used by slow local commands (e.g. /compact) so the UI doesn't look
        frozen during long async work. Returns the awaitable's result; any
        exception raised by the awaitable propagates after the spinner is
        torn down.
        """
        spinner = ShimmerSpinner(status=f"{label}...")
        live = Live(
            self._with_footer(spinner.render()),
            console=self.console,
            refresh_per_second=20,
            transient=True,
            vertical_overflow="visible",
        )

        async def _refresh() -> None:
            try:
                while True:
                    await asyncio.sleep(0.05)
                    live.update(self._with_footer(spinner.render()))
            except asyncio.CancelledError:
                pass

        live.start()
        refresh_task = asyncio.create_task(_refresh())
        try:
            return await awaitable
        finally:
            await self._stop_refresh(refresh_task)
            self._quiet_stop_live(live)

    def record_user_turn(self, text: str) -> None:
        """Record a user turn into message history."""
        self._message_history.append(RenderedTurn(role="user", text=text, timestamp=time.monotonic()))

    @property
    def message_history(self) -> list[RenderedTurn]:
        """All rendered turns in the conversation."""
        return self._message_history

    def _quiet_stop_live(self, target_live: Live | None) -> None:
        """Stop a Rich Live without scrolling its last render into scrollback.

        Rich's Live.stop() with ``transient=True`` runs this sequence:
          1. final refresh — renders content in place
          2. ``self.console.line()`` — writes a ``\\n``
          3. ``restore_cursor`` — CR + (UP + ERASE_LINE) × height

        Step 2 is the bug for us: when Live sits on the terminal's last row
        (which is always — our Live is pinned just above the input footer),
        that ``\\n`` scrolls the top row of the Live out of the viewport
        before step 3 can erase it. The evicted row is now in scrollback
        forever. Every stop leaks exactly one row — the header of whatever
        Live was showing, e.g. ``● 探索(...)`` — and they stack on repeat.

        We do the essential teardown ourselves and skip the ``line()``
        scroll altogether: stop the refresh thread, acquire Live's lock so
        we don't race a concurrent auto-refresh, erase the rendered area
        with plain ANSI (position-cursor pattern — works from the end of
        the render), pop the render hook and restore stdio.
        """
        if target_live is None or not getattr(target_live, "_started", False):
            return
        thread = getattr(target_live, "_refresh_thread", None)
        if thread is not None:
            try:
                thread.stop()
                thread.join(timeout=0.2)
            except Exception:
                pass
            target_live._refresh_thread = None

        lock = getattr(target_live, "_lock", None)
        acquired = False
        if lock is not None:
            try:
                lock.acquire()
                acquired = True
            except Exception:
                pass
        try:
            render = getattr(target_live, "_live_render", None)
            if render is not None:
                shape = getattr(render, "_shape", None)
                if shape is not None:
                    _, height = shape
                    if height > 0:
                        out = target_live.console.file
                        try:
                            # Cursor is at the end of the last rendered line.
                            # ``CR + ERASE`` clears that line, then each
                            # ``UP + ERASE`` walks up one row and clears it.
                            # Emits zero newlines, so no scroll, no leak.
                            out.write("\r\x1b[2K")
                            for _ in range(height - 1):
                                out.write("\x1b[A\x1b[2K")
                            out.flush()
                        except Exception:
                            pass
                    render._shape = None
            target_live._started = False
        finally:
            if acquired and lock is not None:
                try:
                    lock.release()
                except Exception:
                    pass

        for cleanup in (
            lambda: target_live._disable_redirect_io(),
            lambda: target_live.console.pop_render_hook(),
            lambda: target_live.console.clear_live(),
            lambda: target_live.console.show_cursor(True),
        ):
            try:
                cleanup()
            except Exception:
                pass

    def _render_turn_segments(self, segments: list[_Segment]) -> None:
        """Re-render all segments of a turn to console (used by expand toggle)."""
        has_content = False
        text_flushed = False
        for seg in segments:
            if seg.kind == "text" and seg.text:
                if has_content:
                    self.console.print()
                for part in self._render_text_block(seg.text, continuation=text_flushed):
                    self.console.print(part)
                text_flushed = True
                has_content = True
            elif seg.kind == "thinking_summary":
                if has_content:
                    self.console.print()
                label = _("Thought for {seconds:.1f}s").format(seconds=seg.elapsed_seconds)
                self.console.print(Text(f"▌ {label}", style="dim"))
                has_content = True
                text_flushed = False
            elif seg.kind == "tool" and seg.tool:
                if has_content:
                    self.console.print()
                self.console.print(self._render_tool_header(seg.tool))
                result_line = self._render_tool_result(seg.tool)
                if result_line:
                    self.console.print(result_line)
                has_content = True
                text_flushed = False
        # Show expand hint only when at least one tool actually has richer
        # verbose content to reveal.
        if not self._verbose and self._any_segment_has_verbose(segments):
            self.console.print(Text("  " + _("(ctrl+o to expand)"), style="dim"))

    def show_transcript(self, current_segments: "list[_Segment] | None" = None) -> None:
        """Open the transcript view in the alternate screen.

        ``current_segments`` are the live, un-archived segments of the in-
        progress assistant turn (if any); passing them lets the view show a
        running agent's child-tool list before it has been flushed to
        ``_message_history``.
        """
        from iac_code.ui.transcript_view import TranscriptView

        TranscriptView(self, current_segments=current_segments).run()

    # ── Render helpers ──────────────────────────────────────────────

    def _render_stack_progress(self, event: StackProgressEvent) -> Group:
        """Render stack progress as a Rich Group (title + table)."""
        stack_status_display = translate_status(event.status)
        title = Text(
            f"Stack: {event.stack_name}({event.stack_id})  [{stack_status_display}]  {event.progress_percentage:.0f}%",
            no_wrap=True,
        )
        table = Table(
            show_header=True,
            border_style="dim",
        )
        table.add_column(_("Resource"))
        table.add_column(_("Type"))
        table.add_column(_("Status"))
        for r in event.resources:
            status_icon = r.get("status_icon", "") if isinstance(r, dict) else ""
            status = r.get("status", "") if isinstance(r, dict) else ""
            table.add_row(
                r.get("name", ""),
                r.get("resource_type", ""),
                f"{status_icon} {translate_status(status)}",
            )
        return Group(title, table)

    def _render_instances_progress(self, event: StackInstancesProgressEvent) -> Group:
        """Render stack instances progress as a Rich Group (title + table)."""
        title = Text(
            f"StackGroup: {event.stack_group_name}  [{event.status}]  {event.progress_percentage}%",
            no_wrap=True,
        )
        table = Table(
            show_header=True,
            border_style="dim",
        )
        table.add_column(_("Account ID"))
        table.add_column(_("Region"))
        table.add_column(_("Status"))
        for i in event.instances:
            status_icon = i.get("status_icon", "")
            status = i.get("status", "")
            table.add_row(
                i.get("account_id", ""),
                i.get("region_id", ""),
                f"{status_icon} {status}",
            )
        return Group(title, table)

    def _has_verbose_content(self, rec: _ToolCallRecord) -> bool:
        """True if rendering this tool in verbose mode would differ from compact.

        Used to decide whether to show the ``(ctrl+o 展开)`` hint — pointless
        when the tool has nothing extra to reveal (e.g. the skill-load tool).
        """
        if not rec.done:
            return False
        # Agent tools show their child tool tree in verbose only.
        if rec.children:
            return True
        tool = self._tool_registry.get(rec.tool_name)
        if tool is None:
            return False
        if tool.render_tool_use_message(rec.tool_input, verbose=False) != tool.render_tool_use_message(
            rec.tool_input, verbose=True
        ):
            return True
        result = rec.result or ""
        return tool.render_tool_result_message(
            result, is_error=rec.is_error, verbose=False
        ) != tool.render_tool_result_message(result, is_error=rec.is_error, verbose=True)

    def _any_segment_has_verbose(self, segments: list[_Segment]) -> bool:
        """True if any tool segment has content that differs between modes."""
        return any(s.kind == "tool" and s.tool and self._has_verbose_content(s.tool) for s in segments)

    def _render_tool_header(self, rec: _ToolCallRecord) -> Text:
        """Render ``● ToolName(detail)`` line with optional child tool tree."""
        tool = self._tool_registry.get(rec.tool_name)
        tool_name = tool.user_facing_name(rec.tool_input) if tool else rec.tool_name
        detail = tool.render_tool_use_message(rec.tool_input, verbose=self._verbose) if tool else None

        line = Text()
        if not rec.done:
            phase = time.monotonic() % 1.0
            dot_style = "bold white" if phase < 0.5 else "dim white"
            line.append("● ", style=dot_style)
        elif rec.is_error:
            line.append("● ", style="bold red")
        else:
            line.append("● ", style="bold green")
        line.append(tool_name, style="bold")
        if detail:
            line.append(f"({detail})")

        # Render sub-agent child tool tree
        if rec.children:
            if rec.done:
                # Completed: show summary line
                elapsed = ""
                if rec.start_time > 0:
                    from iac_code.ui.spinner import _format_elapsed

                    elapsed = f" · {_format_elapsed(time.monotonic() - rec.start_time)}"
                child_count = len(rec.children)
                # Try to extract token info from result
                token_info = ""
                if rec.result:
                    import re

                    match = re.search(r"(\d+) tokens", rec.result)
                    if match:
                        tokens = int(match.group(1))
                        token_info = f" · {tokens / 1000:.1f}k tokens" if tokens >= 1000 else f" · {tokens} tokens"
                done_text = _("Done ({child_count} tool uses{token_info}{elapsed})").format(
                    child_count=child_count,
                    token_info=token_info,
                    elapsed=elapsed,
                )
                line.append(f"\n  └ {done_text}", style="dim")
            else:
                # In-progress: show recent child tools with tree connectors
                max_visible = 3 if not self._verbose else len(rec.children)
                visible = rec.children[-max_visible:]
                hidden_count = len(rec.children) - len(visible)

                for i, child in enumerate(visible):
                    tool_obj = self._tool_registry.get(child.tool_name)
                    child_display = tool_obj.user_facing_name(child.tool_input) if tool_obj else child.tool_name
                    child_detail = ""
                    if tool_obj:
                        d = tool_obj.render_tool_use_message(child.tool_input, verbose=self._verbose)
                        if d:
                            child_detail = f"({d})"
                    if i == 0:
                        line.append("\n  └ ", style="dim")
                    else:
                        line.append("\n    ", style="dim")
                    line.append(child_display, style="bold")
                    if child_detail:
                        line.append(child_detail, style="dim")

                if hidden_count > 0:
                    line.append(
                        "\n  " + _("+ {count} more tool uses (ctrl+o to expand)").format(count=hidden_count),
                        style="dim",
                    )

        return line

    def _render_tool_result(self, rec: _ToolCallRecord) -> Text | None:
        """Render ``  ⎿  result`` line (compact or verbose)."""
        if not rec.done:
            return None

        # For agent tools with children, the summary is already in the header
        if rec.children and not self._verbose:
            return None

        tool = self._tool_registry.get(rec.tool_name)
        result_text = None
        if tool:
            result_text = tool.render_tool_result_message(
                rec.result or "", is_error=rec.is_error, verbose=self._verbose
            )
        if result_text is None and rec.result:
            result_text = rec.result

        if not result_text:
            return None

        line = Text()
        line.append("  ⎿  ", style="dim")
        if rec.is_error:
            line.append(str(result_text), style="red")
        else:
            line.append(str(result_text))
        return line

    def _render_text_block(self, text: str, continuation: bool = False) -> list[Any]:
        """Render a text block with ``✦`` bullet prefix and indented content.

        Uses a 2-column grid so the bullet sits on the same line as the first
        line of text, and all subsequent lines are indented to align.

        When *continuation* is True the bullet is replaced with blank space,
        keeping indentation aligned with a preceding flushed block.
        """
        tbl = Table.grid(padding=0)
        tbl.add_column(width=2, no_wrap=True)
        tbl.add_column()
        bullet = Text("  ") if continuation else Text("✦ ", style="bold white")
        tbl.add_row(bullet, _DashMarkdown(text))
        return [tbl]

    @staticmethod
    def _find_safe_split_pos(text: str) -> tuple[int, bool]:
        """Find the last ``\\n\\n`` that is **outside** a fenced code block.

        Returns ``(position, currently_in_fence)``.  *position* is -1 when no
        safe split point exists.
        """
        in_fence = False
        last_safe = -1
        i = 0
        while i < len(text):
            if text[i : i + 3] == "```":
                in_fence = not in_fence
                i += 3
                # skip optional info-string on opening fence
                while i < len(text) and text[i] != "\n":
                    i += 1
                continue
            if not in_fence and text[i : i + 2] == "\n\n":
                last_safe = i
            i += 1
        return last_safe, in_fence

    def _render_segments(
        self,
        segments: list[_Segment],
        spinner: ShimmerSpinner | None,
        text_buffer: str,
        task_spinner: ShimmerSpinner | None = None,
        *,
        thinking_buffer: str = "",
    ) -> Group | _CropTop:
        """Render all buffered segments + current spinner into a Group."""
        parts: list[Any] = []
        has_content = False

        for seg in segments:
            if seg.kind == "text":
                if has_content:
                    parts.append(Text())  # blank line between segments
                parts.extend(self._render_text_block(seg.text))
                has_content = True
            elif seg.kind == "thinking_summary":
                if has_content:
                    parts.append(Text())
                label = _("Thought for {seconds:.1f}s").format(seconds=seg.elapsed_seconds)
                parts.append(Text(f"▌ {label}", style="dim"))
                has_content = True
            elif seg.kind == "tool" and seg.tool:
                if has_content:
                    parts.append(Text())  # blank line between segments
                parts.append(self._render_tool_header(seg.tool))
                if seg.tool.progress_renderable is not None and not seg.tool.done:
                    parts.append(seg.tool.progress_renderable)
                result_line = self._render_tool_result(seg.tool)
                if result_line:
                    parts.append(result_line)
                has_content = True

        if thinking_buffer:
            if has_content:
                parts.append(Text())
            parts.append(self._render_thinking_quote(thinking_buffer))
            has_content = True

        # Streaming text that hasn't been finalized yet
        if text_buffer:
            if has_content:
                parts.append(Text())  # blank line before ✦ block
            parts.extend(self._render_text_block(text_buffer, continuation=self._text_flushed))

        # Current spinner (thinking)
        if spinner:
            parts.append(spinner.render())

        # Verbose-mode hint — only when some tool actually has more to show.
        if not self._verbose and self._any_segment_has_verbose(segments):
            parts.append(Text("  " + _("(ctrl+o to expand)"), style="dim"))

        # Task-level spinner with elapsed time (always shown while processing)
        if task_spinner:
            parts.append(Text())  # blank line separator
            parts.append(task_spinner.render())

        group = Group(*parts) if parts else Group(Text(""))

        # Wrap in _CropTop so Live never pushes content into the terminal
        # scrollback buffer.  The full markdown is rendered first (preserving
        # code-block styling), then only the bottom N lines are kept.
        terminal_height = self.console.height or 24
        max_height = max(terminal_height - 8, 5)  # room for footer
        return _CropTop(group, max_height)

    def _render_thinking_quote(self, text: str) -> _CropTop:
        """Render the live thinking buffer as a dim quote block, height-cropped."""
        lines = text.splitlines() or [""]
        rendered = Group(*[Text(f"▌ {line}", style="dim") for line in lines])
        terminal_height = self.console.height or 24
        max_height = min(6, max(terminal_height // 4, 3))
        return _CropTop(rendered, max_height)

    # ── Dynamic streaming output ────────────────────────────────────

    async def run_streaming_output(
        self,
        events: AsyncGenerator[StreamEvent, None],
        permission_handler: Callable[[PermissionRequestEvent], Awaitable[bool]],
    ) -> float:
        """Consume the event stream and render everything."""
        self._last_streaming_errors = []
        self.console.print()  # blank line between user input and agent response
        live: Live | None = None
        spinner: ShimmerSpinner | None = None
        task_spinner: ShimmerSpinner | None = None
        refresh_task: asyncio.Task | None = None
        key_task: asyncio.Task | None = None
        text_buffer = ""
        thinking_buffer: str = ""
        thinking_start_time: float | None = None
        segments: list[_Segment] = []
        turn_start_time: float = time.monotonic()

        # Save terminal settings before any background task modifies them
        # so we can unconditionally restore on exit.
        _saved_termios = None
        try:
            _saved_termios = termios.tcgetattr(sys.stdin.fileno())
        except (termios.error, OSError, ValueError):
            pass

        def _finalize_thinking() -> None:
            nonlocal thinking_buffer, thinking_start_time
            if thinking_start_time is not None and thinking_buffer.strip():
                elapsed = time.monotonic() - thinking_start_time
                segments.append(_Segment(kind="thinking_summary", elapsed_seconds=elapsed))
            thinking_buffer = ""
            thinking_start_time = None

        def _update_live():
            if live:
                content = self._render_segments(
                    segments, spinner, text_buffer, task_spinner, thinking_buffer=thinking_buffer
                )
                live.update(self._with_footer(content))

        def _ensure_live():
            nonlocal live
            if live is None:
                live = Live(
                    self._with_footer(Group(Text(""))),
                    console=self.console,
                    refresh_per_second=20,
                    transient=True,
                    vertical_overflow="visible",
                )
                live.start()

        async def _rebuild_after_transcript():
            """Rebuild Live + refresh task immediately after the transcript
            view closes, so the user sees the current segments right away
            instead of waiting for the next stream event to unblock the
            main loop.
            """
            nonlocal refresh_task, live
            await self._stop_refresh(refresh_task)
            refresh_task = None
            if live is not None:
                self._quiet_stop_live(live)
                live = None
            _ensure_live()
            _update_live()
            if live is not None:
                refresh_task = asyncio.create_task(
                    self._refresh_loop(
                        live,
                        segments,
                        spinner,
                        lambda: text_buffer,
                        lambda: task_spinner,
                        lambda: thinking_buffer,
                    )
                )
            # Already handled; don't let the main-loop reset block redo it.
            self._stream_invalidated = False

        # Map tool_use_id → _ToolCallRecord for partial-input accumulation
        tool_records: dict[str, _ToolCallRecord] = {}

        # Turn-level token usage accumulator (summed across MessageEndEvents)
        turn_usage = Usage()

        try:
            async for event in events:
                # After a mid-stream transcript view, tear down the stale
                # Live and its background tasks before handling the next
                # event — the alt-screen sequence left them pointing at a
                # now-invalid render context.
                if self._stream_invalidated:
                    self._stream_invalidated = False
                    await self._stop_refresh(refresh_task)
                    refresh_task = None
                    await self._stop_refresh(key_task)
                    key_task = None
                    if live is not None:
                        try:
                            self._quiet_stop_live(live)
                        except Exception:
                            pass
                        live = None
                    # Proactively rebuild Live + both helpers so the task
                    # spinner keeps animating and Ctrl+O stays responsive
                    # from the very next frame. Without this the UI would
                    # appear frozen until an event handler happened to
                    # reach a branch that recreates them (MessageStart,
                    # ToolUseStart, …).
                    _ensure_live()
                    # Paint the current segments immediately — otherwise the
                    # new Live starts empty and stays empty for up to 50ms
                    # (one refresh tick), which is very visible when a sub-
                    # agent with many children is mid-flight.
                    _update_live()
                    if live is not None:
                        refresh_task = asyncio.create_task(
                            self._refresh_loop(
                                live,
                                segments,
                                spinner,
                                lambda: text_buffer,
                                lambda: task_spinner,
                                lambda: thinking_buffer,
                            )
                        )

                # Ensure Ctrl+O is always being listened for during streaming.
                # Several event handlers (sub-agent activity, stack progress,
                # compaction, …) stop/skip recreating the key task, which
                # would swallow Ctrl+O until the next MessageStart or
                # ToolUseStart rebuilt it.
                if live is not None and (key_task is None or key_task.done()):
                    key_task = asyncio.create_task(
                        self._key_listener(
                            live,
                            segments,
                            spinner,
                            lambda: text_buffer,
                            _rebuild_after_transcript,
                        )
                    )

                # ── Message start ───────────────────────────────
                if isinstance(event, MessageStartEvent):
                    self._text_flushed = False  # new message = new text block
                    if task_spinner is None:
                        task_spinner = ShimmerSpinner()
                    _ensure_live()
                    # Always ensure refresh loop is running for spinner animation
                    if refresh_task is None or refresh_task.done():
                        refresh_task = asyncio.create_task(
                            self._refresh_loop(
                                live,
                                segments,
                                spinner,
                                lambda: text_buffer,
                                lambda: task_spinner,
                                lambda: thinking_buffer,
                            )
                        )
                    if key_task is None or key_task.done():
                        key_task = asyncio.create_task(
                            self._key_listener(
                                live,
                                segments,
                                spinner,
                                lambda: text_buffer,
                                _rebuild_after_transcript,
                            )
                        )

                # ── Thinking delta ─────────────────────────────
                elif isinstance(event, ThinkingDeltaEvent):
                    if thinking_start_time is None:
                        thinking_start_time = time.monotonic()
                    thinking_buffer += event.text
                    spinner = None
                    _ensure_live()
                    _update_live()

                # ── Text delta ──────────────────────────────────
                elif isinstance(event, TextDeltaEvent):
                    _finalize_thinking()
                    spinner = None  # stop spinner when text starts
                    text_buffer += event.text
                    _ensure_live()

                    # Flush completed text to scrollback when it exceeds
                    # half the terminal height, preventing Live overflow
                    # that causes duplicate content on scroll-up.
                    terminal_height = self.console.height or 24
                    if text_buffer.count("\n") + 1 > terminal_height // 2:
                        split_pos, in_fence = self._find_safe_split_pos(text_buffer)
                        flush_text: str | None = None
                        if split_pos > 0:
                            # Split at safe paragraph break outside code blocks
                            flush_text = text_buffer[:split_pos]
                            text_buffer = text_buffer[split_pos + 2 :]
                        elif not in_fence:
                            # No paragraph break but not inside a fence —
                            # flush entire buffer to prevent overflow
                            flush_text = text_buffer
                            text_buffer = ""
                        # else: inside a code fence — cannot split safely

                        if flush_text is not None:
                            await self._stop_refresh(refresh_task)
                            refresh_task = None
                            await self._stop_refresh(key_task)
                            key_task = None
                            if live:
                                self._quiet_stop_live(live)
                                live = None
                            # Archive + print the flushed text so it also
                            # appears in the transcript view. Printing it
                            # directly via console.print used to skip the
                            # message history, which is why the detail page
                            # showed only a bare ✦ bullet for any turn
                            # whose response was flushed mid-stream.
                            self._print_segments_to_scrollback([], flush_text)
                            _ensure_live()
                            refresh_task = asyncio.create_task(
                                self._refresh_loop(
                                    live,
                                    segments,
                                    spinner,
                                    lambda: text_buffer,
                                    lambda: task_spinner,
                                    lambda: thinking_buffer,
                                )
                            )
                            if key_task is None or key_task.done():
                                key_task = asyncio.create_task(
                                    self._key_listener(
                                        live,
                                        segments,
                                        spinner,
                                        lambda: text_buffer,
                                        _rebuild_after_transcript,
                                    )
                                )

                    _update_live()

                # ── Tool use start ──────────────────────────────
                elif isinstance(event, ToolUseStartEvent):
                    _finalize_thinking()
                    # Finalize any pending text into a segment
                    if text_buffer:
                        segments.append(_Segment(kind="text", text=text_buffer))
                        text_buffer = ""

                    # Flush completed segments (text + done tools) to scrollback
                    # to prevent Live content from growing beyond terminal height
                    completed = [
                        s for s in segments if s.kind == "text" or (s.kind == "tool" and s.tool and s.tool.done)
                    ]
                    if completed:
                        remaining = [s for s in segments if s not in completed]
                        await self._stop_refresh(refresh_task)
                        refresh_task = None
                        await self._stop_refresh(key_task)
                        key_task = None
                        if live:
                            self._quiet_stop_live(live)
                            live = None
                        self._print_segments_to_scrollback(completed, "")
                        segments.clear()
                        segments.extend(remaining)
                    self._text_flushed = False  # text block done before tool

                    rec = _ToolCallRecord(
                        tool_name=event.name,
                        tool_input={},
                        start_time=time.monotonic(),
                    )
                    tool_records[event.tool_use_id] = rec
                    segments.append(_Segment(kind="tool", tool=rec))

                    # No separate spinner — the ● dot animates itself
                    spinner = None
                    _ensure_live()
                    # Restart refresh with updated refs
                    await self._stop_refresh(refresh_task)
                    refresh_task = asyncio.create_task(
                        self._refresh_loop(
                            live, segments, spinner, lambda: text_buffer, lambda: task_spinner, lambda: thinking_buffer
                        )
                    )
                    if key_task is None or key_task.done():
                        key_task = asyncio.create_task(
                            self._key_listener(
                                live,
                                segments,
                                spinner,
                                lambda: text_buffer,
                                _rebuild_after_transcript,
                            )
                        )

                # ── Tool input delta ────────────────────────────
                elif isinstance(event, ToolInputDeltaEvent):
                    rec = tool_records.get(event.tool_use_id)
                    if rec:
                        rec.partial_input += event.partial_json
                    _update_live()

                # ── Tool use end ────────────────────────────────
                elif isinstance(event, ToolUseEndEvent):
                    rec = tool_records.get(event.tool_use_id)
                    if rec:
                        rec.tool_input = event.input
                    _update_live()

                # ── Tool result ─────────────────────────────────
                elif isinstance(event, ToolResultEvent):
                    rec = tool_records.get(event.tool_use_id)
                    if rec is None:
                        # Fallback: match by tool_name for any unfinished tool
                        for r in tool_records.values():
                            if r.tool_name == event.tool_name and not r.done:
                                rec = r
                                break
                    if rec:
                        rec.result = event.result
                        rec.is_error = event.is_error
                        rec.done = True
                    spinner = None
                    _ensure_live()
                    _update_live()

                    # If all tools are done, finalize deferred turn
                    all_tools_done = all(s.tool.done for s in segments if s.kind == "tool" and s.tool)
                    if all_tools_done and segments:
                        await self._stop_refresh(refresh_task)
                        refresh_task = None
                        await self._stop_refresh(key_task)
                        key_task = None
                        if live:
                            self._quiet_stop_live(live)
                            live = None
                        self._print_segments_to_scrollback(segments, "")
                        self._print_turn_usage(turn_usage)
                        turn_usage = Usage()
                        segments.clear()
                        tool_records.clear()

                # ── Sub-agent tool activity ──────────────────────
                elif isinstance(event, SubAgentToolEvent):
                    rec = tool_records.get(event.parent_tool_use_id)
                    if rec:
                        if rec.children is None:
                            rec.children = []
                        if event.is_done:
                            # Mark existing child as done
                            for child in rec.children:
                                if child.tool_name == event.child_tool_name and not child.is_done:
                                    child.is_done = True
                                    child.is_error = event.is_error
                                    break
                        else:
                            # New child tool started
                            rec.children.append(
                                _SubAgentChild(
                                    tool_name=event.child_tool_name,
                                    tool_input=event.child_tool_input,
                                )
                            )
                    _ensure_live()
                    # Ensure refresh loop is running to animate child updates
                    if refresh_task is None or refresh_task.done():
                        refresh_task = asyncio.create_task(
                            self._refresh_loop(
                                live,
                                segments,
                                spinner,
                                lambda: text_buffer,
                                lambda: task_spinner,
                                lambda: thinking_buffer,
                            )
                        )
                    _update_live()

                # ── Stack progress ─────────────────────────────
                elif isinstance(event, StackProgressEvent):
                    for rec in tool_records.values():
                        if rec.tool_name == "ros_stack" and not rec.done:
                            rec.progress_renderable = self._render_stack_progress(event)
                            break
                    _ensure_live()
                    _update_live()

                # ── Stack instances progress ──────────────────
                elif isinstance(event, StackInstancesProgressEvent):
                    for rec in tool_records.values():
                        if rec.tool_name == "ros_stack_instances" and not rec.done:
                            rec.progress_renderable = self._render_instances_progress(event)
                            break
                    _ensure_live()
                    _update_live()

                # ── Permission request ──────────────────────────
                elif isinstance(event, PermissionRequestEvent):
                    # Must stop Live to interact with user
                    await self._stop_refresh(refresh_task)
                    refresh_task = None
                    await self._stop_refresh(key_task)
                    key_task = None
                    if live:
                        self._quiet_stop_live(live)
                        live = None
                    spinner = None
                    # Print current state to scrollback
                    self._print_segments_to_scrollback(segments, text_buffer)
                    segments.clear()
                    text_buffer = ""
                    # Handle permission
                    allowed = await permission_handler(event)
                    if event.response_future is not None:
                        if allowed:
                            log_event(
                                Events.TOOL_USE_GRANTED_IN_PROMPT,
                                {
                                    "tool_name": event.tool_name,
                                    "scope": "once",
                                },
                            )
                        else:
                            log_event(
                                Events.TOOL_USE_REJECTED_IN_PROMPT,
                                {
                                    "tool_name": event.tool_name,
                                },
                            )
                            add_metric(
                                Metrics.TOOL_USE_COUNT,
                                1,
                                {
                                    "tool_name": event.tool_name,
                                    "outcome": "denied",
                                },
                            )
                        event.response_future.set_result(allowed)

                # ── Compaction ────────────────────────────────
                elif isinstance(event, CompactionEvent):
                    compact_msg = _("Context auto-compacted: {original} → {compacted} tokens").format(
                        original=event.original_tokens,
                        compacted=event.compacted_tokens,
                    )
                    segments.append(_Segment(kind="text", text=f"*{compact_msg}*"))
                    _update_live()

                # ── Tombstone ──────────────────────────────────
                elif isinstance(event, TombstoneEvent):
                    # Discard all rendered content for the orphaned message
                    segments.clear()
                    text_buffer = ""
                    tool_records.clear()
                    spinner = None
                    if live:
                        self._quiet_stop_live(live)
                        live = None
                    await self._stop_refresh(refresh_task)
                    refresh_task = None
                    await self._stop_refresh(key_task)
                    key_task = None

                # ── Task notification ──────────────────────────
                elif isinstance(event, TaskNotificationEvent):
                    style_map = {
                        "completed": "green",
                        "failed": "red",
                        "stopped": "yellow",
                    }
                    style = style_map.get(event.status, "dim")
                    notice = Text()
                    notice.append(f"[{event.status}] ", style=f"bold {style}")
                    notice.append(event.description)
                    if event.result:
                        notice.append(f": {event.result}")
                    if event.error:
                        notice.append(f" (error: {event.error})", style="red")
                    self.console.print(notice)

                # ── Error ──────────────────────────────────────
                elif isinstance(event, ErrorEvent):
                    self._last_streaming_errors.append(event.error)
                    self.console.print(Text(event.error, style="bold red"))

                # ── Message end ─────────────────────────────────
                elif isinstance(event, MessageEndEvent):
                    _finalize_thinking()
                    # Accumulate turn-level token usage
                    turn_usage.input_tokens += event.usage.input_tokens
                    turn_usage.output_tokens += event.usage.output_tokens
                    turn_usage.cache_creation_input_tokens += event.usage.cache_creation_input_tokens
                    turn_usage.cache_read_input_tokens += event.usage.cache_read_input_tokens
                    # Finalize remaining text
                    if text_buffer:
                        segments.append(_Segment(kind="text", text=text_buffer))
                        text_buffer = ""
                    spinner = None

                    # Check if there are unfinished tool calls (e.g. agent tools
                    # that will produce SubAgentToolEvents during execution)
                    has_pending_tools = any(s.kind == "tool" and s.tool and not s.tool.done for s in segments)

                    if has_pending_tools:
                        # Keep Live, segments, and tool_records alive —
                        # SubAgentToolEvent and ToolResultEvent will arrive next.
                        # The turn is finalized in ToolResultEvent when all tools done.
                        pass
                    else:
                        # No pending tools — finalize turn normally
                        await self._stop_refresh(refresh_task)
                        refresh_task = None
                        await self._stop_refresh(key_task)
                        key_task = None
                        if live:
                            self._quiet_stop_live(live)
                            live = None
                        self._print_segments_to_scrollback(segments, "")
                        self._print_turn_usage(turn_usage)
                        turn_usage = Usage()
                        segments.clear()
                        tool_records.clear()
                    # DON'T stop task_spinner — it persists across turns
                    # DON'T break — there may be more events after tool execution

        except (asyncio.CancelledError, KeyboardInterrupt):
            self.console.print(Text(_("Operation cancelled."), style="yellow"))
        except Exception as e:
            error_msg = str(e)
            if "No key found" in error_msg or "api_key" in error_msg.lower() or "API key" in error_msg.lower():
                msg = (
                    _("No API key configured.") + "\n" + _("Please run /auth to set up your LLM provider and API key.")
                )
                self._last_streaming_errors.append(msg)
                self.print_system_message(msg, style="yellow")
            else:
                msg = _("Error: {error}").format(error=error_msg)
                self._last_streaming_errors.append(msg)
                self.print_system_message(msg, style="red")
        finally:
            task_spinner = None
            # Restore terminal settings first, before awaiting tasks, so that
            # even if a second Ctrl+C aborts the cleanup the terminal is sane.
            if _saved_termios is not None:
                try:
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, _saved_termios)
                except (termios.error, OSError, ValueError):
                    pass
            # Stop background tasks.  Wrap each await separately so that a
            # pending CancelledError (from task cancellation edge cases)
            # does not skip the remaining cleanup.
            for _bg_task in (refresh_task, key_task):
                try:
                    await self._stop_refresh(_bg_task)
                except asyncio.CancelledError:
                    if _bg_task and not _bg_task.done():
                        _bg_task.cancel()
            if live:
                self._quiet_stop_live(live)
                live = None
            # Print any remaining segments
            if segments:
                self._print_segments_to_scrollback(segments, text_buffer)
                segments.clear()
            # Print completion message with random verb and duration
            from iac_code.ui.spinner import _format_elapsed, random_completion_verb

            elapsed = time.monotonic() - turn_start_time
            if elapsed >= 1.0:
                self.console.print()  # blank line before completion
                verb = random_completion_verb()
                self.console.print(Text(f"✻ {verb} {_format_elapsed(elapsed)}", style="dim italic"))

        return elapsed

    # ── Permission prompting ────────────────────────────────────────

    async def prompt_permission(self, event: PermissionRequestEvent) -> bool:
        """Inline permission prompt — arrow-key selector aligned with ACP."""
        from iac_code.state.app_state import lookup_permission, record_permission

        tool_name = event.tool_name
        cache = self._app_state_store.get_state().always_allow_rules if self._app_state_store is not None else None

        # Short-circuit on cached sticky decisions — no prompt, no input read.
        cached = lookup_permission(cache, tool_name)
        if cached == "always_allow":
            return True
        if cached == "always_deny":
            return False

        tool = self._tool_registry.get(tool_name)
        tool_display = tool.user_facing_name(event.tool_input) if tool else tool_name
        detail = None
        if tool:
            detail = tool.render_tool_use_message(event.tool_input)

        # Tool-use header.
        line = Text()
        line.append("● ", style="bold")
        line.append(tool_display, style="bold")
        if detail:
            line.append(f" ({detail})")
        self.console.print(line)

        # Arrow-key selector with smart rule suggestions from permission engine.
        self.console.print(Text(_("Allow this action?"), style="bold"))

        options: list[OptionType] = [
            TextOption(label=_("Yes, allow once"), value="allow_once"),
        ]

        suggestions = (
            event.permission_result.suggestions
            if event.permission_result is not None
            and hasattr(event.permission_result, "suggestions")
            and event.permission_result.suggestions
            else []
        )
        if suggestions:
            rules_display = ", ".join(s.rule_content for s in suggestions)
            options.append(
                TextOption(
                    label=_('Yes, always allow "{rule}" (this session)').format(rule=rules_display),
                    value="always_allow_rule",
                )
            )
        elif tool and tool.supports_blanket_allow:
            options.append(TextOption(label=_("Yes, allow always for this tool"), value="always_allow"))

        options.append(
            TextOption(label=_("No, reject once"), value="reject_once", description="({})".format(_("default")))
        )

        if suggestions:
            rules_display = ", ".join(s.rule_content for s in suggestions)
            options.append(
                TextOption(
                    label=_('No, always deny "{rule}" (this session)').format(rule=rules_display),
                    value="always_deny_rule",
                )
            )

        options.append(TextOption(label=_("No, always reject this tool"), value="always_deny"))

        select = Select(
            options=options,
            default_value="reject_once",
            layout=SelectLayout.EXPANDED,
            visible_count=len(options),
        )

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, select.run)

        if result is None:
            return False

        if result == "allow_once":
            return True
        if result == "always_allow":
            record_permission(cache, tool_name, "always_allow")
            return True
        if result == "always_allow_rule":
            if suggestions and self._app_state_store is not None:
                perm_ctx = self._app_state_store.get_state().permission_context
                if perm_ctx is not None:
                    import dataclasses

                    from iac_code.services.permissions.storage import apply_session_rule

                    for sug in suggestions:
                        perm_ctx = apply_session_rule(perm_ctx, "allow", sug)
                    self._app_state_store.set_state(lambda s: dataclasses.replace(s, permission_context=perm_ctx))
            return True
        if result == "always_deny_rule":
            if suggestions and self._app_state_store is not None:
                perm_ctx = self._app_state_store.get_state().permission_context
                if perm_ctx is not None:
                    import dataclasses

                    from iac_code.services.permissions.storage import apply_session_rule

                    for sug in suggestions:
                        perm_ctx = apply_session_rule(perm_ctx, "deny", sug)
                    self._app_state_store.set_state(lambda s: dataclasses.replace(s, permission_context=perm_ctx))
            return False
        if result == "always_deny":
            record_permission(cache, tool_name, "always_deny")
            return False
        return False

    # ── Scrollback finalization ──────────────────────────────────────

    def _print_segments_to_scrollback(self, segments: list[_Segment], trailing_text: str) -> None:
        """Print finalized segments to terminal scrollback."""
        archived = copy.deepcopy(segments)
        if trailing_text:
            archived.append(_Segment(kind="text", text=trailing_text))
        if not archived:
            return

        if self._message_history and self._message_history[-1].role == "assistant":
            self._message_history[-1].segments.extend(archived)
        else:
            self._message_history.append(RenderedTurn(role="assistant", segments=archived, timestamp=time.monotonic()))

        has_content = False
        for seg in segments:
            if seg.kind == "text" and seg.text:
                if has_content:
                    self.console.print()
                for part in self._render_text_block(seg.text, continuation=self._text_flushed):
                    self.console.print(part)
                self._text_flushed = True
                has_content = True
            elif seg.kind == "thinking_summary":
                if has_content:
                    self.console.print()
                label = _("Thought for {seconds:.1f}s").format(seconds=seg.elapsed_seconds)
                self.console.print(Text(f"▌ {label}", style="dim"))
                has_content = True
                self._text_flushed = False
            elif seg.kind == "tool" and seg.tool:
                if has_content:
                    self.console.print()
                self.console.print(self._render_tool_header(seg.tool))
                result_line = self._render_tool_result(seg.tool)
                if result_line:
                    self.console.print(result_line)
                has_content = True
                self._text_flushed = False
        if trailing_text:
            if has_content:
                self.console.print()
            for part in self._render_text_block(trailing_text, continuation=self._text_flushed):
                self.console.print(part)
            self._text_flushed = True

        if not self._verbose and self._any_segment_has_verbose(segments):
            self.console.print(Text("  " + _("(ctrl+o to expand)"), style="dim"))

    def _print_turn_usage(self, usage: Usage) -> None:
        """Print a dim one-line token/cache summary after a completed turn.

        Only shown when debug logging is active (``--debug`` flag or ``/debug`` command).
        """
        from iac_code.utils.log import is_debug_enabled

        if not is_debug_enabled():
            return
        if not usage.input_tokens and not usage.output_tokens:
            return
        parts: list[str] = []
        parts.append(self._format_token_count(usage.input_tokens, "input"))
        parts.append(self._format_token_count(usage.output_tokens, "output"))
        if usage.cache_read_input_tokens:
            parts.append(self._format_token_count(usage.cache_read_input_tokens, "cache_read"))
        if usage.cache_creation_input_tokens:
            parts.append(self._format_token_count(usage.cache_creation_input_tokens, "cache_create"))
        self.console.print(Text("  " + " · ".join(parts), style="dim"))

    @staticmethod
    def _format_token_count(count: int, label: str) -> str:
        if count >= 1_000_000:
            return f"{count / 1_000_000:.1f}M {label}"
        if count >= 1_000:
            return f"{count / 1_000:.1f}k {label}"
        return f"{count} {label}"

    def replay_history(self, messages: list) -> None:
        """Replay saved Message objects to scrollback with 1:1 visual fidelity."""
        from iac_code.agent.message import TextBlock, ToolResultBlock, ToolUseBlock

        # Build a lookup of tool_use_id → ToolResultBlock from all user messages
        tool_results: dict[str, ToolResultBlock] = {}
        for msg in messages:
            if msg.role == "user" and isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        tool_results[block.tool_use_id] = block

        first_turn = True
        for msg in messages:
            if msg.role == "user":
                is_tool_result_only = isinstance(msg.content, list) and all(
                    isinstance(b, ToolResultBlock) for b in msg.content
                )
                if is_tool_result_only:
                    continue
                if not first_turn:
                    self.console.print()
                first_turn = False
                if isinstance(msg.content, str):
                    self.print_user_message(msg.content)
                else:
                    text = msg.get_text()
                    if text:
                        self.print_user_message(text)
                self.console.print()  # blank line between user input and agent response
            elif msg.role == "assistant":
                segments: list[_Segment] = []
                if isinstance(msg.content, str):
                    segments.append(_Segment(kind="text", text=msg.content))
                elif isinstance(msg.content, list):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            segments.append(_Segment(kind="text", text=block.text))
                        elif isinstance(block, ToolUseBlock):
                            result = tool_results.get(block.id)
                            rec = _ToolCallRecord(
                                tool_name=block.name,
                                tool_input=block.input,
                                result=result.content if result else None,
                                is_error=result.is_error if result else False,
                                done=True,
                            )
                            segments.append(_Segment(kind="tool", tool=rec))
                if segments:
                    self._text_flushed = False
                    self._print_segments_to_scrollback(segments, "")
                if msg.elapsed_seconds >= 1.0:
                    from iac_code.ui.spinner import _format_elapsed, random_completion_verb

                    self.console.print()
                    self.console.print(
                        Text(f"✻ {random_completion_verb()} {_format_elapsed(msg.elapsed_seconds)}", style="dim italic")
                    )

    # ── Background tasks ────────────────────────────────────────────

    async def _refresh_loop(
        self,
        live: Live,
        segments: list[_Segment],
        spinner: ShimmerSpinner | None,
        get_text: Callable[[], str],
        get_task_spinner: Callable[[], ShimmerSpinner | None] | None = None,
        get_thinking: Callable[[], str] | None = None,
    ) -> None:
        """Background task: update Live with spinner frames at ~20fps."""
        try:
            while True:
                await asyncio.sleep(0.05)
                ts = get_task_spinner() if get_task_spinner else None
                tb = get_thinking() if get_thinking else ""
                content = self._render_segments(segments, spinner, get_text(), ts, thinking_buffer=tb)
                live.update(self._with_footer(content))
        except asyncio.CancelledError:
            pass

    async def _key_listener(
        self,
        live: Live,
        segments: list[_Segment],
        spinner: ShimmerSpinner | None,
        get_text: Callable[[], str],
        on_transcript_done: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Background task: listen for Ctrl+O to toggle verbose mode.

        Uses loop.add_reader for proper asyncio integration and clears
        IEXTEN to prevent macOS from intercepting Ctrl+O as VDISCARD.
        """
        fd = sys.stdin.fileno()
        try:
            old_settings = termios.tcgetattr(fd)
        except termios.error:
            return  # not a TTY

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[int] = asyncio.Queue()

        def _on_readable() -> None:
            try:
                data = os.read(fd, 64)
                for b in data:
                    queue.put_nowait(b)
            except OSError:
                pass

        try:
            loop.add_reader(fd, _on_readable)
        except OSError:
            # macOS kqueue cannot register certain fds (e.g. /dev/tty
            # reopened after piped stdin).  Silently disable key listener.
            return

        try:
            # cbreak mode + clear IEXTEN so Ctrl+O (VDISCARD) reaches us
            mode = termios.tcgetattr(fd)
            mode[3] = mode[3] & ~(termios.ECHO | termios.ICANON | termios.IEXTEN)
            mode[6][termios.VMIN] = 1
            mode[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, mode)

            show_transcript_after = False
            while True:
                ch = await queue.get()
                if ch == 0x0F:  # Ctrl+O — break out and open transcript view
                    show_transcript_after = True
                    break
                if ch == 0x1B:  # Escape — interrupt
                    break
        except asyncio.CancelledError:
            return
        finally:
            try:
                loop.remove_reader(fd)
            except Exception:
                pass
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except termios.error:
                pass

        if show_transcript_after:
            # Stop the Live region so the alt-screen view starts from a
            # clean main screen. Use the quiet variant so we don't leak a
            # stray line of Live content into scrollback on every cycle.
            if live is not None:
                self._quiet_stop_live(live)
            # Pass the live segments so the transcript shows the currently
            # running tool call tree (e.g. a sub-agent mid-flight) that
            # hasn't been flushed into _message_history yet.
            self.show_transcript(current_segments=list(segments))
            # Rebuild Live from within this task so the user sees the
            # streaming state the instant the transcript closes — waiting
            # for the outer loop's next event would leave the screen blank
            # for however long the LLM stays silent after we resume.
            if on_transcript_done is not None:
                try:
                    await on_transcript_done()
                except Exception:
                    # Fall back to the slower main-loop reset path if the
                    # immediate rebuild failed for any reason.
                    self._stream_invalidated = True
            else:
                self._stream_invalidated = True

    async def _stop_refresh(self, task: asyncio.Task | None) -> None:
        """Cancel a background task."""
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
