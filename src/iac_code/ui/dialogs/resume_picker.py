"""ResumePicker dialog — pick a session to resume.

Header with "Resume Session (X of Y)", a search box, three-line item
rows (project group header, title with focus marker, "<time> · <branch>
· <size>" subtitle), and a footer of available shortcuts.

Pressing Space (with the search field empty) enters preview mode: the
picker switches into the alternate screen buffer, replays the focused
session at full terminal height via :meth:`Renderer.replay_history`,
and lets the user navigate with ``Up``/``Down``/``PageUp``/``PageDown``/
``Home``/``End`` *and* the mouse wheel (which the alt-screen normally
swallows — we explicitly enable SGR mouse tracking for the duration of
the preview so wheel ticks are forwarded to us as scroll commands).
``Enter`` resumes the session, ``Esc`` returns to the picker list, and
because the alt-screen is restored on exit nothing the preview drew
ever leaks into the user's main-buffer scrollback.
"""

from __future__ import annotations

import io
import time
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.console import Console, Group, RenderableType
from rich.text import Text

from iac_code.agent.message import Message, ToolResultBlock
from iac_code.i18n import _
from iac_code.services.session_index import SessionEntry, SessionIndex
from iac_code.ui.components.fuzzy_picker import fuzzy_match
from iac_code.ui.components.search_box import SearchBox
from iac_code.ui.core.in_place_render import InPlaceRenderer
from iac_code.ui.core.key_event import KeyEvent
from iac_code.ui.core.raw_input import RawInputCapture, query_cursor_row
from iac_code.ui.core.screen import ScreenManager
from iac_code.utils.project_paths import get_git_branch

if TYPE_CHECKING:
    from iac_code.ui.renderer import Renderer


# Wheel ticks are tiny — scroll a few lines per tick so the preview
# moves at a comfortable pace under fast spinning.
_WHEEL_LINES = 3


class ResumePicker:
    """Interactive picker for session resume.

    Construct with a populated :class:`SessionIndex`, the user's current
    cwd (used for the default "current directory" filter), and the
    current session id (excluded from the list). Call :meth:`run` to
    block until the user selects an entry or cancels.
    """

    VISIBLE_ITEM_LINES = 3  # project-header / title / subtitle

    def __init__(
        self,
        index: SessionIndex,
        current_cwd: str,
        current_session_id: str | None,
        keybinding_manager: object | None = None,
        renderer: "Renderer | None" = None,
        entries: list[SessionEntry] | None = None,
    ) -> None:
        self._index = index
        self._current_cwd = current_cwd
        self._current_session_id = current_session_id
        self._km = keybinding_manager
        self._entries_override = entries
        # Live REPL renderer — reused inside the preview so the dump
        # uses the same tool-name translation, argument formatting, and
        # result-summary helpers as the live UI.
        self._renderer = renderer

        self._show_all_projects = False
        self._only_current_branch = False
        self._show_preview = False
        self._current_branch: str | None = get_git_branch(current_cwd)

        self._all_entries: list[SessionEntry] = []
        self._filtered: list[SessionEntry] = []
        self._focused_index = 0
        self._visible_from = 0
        self._done = False
        self._result: SessionEntry | None = None
        # Set in run(); used by _visible_count to size the list to the
        # current terminal height.
        self._console: Console | None = None
        # 1-indexed cursor row when the picker started — used to compute
        # how many lines below the cursor are still in the viewport. None
        # when the terminal didn't answer DSR-6; falls back to a smaller
        # default.
        self._entry_row: int | None = None
        # Loaded session messages keyed by session_id — populated lazily
        # so a re-preview doesn't re-read the JSONL file.
        self._messages_cache: dict[str, list[Message]] = {}
        # Cache of pre-rendered preview body lines (one entry per
        # ``(session_id, width)``) — replay_history uses
        # ``random_completion_verb()`` which is non-deterministic, so
        # caching keeps the body stable across redraws caused by
        # scrolling.
        self._rendered_body_cache: dict[tuple[str, int], list[str]] = {}
        # Number of body lines hidden below the visible window. ``0``
        # pins the newest content to the bottom.  Up arrow / wheel up
        # *increases* the offset (reveals older content above).
        self._preview_scroll_offset = 0
        # Body height observed during the last redraw — needed by
        # PageUp/PageDown to scroll a screenful at a time.
        self._preview_body_height_last = 0

        self._search_box = SearchBox(
            placeholder=_("Search..."),
            on_change=self._on_query_change,
        )

        self._reload_entries()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> SessionEntry | None:
        import sys

        self._console = Console()
        renderer = InPlaceRenderer(self._console)
        screen = ScreenManager(self._console)
        try:
            with RawInputCapture() as cap:
                # Query *inside* raw mode — under cooked mode the
                # response wouldn't be readable until a newline.
                self._entry_row = query_cursor_row(sys.stdin.fileno())
                while not self._done:
                    if self._show_preview and self._filtered:
                        renderer.clear()
                        self._run_preview_loop(cap, screen)
                        continue
                    renderer.render(
                        self.render(),
                        cursor_to=self._search_cursor_pos(),
                    )
                    key_event = cap.read_key(timeout=0.1)
                    if key_event is not None:
                        self.handle_key(key_event)
        except OSError:
            return None
        finally:
            renderer.clear()
            self._console = None
            self._entry_row = None
        return self._result

    # Layout: row 0 = header, row 1 = blank, row 2 = search box.
    _SEARCH_BOX_ROW = 2

    def _search_cursor_pos(self) -> tuple[int, int]:
        sb = self._search_box
        if not sb.value:
            col = 2
        else:
            col = 2 + cell_len(sb.value[: sb.cursor])
        return (self._SEARCH_BOX_ROW, col)

    # ------------------------------------------------------------------
    # Key handling
    # ------------------------------------------------------------------

    def handle_key(self, key_event: KeyEvent) -> bool:
        key = key_event.key
        ctrl = key_event.ctrl

        if ctrl and key == "c":
            self._done = True
            return True

        if self._show_preview:
            return self._handle_key_preview(key_event)
        return self._handle_key_list(key_event)

    def _handle_key_preview(self, key_event: KeyEvent) -> bool:
        key = key_event.key
        ctrl = key_event.ctrl

        if key == "escape":
            self._show_preview = False
            return True

        if key == "enter":
            if self._filtered:
                self._result = self._filtered[self._focused_index]
                self._done = True
            return True

        # Up/Down/Page keys scroll the preview body.  ``Up`` reveals
        # older content (offset grows), ``Down`` reveals newer.
        if key == "up" or (ctrl and key == "p"):
            self._scroll_preview(1)
            return True
        if key == "down" or (ctrl and key == "n"):
            self._scroll_preview(-1)
            return True
        if key == "wheel_up":
            self._scroll_preview(_WHEEL_LINES)
            return True
        if key == "wheel_down":
            self._scroll_preview(-_WHEEL_LINES)
            return True
        if key == "pageup":
            self._scroll_preview(max(1, self._preview_body_height_last - 1))
            return True
        if key == "pagedown":
            self._scroll_preview(-max(1, self._preview_body_height_last - 1))
            return True
        if key == "home":
            self._preview_scroll_offset = 1 << 30  # clamp on next draw
            return True
        if key == "end":
            self._preview_scroll_offset = 0
            return True

        return False

    def _handle_key_list(self, key_event: KeyEvent) -> bool:
        key = key_event.key
        ctrl = key_event.ctrl

        if key == "escape":
            self._done = True
            return True

        if key == "enter":
            if self._filtered:
                self._result = self._filtered[self._focused_index]
                self._done = True
            return True

        if key == "up" or (ctrl and key == "p"):
            self._move_focus(-1)
            return True
        if key == "down" or (ctrl and key == "n"):
            self._move_focus(1)
            return True
        if key == "pageup":
            self._move_focus(-self._visible_count())
            return True
        if key == "pagedown":
            self._move_focus(self._visible_count())
            return True

        if ctrl and key == "a":
            self._toggle_show_all_projects()
            return True
        if ctrl and key == "b":
            self._toggle_only_current_branch()
            return True

        if key == " " and not self._search_box.value:
            self._show_preview = True
            self._preview_scroll_offset = 0
            return True

        return self._search_box.handle_key(key_event)

    def _scroll_preview(self, delta: int) -> None:
        new_offset = self._preview_scroll_offset + delta
        if new_offset < 0:
            new_offset = 0
        self._preview_scroll_offset = new_offset

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def _on_query_change(self, _query: str) -> None:
        self._apply_filter()

    def _toggle_show_all_projects(self) -> None:
        self._show_all_projects = not self._show_all_projects
        self._reload_entries()

    def _toggle_only_current_branch(self) -> None:
        self._only_current_branch = not self._only_current_branch
        self._apply_filter()

    def _reload_entries(self) -> None:
        if self._entries_override is not None:
            entries = list(self._entries_override)
        elif self._show_all_projects:
            entries = self._index.list_all_projects()
        else:
            entries = self._index.list_for_cwd(self._current_cwd)
        if self._current_session_id:
            entries = [e for e in entries if e.session_id != self._current_session_id]
        self._all_entries = entries
        self._apply_filter()

    def _apply_filter(self) -> None:
        query = self._search_box.value.strip()
        candidates = self._all_entries
        if self._only_current_branch and self._current_branch:
            candidates = [e for e in candidates if e.git_branch == self._current_branch]
        if not query:
            self._filtered = list(candidates)
        else:
            scored: list[tuple[float, SessionEntry]] = []
            for entry in candidates:
                haystack = " ".join(
                    part
                    for part in (
                        entry.name,
                        entry.session_id,
                        entry.title,
                        entry.auto_title,
                        entry.project_name,
                        entry.git_branch or "",
                    )
                    if part
                )
                if entry.session_id.startswith(query):
                    scored.append((1_000_000.0, entry))
                    continue
                score = fuzzy_match(query, haystack)
                if score is not None:
                    scored.append((score, entry))
            scored.sort(key=lambda x: x[0], reverse=True)
            self._filtered = [e for _, e in scored]

        self._focused_index = 0
        self._visible_from = 0

    # ------------------------------------------------------------------
    # Focus / scrolling
    # ------------------------------------------------------------------

    def _move_focus(self, delta: int) -> None:
        n = len(self._filtered)
        if n == 0:
            return
        new_idx = max(0, min(self._focused_index + delta, n - 1))
        self._focused_index = new_idx
        vc = self._visible_count()
        if self._focused_index < self._visible_from:
            self._visible_from = self._focused_index
        elif self._focused_index >= self._visible_from + vc:
            self._visible_from = self._focused_index - vc + 1

    def _visible_count(self) -> int:
        if self._console is None:
            return 5
        height = self._console.size.height
        if height <= 0:
            return 5
        if self._entry_row is not None and self._entry_row > 0:
            available = height - (self._entry_row - 1)
        else:
            available = height // 2
        overhead = 6
        return max(1, (available - overhead) // 2)

    # ------------------------------------------------------------------
    # Rendering — list mode (in-place)
    # ------------------------------------------------------------------

    def render(self) -> RenderableType:
        parts: list[RenderableType] = []

        total = len(self._filtered)
        focus_pos = (self._focused_index + 1) if total else 0
        header = Text()
        header.append(_("Resume Session"), style="bold cyan")
        if total:
            header.append(f" ({focus_pos} of {total})", style="dim")
        parts.append(header)
        parts.append(Text(""))

        parts.append(self._search_box.render())
        parts.append(Text(""))

        if not self._filtered:
            parts.append(Text(_("No sessions found"), style="dim"))
        else:
            parts.extend(self._render_visible_items())

        parts.append(Text(""))
        parts.append(self._render_footer())

        return Group(*parts)

    def _render_visible_items(self) -> list[RenderableType]:
        out: list[RenderableType] = []
        vc = self._visible_count()
        visible = self._filtered[self._visible_from : self._visible_from + vc]

        if self._visible_from > 0:
            out.append(Text("↑", style="dim"))
        last_project: str | None = None
        if self._visible_from > 0:
            prev_entry = self._filtered[self._visible_from - 1]
            last_project = prev_entry.project_name

        for i, entry in enumerate(visible):
            abs_i = self._visible_from + i
            is_focused = abs_i == self._focused_index
            if entry.project_name and entry.project_name != last_project:
                out.append(Text(entry.project_name, style="dim"))
                last_project = entry.project_name
            out.append(self._render_title_line(entry, is_focused))
            out.append(self._render_subtitle_line(entry))

        if self._visible_from + vc < len(self._filtered):
            out.append(Text("↓", style="dim"))

        return out

    @staticmethod
    def _render_title_line(entry: SessionEntry, is_focused: bool) -> Text:
        text = Text()
        if is_focused:
            text.append("❯ ", style="bold cyan")
        else:
            text.append("  ")
        text.append(entry.title, style="bold" if is_focused else "")
        return text

    @staticmethod
    def _render_subtitle_line(entry: SessionEntry) -> Text:
        text = Text("  ", style="dim")
        parts = [_format_relative_time(entry.mtime)]
        if entry.name:
            parts.append(_short_session_id(entry.session_id))
        if entry.git_branch:
            parts.append(entry.git_branch)
        parts.append(_format_size(entry.size_bytes))
        text.append(" · ".join(parts), style="dim")
        return text

    def _render_footer(self) -> Text:
        hints: list[tuple[str, str]] = []
        if self._show_all_projects:
            hints.append(("Ctrl+A", _("show current dir")))
        else:
            hints.append(("Ctrl+A", _("show all projects")))
        if self._current_branch:
            if self._only_current_branch:
                hints.append(("Ctrl+B", _("show all branches")))
            else:
                hints.append(("Ctrl+B", _("only show current branch")))
        hints.append(("Space", _("preview")))
        hints.append(("", _("Type to search")))
        hints.append(("Esc", _("cancel")))

        text = Text()
        for i, (key, label) in enumerate(hints):
            if i > 0:
                text.append(" · ", style="dim")
            if key:
                text.append(key, style="bold")
                text.append(f" {label}", style="dim")
            else:
                text.append(label, style="dim")
        return text

    # ------------------------------------------------------------------
    # Preview mode (alt-screen, full height, scrollable)
    # ------------------------------------------------------------------

    def _run_preview_loop(self, cap: RawInputCapture, screen: ScreenManager) -> None:
        """Show the focused session in the alternate screen until the user resumes / goes back.

        Mouse-wheel events are received as ``wheel_up``/``wheel_down``
        ``KeyEvent``s thanks to SGR mouse tracking — so the preview
        feels native even though the alt-screen has no real scrollback.
        Nothing the preview draws lives in the main-buffer scrollback,
        so pressing ``Esc`` to leave the picker leaves zero residue.
        """
        if self._renderer is None or self._console is None:
            self._show_preview = False
            return

        screen.enter_alternate_screen()
        screen.enable_mouse_tracking()
        try:
            while self._show_preview and not self._done:
                self._draw_preview_alt_screen()
                key_event = cap.read_key(timeout=None)
                if key_event is None:
                    continue
                self.handle_key(key_event)
        finally:
            screen.disable_mouse_tracking()
            screen.leave_alternate_screen()

    def _draw_preview_alt_screen(self) -> None:
        """Repaint the alt-screen with the focused session preview."""
        if self._console is None:
            return
        width = self._console.size.width
        rows = self._console.size.height
        entry = self._filtered[self._focused_index]
        messages = self._load_messages_cached(entry)

        header_lines = self._capture_lines(self._build_preview_header(entry, len(messages)), width)

        body_height = max(1, rows - len(header_lines) - 1)
        self._preview_body_height_last = body_height

        body_lines = self._cached_session_lines(entry, messages, width)
        total = len(body_lines)

        if total <= body_height:
            visible = body_lines
            self._preview_scroll_offset = 0
            above_count = 0
            below_count = 0
        else:
            inner_height = max(1, body_height - 2)
            max_offset = total - inner_height
            if self._preview_scroll_offset > max_offset:
                self._preview_scroll_offset = max_offset
            end = total - self._preview_scroll_offset
            start = end - inner_height
            visible = body_lines[start:end]
            above_count = start
            below_count = total - end

        body_block: list[str] = []
        if total > body_height:
            body_block.append(self._render_scroll_marker("up", above_count, width))
            body_block.extend(visible)
            body_block.append(self._render_scroll_marker("down", below_count, width))
        else:
            body_block.extend(visible)

        if total > body_height:
            window_start = total - self._preview_scroll_offset - len(visible) + 1
            window_end = total - self._preview_scroll_offset
            position = (window_start, window_end, total)
        else:
            position = None
        footer_line = self._build_preview_footer_line(width, len(messages), position)

        out = self._console.file
        out.write("\x1b[H\x1b[2J")
        for line in header_lines:
            out.write(line)
            out.write("\r\n")
        for line in body_block:
            out.write(line)
            out.write("\r\n")
        used = len(header_lines) + len(body_block)
        pad = rows - used - 1
        for _i in range(max(0, pad)):
            out.write("\r\n")
        out.write(f"\x1b[{rows};1H\x1b[2K{footer_line}")
        out.flush()

    def _render_scroll_marker(self, direction: str, count: int, width: int) -> str:
        arrow = "↑" if direction == "up" else "↓"
        if count <= 0:
            text = Text(f"{arrow} ─", style="dim")
        else:
            text = Text(style="dim")
            text.append(arrow, style="bold dim")
            text.append(" ")
            text.append(_("{n} more line{s}").format(n=count, s="" if count == 1 else "s"))
        lines = self._capture_lines(text, width)
        return lines[0] if lines else ""

    def _build_preview_header(self, entry: SessionEntry, msg_count: int) -> RenderableType:
        title = Text()
        title.append(entry.title, style="bold cyan")

        meta = Text(style="dim")
        meta.append(_format_relative_time(entry.mtime))
        if entry.git_branch:
            meta.append(" · ")
            meta.append(entry.git_branch)
        meta.append(" · ")
        meta.append(_("{n} message{s}").format(n=msg_count, s="" if msg_count == 1 else "s"))
        meta.append(" · ")
        meta.append(_format_size(entry.size_bytes))
        return Group(title, meta, Text(""))

    def _build_preview_footer_line(
        self,
        width: int,
        msg_count: int,
        position: tuple[int, int, int] | None = None,
    ) -> str:
        text = Text()
        text.append("Enter", style="bold")
        text.append(" ")
        text.append(_("resume"), style="dim")
        text.append(" · ", style="dim")
        text.append("Esc", style="bold")
        text.append(" ")
        text.append(_("back"), style="dim")
        if msg_count > 0:
            text.append(" · ", style="dim")
            text.append("↑↓", style="bold")
            text.append(" ")
            text.append(_("scroll"), style="dim")
        if position is not None:
            start, end, total = position
            text.append(" · ", style="dim")
            text.append(f"{start}-{end}/{total}", style="dim")
        lines = self._capture_lines(text, width)
        return lines[0] if lines else ""

    def _cached_session_lines(self, entry: SessionEntry, messages: list[Message], width: int) -> list[str]:
        key = (entry.session_id, width)
        cached = self._rendered_body_cache.get(key)
        if cached is not None:
            return cached
        lines = self._build_session_lines(messages, width)
        self._rendered_body_cache[key] = lines
        return lines

    def _build_session_lines(self, messages: list[Message], width: int) -> list[str]:
        if not messages:
            return [_("(empty session)")]
        buf = io.StringIO()
        sub = Console(
            file=buf,
            width=width,
            force_terminal=True,
            color_system="truecolor",
            legacy_windows=False,
            soft_wrap=False,
            highlight=False,
        )
        if self._renderer is not None:
            self._replay_via_renderer(sub, messages)
        else:
            self._fallback_render(sub, messages)
        text = buf.getvalue()
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        return lines

    def _replay_via_renderer(self, console: Console, messages: list[Message]) -> None:
        """Replay messages into ``console`` via :meth:`Renderer.replay_history`.

        Verbose mode is forced *off* so the preview matches the compact
        ``--resume <id>`` look — tool calls collapse to one-line
        summaries (``收到响应 (60 行)``) instead of dumping full
        payloads. Tool details aren't useful for *deciding whether to
        resume*; they'd just bury the conversational flow.
        """
        renderer = self._renderer
        assert renderer is not None

        saved_console = renderer.console
        saved_verbose = renderer._verbose
        saved_text_flushed = renderer._text_flushed
        saved_history = renderer._message_history
        renderer.console = console
        renderer._verbose = False
        renderer._text_flushed = False
        renderer._message_history = []
        try:
            renderer.replay_history(messages)
        finally:
            renderer.console = saved_console
            renderer._verbose = saved_verbose
            renderer._text_flushed = saved_text_flushed
            renderer._message_history = saved_history

    @staticmethod
    def _fallback_render(console: Console, messages: list[Message]) -> None:
        """Minimal renderer used in tests / when no live renderer is provided."""
        first = True
        for msg in messages:
            if not first:
                console.print()
            first = False
            if msg.role == "user":
                content = msg.content
                if isinstance(content, str) and content.strip():
                    line = Text()
                    line.append("❯ ", style="bold cyan")
                    line.append(content)
                    console.print(line)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, ToolResultBlock):
                            line = Text("  ⎿  ", style="dim")
                            preview = block.content.replace("\n", " ").strip()
                            if len(preview) > 200:
                                preview = preview[:200].rstrip() + "…"
                            line.append(preview, style="dim")
                            console.print(line)
            else:
                text = msg.get_text()
                if text.strip():
                    console.print(Text(text))

    def _capture_lines(self, renderable: RenderableType, width: int) -> list[str]:
        buf = io.StringIO()
        sub = Console(
            file=buf,
            width=width,
            force_terminal=True,
            color_system="truecolor",
            legacy_windows=False,
            soft_wrap=False,
            highlight=False,
        )
        sub.print(renderable)
        text = buf.getvalue()
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        return lines

    def _load_messages_cached(self, entry: SessionEntry) -> list[Message]:
        cached = self._messages_cache.get(entry.session_id)
        if cached is not None:
            return cached
        try:
            from iac_code.services.session_storage import SessionStorage

            storage = SessionStorage()
            msgs = storage.load(entry.cwd, entry.session_id)
        except Exception:
            msgs = []
        self._messages_cache[entry.session_id] = msgs
        return msgs


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------


def _format_relative_time(mtime: float) -> str:
    delta = max(0.0, time.time() - mtime)
    seconds = int(delta)
    if seconds < 60:
        return _("just now")
    minutes = seconds // 60
    if minutes < 60:
        return _("{n} minute{s} ago").format(n=minutes, s="" if minutes == 1 else "s")
    hours = minutes // 60
    if hours < 24:
        return _("{n} hour{s} ago").format(n=hours, s="" if hours == 1 else "s")
    days = hours // 24
    return _("{n} day{s} ago").format(n=days, s="" if days == 1 else "s")


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}B"
    kb = size_bytes / 1024
    if kb < 1024:
        return f"{kb:.1f}KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f}MB"
    gb = mb / 1024
    return f"{gb:.1f}GB"


def _short_session_id(session_id: str) -> str:
    if len(session_id) <= 8:
        return session_id
    return session_id[:8]
