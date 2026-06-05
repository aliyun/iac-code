from __future__ import annotations

from io import StringIO
from os import terminal_size

from rich.cells import cell_len
from rich.console import Console

from iac_code.ui.core.key_event import KeyEvent
from iac_code.ui.dialogs import memory_editor as memory_editor_module
from iac_code.ui.dialogs.memory_editor import FullscreenRenderer, VimMemoryEditor


def _keys(editor: VimMemoryEditor, keys: list[str]) -> None:
    for key in keys:
        char = key if len(key) == 1 else ""
        editor.handle_key(KeyEvent(key=key, char=char))


def test_vim_memory_editor_saves_only_when_content_changed():
    editor = VimMemoryEditor("old", title="IAC-CODE.md")

    _keys(editor, ["i", "!", "escape", ":", "w", "q", "enter"])

    assert editor.result is not None
    assert editor.result.status == "saved"
    assert editor.result.content == "!old"


def test_vim_memory_editor_reports_unchanged_on_wq_without_changes():
    editor = VimMemoryEditor("old", title="IAC-CODE.md")

    _keys(editor, [":", "w", "q", "enter"])

    assert editor.result is not None
    assert editor.result.status == "unchanged"
    assert editor.result.content == "old"


def test_vim_memory_editor_discards_changes_with_q_bang():
    editor = VimMemoryEditor("old", title="IAC-CODE.md")

    _keys(editor, ["i", "!", "escape", ":", "q", "!", "enter"])

    assert editor.result is not None
    assert editor.result.status == "cancelled"
    assert editor.result.content == "old"


def test_vim_memory_editor_supports_dd_delete_line():
    editor = VimMemoryEditor("one\ntwo", title="IAC-CODE.md")

    _keys(editor, ["d", "d", ":", "w", "q", "enter"])

    assert editor.result is not None
    assert editor.result.status == "saved"
    assert editor.result.content == "two"


def test_vim_memory_editor_renders_focused_terminal_layout():
    editor = VimMemoryEditor("one\ntwo", title="Project memory", path="./IAC-CODE.md")

    lines = editor.render_lines()

    assert "Project memory" in lines[0]
    assert "./IAC-CODE.md" in lines[0]
    assert lines[1].startswith(" 1 │ one")
    assert lines[2].startswith(" 2 │ two")
    assert "NORMAL" in lines[-1]
    assert ":wq save" in lines[-1]


def test_vim_memory_editor_leaves_last_terminal_column_empty(monkeypatch):
    monkeypatch.setattr(
        memory_editor_module.shutil,
        "get_terminal_size",
        lambda fallback: terminal_size((20, 6)),
    )
    editor = VimMemoryEditor("one\ntwo", title="Project memory", path="./IAC-CODE.md")

    assert all(cell_len(line) <= 19 for line in editor.render_lines())


def test_vim_memory_editor_exposes_cursor_position_inside_text_body():
    editor = VimMemoryEditor("old", title="IAC-CODE.md")

    _keys(editor, ["i", "!", "escape"])

    assert editor.cursor_position() == (1, 6)


def test_vim_memory_editor_cursor_position_uses_display_width_for_wide_chars():
    editor = VimMemoryEditor("测试", title="IAC-CODE.md")

    _keys(editor, ["a"])

    assert editor.cursor_position() == (1, 7)


def test_vim_memory_editor_run_renders_with_cursor_position():
    class FakeRenderer:
        def __init__(self):
            self.cursor_positions: list[tuple[int, int] | None] = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return None

        def render(self, renderable, cursor_to=None):
            self.cursor_positions.append(cursor_to)

    class FakeInput:
        def __init__(self):
            self.events = [
                KeyEvent("i", "i"),
                KeyEvent("!", "!"),
                KeyEvent("escape", ""),
                KeyEvent(":", ":"),
                KeyEvent("w", "w"),
                KeyEvent("q", "q"),
                KeyEvent("enter", "\n"),
            ]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return None

        def read_key(self, timeout=None):
            return self.events.pop(0) if self.events else None

    renderer = FakeRenderer()
    editor = VimMemoryEditor("old", title="IAC-CODE.md")

    result = editor.run(renderer=renderer, input_capture=FakeInput())

    assert result.status == "saved"
    assert (1, 6) in renderer.cursor_positions


def test_vim_memory_editor_does_not_repaint_without_state_change():
    class FakeRenderer:
        def __init__(self):
            self.render_count = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return None

        def render(self, renderable, cursor_to=None):
            self.render_count += 1

    class FakeInput:
        def __init__(self):
            self.events = [
                None,
                KeyEvent("mouse", ""),
                None,
                KeyEvent(":", ":"),
                KeyEvent("q", "q"),
                KeyEvent("!", "!"),
                KeyEvent("enter", "\n"),
            ]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return None

        def read_key(self, timeout=None):
            return self.events.pop(0) if self.events else None

    renderer = FakeRenderer()
    editor = VimMemoryEditor("old", title="IAC-CODE.md")

    result = editor.run(renderer=renderer, input_capture=FakeInput())

    assert result.status == "cancelled"
    assert renderer.render_count == 4


def test_fullscreen_renderer_uses_alternate_screen_and_moves_cursor():
    stream = StringIO()
    console = Console(file=stream, force_terminal=True, width=40, color_system=None)

    with FullscreenRenderer(console) as renderer:
        renderer.render("hello", cursor_to=(2, 3))

    output = stream.getvalue()
    assert "\x1b[?1049h" in output
    assert "\x1b[3;4H" in output
    assert "\x1b[?1049l" in output


def test_fullscreen_renderer_does_not_scroll_before_moving_cursor():
    stream = StringIO()
    console = Console(file=stream, force_terminal=True, width=20, color_system=None)

    with FullscreenRenderer(console) as renderer:
        renderer.render("hello", cursor_to=(1, 5))

    output = stream.getvalue()
    assert "\r\n\x1b[2;6H" not in output
