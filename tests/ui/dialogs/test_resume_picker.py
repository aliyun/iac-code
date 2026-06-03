"""Tests for ResumePicker dialog (rendering and key handling)."""

from __future__ import annotations

import io
import time
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console as RichConsole

from iac_code.agent.message import Message
from iac_code.services.session_index import SessionEntry, SessionIndex
from iac_code.services.session_storage import SessionStorage
from iac_code.ui.core.key_event import KeyEvent
from iac_code.ui.dialogs.resume_picker import (
    ResumePicker,
    _format_relative_time,
    _format_size,
)


@pytest.fixture
def two_session_index(tmp_path):
    """Two sessions in the same project, one in another."""
    storage = SessionStorage(projects_dir=tmp_path)
    storage.append("/proj/a", "id-aa", Message(role="user", content="alpha"), git_branch="main")
    storage.append("/proj/a", "id-ab", Message(role="user", content="beta"), git_branch="dev")
    storage.append("/proj/b", "id-bc", Message(role="user", content="other-proj"), git_branch="main")
    return SessionIndex(projects_dir=tmp_path)


@pytest.fixture
def picker(two_session_index):
    with patch("iac_code.ui.dialogs.resume_picker.get_git_branch", return_value="main"):
        yield ResumePicker(
            index=two_session_index,
            current_cwd="/proj/a",
            current_session_id=None,
        )


def _make_renderer():
    """A tiny Renderer with Bash registered, writing to a throwaway StringIO."""
    from iac_code.tools.base import ToolRegistry
    from iac_code.tools.bash import BashTool
    from iac_code.ui.renderer import Renderer

    registry = ToolRegistry()
    registry.register(BashTool())
    scratch = RichConsole(file=io.StringIO(), force_terminal=True, width=80)
    return Renderer(scratch, registry)


def _entry(**overrides) -> SessionEntry:
    defaults = dict(
        session_id="1234567890abcdef",
        cwd="/proj/a",
        project_name="a",
        git_branch="main",
        title="deploy-prod",
        mtime=time.time(),
        size_bytes=42,
        name=None,
        auto_title="create vpc resources",
        is_legacy=False,
    )
    defaults.update(overrides)
    return SessionEntry(**defaults)


class TestResumePickerLoad:
    def test_default_view_is_current_cwd(self, picker):
        ids = [e.session_id for e in picker._all_entries]
        assert set(ids) == {"id-aa", "id-ab"}

    def test_filtered_initially_matches_all(self, picker):
        assert len(picker._filtered) == 2

    def test_excludes_current_session(self, two_session_index):
        with patch("iac_code.ui.dialogs.resume_picker.get_git_branch", return_value=None):
            p = ResumePicker(
                index=two_session_index,
                current_cwd="/proj/a",
                current_session_id="id-ab",
            )
        ids = [e.session_id for e in p._all_entries]
        assert ids == ["id-aa"]

    def test_supplied_entries_are_used_without_index_reload(self):
        index = MagicMock()
        index.list_for_cwd = MagicMock(side_effect=AssertionError("should not reload from index"))
        index.list_all_projects = MagicMock(side_effect=AssertionError("should not reload from index"))
        entries = [
            _entry(session_id="candidate-a"),
            _entry(session_id="current-session"),
            _entry(session_id="candidate-b"),
        ]

        with patch("iac_code.ui.dialogs.resume_picker.get_git_branch", return_value=None):
            p = ResumePicker(
                index=index,
                current_cwd="/proj/a",
                current_session_id="current-session",
                entries=entries,
            )

        assert [entry.session_id for entry in p._all_entries] == ["candidate-a", "candidate-b"]

    def test_supplied_entries_are_not_reloaded_when_toggling_all_projects(self):
        index = MagicMock()
        index.list_for_cwd = MagicMock(side_effect=AssertionError("should not reload from index"))
        index.list_all_projects = MagicMock(side_effect=AssertionError("should not reload from index"))
        entries = [_entry(session_id="candidate-a")]

        with patch("iac_code.ui.dialogs.resume_picker.get_git_branch", return_value=None):
            p = ResumePicker(
                index=index,
                current_cwd="/proj/a",
                current_session_id=None,
                entries=entries,
            )
        p.handle_key(KeyEvent(key="a", char="\x01", ctrl=True))

        assert [entry.session_id for entry in p._all_entries] == ["candidate-a"]


class TestResumePickerKeys:
    def test_escape_cancels(self, picker):
        picker.handle_key(KeyEvent(key="escape", char="\x1b"))
        assert picker._done
        assert picker._result is None

    def test_ctrl_c_cancels(self, picker):
        picker.handle_key(KeyEvent(key="c", char="\x03", ctrl=True))
        assert picker._done
        assert picker._result is None

    def test_enter_selects_focused(self, picker):
        picker.handle_key(KeyEvent(key="enter", char="\r"))
        assert picker._done
        assert picker._result is not None

    def test_ctrl_a_toggles_all_projects(self, picker):
        picker.handle_key(KeyEvent(key="a", char="\x01", ctrl=True))
        assert picker._show_all_projects
        ids = {e.session_id for e in picker._all_entries}
        assert ids == {"id-aa", "id-ab", "id-bc"}

    def test_ctrl_b_toggles_branch_filter(self, picker):
        picker.handle_key(KeyEvent(key="b", char="\x02", ctrl=True))
        assert picker._only_current_branch
        assert all(e.git_branch == "main" for e in picker._filtered)

    def test_space_enters_preview_when_search_empty(self, picker):
        picker.handle_key(KeyEvent(key=" ", char=" "))
        assert picker._show_preview is True
        # Esc returns to list (NOT cancel).
        picker.handle_key(KeyEvent(key="escape", char="\x1b"))
        assert picker._show_preview is False
        assert not picker._done

    def test_space_inserts_when_search_has_text(self, picker):
        picker.handle_key(KeyEvent(key="a", char="a"))
        picker.handle_key(KeyEvent(key=" ", char=" "))
        assert picker._search_box.value == "a "
        assert picker._show_preview is False

    def test_escape_in_preview_returns_to_list(self, picker):
        picker._show_preview = True
        picker.handle_key(KeyEvent(key="escape", char="\x1b"))
        assert picker._show_preview is False
        assert not picker._done

    def test_enter_in_preview_selects_focused(self, picker):
        picker._show_preview = True
        picker.handle_key(KeyEvent(key="enter", char="\r"))
        assert picker._done
        assert picker._result is not None

    def test_ctrl_c_in_preview_cancels(self, picker):
        picker._show_preview = True
        picker.handle_key(KeyEvent(key="c", char="\x03", ctrl=True))
        assert picker._done
        assert picker._result is None

    def test_arrow_keys_scroll_preview_body(self, picker):
        picker._show_preview = True
        picker._preview_scroll_offset = 0
        picker.handle_key(KeyEvent(key="up", char=""))
        assert picker._preview_scroll_offset == 1
        picker.handle_key(KeyEvent(key="down", char=""))
        assert picker._preview_scroll_offset == 0
        # Down clamped at 0.
        picker.handle_key(KeyEvent(key="down", char=""))
        assert picker._preview_scroll_offset == 0
        # Focus must NOT change while scrolling preview.
        assert picker._focused_index == 0

    def test_wheel_scrolls_preview_body(self, picker):
        # Mouse wheel ticks scroll several lines at once so spinning
        # feels responsive.
        from iac_code.ui.dialogs.resume_picker import _WHEEL_LINES

        picker._show_preview = True
        picker._preview_scroll_offset = 0
        picker.handle_key(KeyEvent(key="wheel_up", char=""))
        assert picker._preview_scroll_offset == _WHEEL_LINES
        picker.handle_key(KeyEvent(key="wheel_down", char=""))
        assert picker._preview_scroll_offset == 0
        # Wheel down at offset 0 must clamp.
        picker.handle_key(KeyEvent(key="wheel_down", char=""))
        assert picker._preview_scroll_offset == 0

    def test_pageup_pagedown_scroll_by_screen(self, picker):
        picker._show_preview = True
        picker._preview_body_height_last = 10
        picker._preview_scroll_offset = 0
        picker.handle_key(KeyEvent(key="pageup", char=""))
        assert picker._preview_scroll_offset == 9
        picker.handle_key(KeyEvent(key="pagedown", char=""))
        assert picker._preview_scroll_offset == 0

    def test_home_end_jump_to_top_and_bottom(self, picker):
        picker._show_preview = True
        picker.handle_key(KeyEvent(key="home", char=""))
        assert picker._preview_scroll_offset >= 1 << 20
        picker.handle_key(KeyEvent(key="end", char=""))
        assert picker._preview_scroll_offset == 0

    def test_list_mode_keys_ignored_in_preview(self, picker):
        picker._show_preview = True
        before = picker._show_all_projects
        picker.handle_key(KeyEvent(key="a", char="\x01", ctrl=True))
        assert picker._show_all_projects == before

    def test_space_resets_scroll_offset_when_entering_preview(self, picker):
        picker._preview_scroll_offset = 99
        picker._show_preview = False
        picker.handle_key(KeyEvent(key=" ", char=" "))
        assert picker._show_preview is True
        assert picker._preview_scroll_offset == 0

    def test_typing_filters_entries(self, picker):
        for ch in "alph":
            picker.handle_key(KeyEvent(key=ch, char=ch))
        ids = [e.session_id for e in picker._filtered]
        assert ids == ["id-aa"]

    def test_search_matches_name_session_id_auto_title_project_and_branch(self, two_session_index):
        entries = [
            _entry(
                session_id="session-by-id",
                project_name="networking",
                git_branch="feature-branch",
                title="title text",
                name="named-session",
                auto_title="auto title text",
            )
        ]
        for query in ("named", "session-by-id", "title", "auto", "networking", "feature"):
            with patch("iac_code.ui.dialogs.resume_picker.get_git_branch", return_value=None):
                p = ResumePicker(
                    index=two_session_index,
                    current_cwd="/proj/a",
                    current_session_id=None,
                    entries=entries,
                )
            for ch in query:
                p.handle_key(KeyEvent(key=ch, char=ch))
            assert [entry.session_id for entry in p._filtered] == ["session-by-id"]

    def test_down_arrow_moves_focus(self, picker):
        assert len(picker._filtered) >= 2
        starting = picker._focused_index
        picker.handle_key(KeyEvent(key="down", char=""))
        assert picker._focused_index == starting + 1


class TestResumePickerRender:
    def test_render_returns_renderable(self, picker):
        from rich.console import Group

        out = picker.render()
        assert isinstance(out, Group)

    def test_render_when_empty(self, two_session_index):
        with patch("iac_code.ui.dialogs.resume_picker.get_git_branch", return_value=None):
            p = ResumePicker(
                index=two_session_index,
                current_cwd="/no/such/proj",
                current_session_id=None,
            )
        p.render()

    def test_named_subtitle_includes_short_session_id(self):
        entry = _entry(session_id="1234567890abcdef", name="deploy-prod", title="deploy-prod")
        subtitle = ResumePicker._render_subtitle_line(entry).plain

        assert "12345678" in subtitle
        assert "123456789" not in subtitle


class TestResumePickerPreviewDraw:
    def _picker_with_console(self, two_session_index, *, height=40, width=80):
        with patch("iac_code.ui.dialogs.resume_picker.get_git_branch", return_value="main"):
            p = ResumePicker(
                index=two_session_index,
                current_cwd="/proj/a",
                current_session_id=None,
            )
        buf = io.StringIO()
        p._console = RichConsole(file=buf, force_terminal=True, width=width, height=height)
        return p, buf

    def test_draw_writes_clear_screen_and_footer(self, two_session_index):
        p, buf = self._picker_with_console(two_session_index)
        p._show_preview = True
        p._renderer = _make_renderer()
        p._draw_preview_alt_screen()
        out = buf.getvalue()
        assert out.startswith("\x1b[H\x1b[2J")  # clear screen each draw
        assert "Enter" in out
        assert "Esc" in out

    def test_draw_uses_full_terminal_height(self, two_session_index):
        p, buf = self._picker_with_console(two_session_index, height=20, width=60)
        p._show_preview = True
        p._renderer = _make_renderer()
        entry = p._filtered[0]
        p._messages_cache[entry.session_id] = [Message(role="user", content=f"msg {i}") for i in range(200)]
        p._draw_preview_alt_screen()
        # rows(20) - header(3) - footer(1) = 16
        assert p._preview_body_height_last == 16

    def test_draw_shows_scroll_markers_when_overflowing(self, two_session_index):
        p, buf = self._picker_with_console(two_session_index, height=20, width=60)
        p._show_preview = True
        p._renderer = _make_renderer()
        entry = p._filtered[0]
        p._messages_cache[entry.session_id] = [Message(role="user", content=f"msg-{i}") for i in range(30)]
        p._preview_scroll_offset = 5  # mid-scroll
        p._draw_preview_alt_screen()
        out = buf.getvalue()
        assert "↑" in out
        assert "↓" in out

    def test_draw_zero_offset_shows_latest_lines(self, two_session_index):
        p, buf = self._picker_with_console(two_session_index, height=20, width=60)
        p._show_preview = True
        p._renderer = _make_renderer()
        entry = p._filtered[0]
        p._messages_cache[entry.session_id] = [Message(role="user", content=f"line-{i}") for i in range(30)]
        p._preview_scroll_offset = 0
        p._draw_preview_alt_screen()
        out = buf.getvalue()
        assert "line-29" in out
        assert "line-0\n" not in out

    def test_run_preview_uses_alt_screen_and_mouse_tracking(self, two_session_index, monkeypatch):
        # The whole point of this redesign: the preview MUST run inside
        # the alternate screen with mouse tracking on, so wheel events
        # are forwarded to us as KeyEvents AND the preview leaves zero
        # residue in the user's main-buffer scrollback when it exits.
        from iac_code.ui.core.raw_input import RawInputCapture
        from iac_code.ui.core.screen import ScreenManager

        p, buf = self._picker_with_console(two_session_index)
        p._renderer = _make_renderer()
        p._show_preview = True
        screen = ScreenManager(p._console)
        cap = RawInputCapture.__new__(RawInputCapture)

        def fake_read(timeout=None):
            return KeyEvent(key="escape", char="\x1b")  # exit immediately

        monkeypatch.setattr(cap, "read_key", fake_read)
        p._run_preview_loop(cap, screen)
        out = buf.getvalue()
        assert "\033[?1049h" in out  # alt-screen on
        assert "\033[?1049l" in out  # alt-screen off
        assert "\033[?1000h" in out and "\033[?1006h" in out  # mouse on
        assert "\033[?1006l" in out and "\033[?1000l" in out  # mouse off

    def test_replay_preserves_renderer_state(self, two_session_index):
        # The picker swaps renderer.console / verbose / message_history
        # while replaying — must restore everything afterwards.
        from iac_code.ui.renderer import RenderedTurn

        p, buf = self._picker_with_console(two_session_index)
        renderer = _make_renderer()
        sentinel = RenderedTurn(role="user", text="sentinel")
        renderer._message_history.append(sentinel)
        renderer._verbose = False
        p._renderer = renderer
        capture = io.StringIO()
        sub = RichConsole(file=capture, force_terminal=True, width=80)
        msgs = [Message(role="user", content="hi"), Message(role="assistant", content="hello")]
        p._replay_via_renderer(sub, msgs)
        assert renderer._message_history == [sentinel]
        assert renderer._verbose is False

    def test_renderer_backed_preview_truncates_long_tool_results(self, two_session_index):
        # Compact mode: BashTool truncates output beyond MAX_OUTPUT_LINES
        # (20). Longer payloads are replaced with a tail summary like
        # ``... N more lines``. Without compact mode the entire body
        # would dump into the preview.
        from iac_code.agent.message import ToolResultBlock, ToolUseBlock
        from iac_code.tools.bash import BashTool

        max_lines = BashTool.MAX_OUTPUT_LINES
        p, _buf = self._picker_with_console(two_session_index, width=120)
        p._renderer = _make_renderer()

        body_lines = [f"line-{i}" for i in range(max_lines + 10)]
        msgs = [
            Message(
                role="assistant",
                content=[ToolUseBlock(id="t1", name="bash", input={"command": "ls"})],
            ),
            Message(role="user", content=[ToolResultBlock(tool_use_id="t1", content="\n".join(body_lines))]),
        ]
        rendered = "\n".join(p._build_session_lines(msgs, 120))
        # Lines in the truncated tail are hidden.
        assert f"line-{max_lines + 5}" not in rendered
        # And the "more lines" summary appears.
        assert "more lines" in rendered

    def test_renderer_backed_preview_uses_user_facing_tool_name(self, two_session_index):
        from iac_code.agent.message import ToolResultBlock, ToolUseBlock

        p, _buf = self._picker_with_console(two_session_index, width=120)
        p._renderer = _make_renderer()
        msgs = [
            Message(
                role="assistant",
                content=[ToolUseBlock(id="t1", name="bash", input={"command": "ls -la"})],
            ),
            Message(role="user", content=[ToolResultBlock(tool_use_id="t1", content="ok")]),
        ]
        lines = p._build_session_lines(msgs, 120)
        joined = "\n".join(lines)
        assert "Bash" in joined  # user_facing_name, not "bash"


class TestResumePickerVisibleCount:
    def _picker_with_console(self, two_session_index, height=40, entry_row=3):
        from rich.console import Console

        with patch("iac_code.ui.dialogs.resume_picker.get_git_branch", return_value="main"):
            p = ResumePicker(
                index=two_session_index,
                current_cwd="/proj/a",
                current_session_id=None,
            )
        p._console = Console(force_terminal=True, width=80, height=height)
        p._entry_row = entry_row
        return p

    def test_visible_count_uses_remaining_viewport_height(self, two_session_index):
        p = self._picker_with_console(two_session_index, height=40, entry_row=3)
        assert p._visible_count() == 16

    def test_visible_count_falls_back_when_dsr_unanswered(self, two_session_index):
        p = self._picker_with_console(two_session_index, height=40, entry_row=None)
        p._entry_row = None
        assert p._visible_count() == 7

    def test_visible_count_clamps_to_at_least_one(self, two_session_index):
        p = self._picker_with_console(two_session_index, height=5, entry_row=4)
        assert p._visible_count() >= 1


class TestResumePickerInPlaceRender:
    def test_render_via_in_place_renderer_uses_crlf(self, picker):
        from rich.console import Console

        from iac_code.ui.core.in_place_render import InPlaceRenderer

        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True, width=80, height=40)
        renderer = InPlaceRenderer(console)
        renderer.render(picker.render())
        out = buf.getvalue()
        assert "\r\n" in out
        assert renderer.last_height > 1


class TestFormatHelpers:
    def test_format_relative_time_minutes(self):
        now = time.time()
        assert "minute" in _format_relative_time(now - 120)

    def test_format_relative_time_just_now(self):
        assert _format_relative_time(time.time()) in ("just now",)

    def test_format_size_units(self):
        assert _format_size(500) == "500B"
        assert _format_size(2048).endswith("KB")
        assert _format_size(2 * 1024 * 1024).endswith("MB")
