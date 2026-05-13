"""Tests for SearchBox component."""

from rich.text import Text

from iac_code.ui.components.search_box import SearchBox
from iac_code.ui.core.key_event import KeyEvent


def key(k, char=None, ctrl=False, alt=False):
    return KeyEvent(key=k, char=char if char is not None else k, ctrl=ctrl, alt=alt)


class TestSearchBox:
    def test_initial_value(self):
        sb = SearchBox(initial_value="hello")
        assert sb.value == "hello"
        assert sb.cursor == 5

    def test_initial_empty(self):
        sb = SearchBox()
        assert sb.value == ""
        assert sb.cursor == 0

    def test_char_input(self):
        sb = SearchBox()
        sb.handle_key(key("a", char="a"))
        sb.handle_key(key("b", char="b"))
        assert sb.value == "ab"
        assert sb.cursor == 2

    def test_char_input_returns_true(self):
        sb = SearchBox()
        result = sb.handle_key(key("a", char="a"))
        assert result is True

    def test_backspace(self):
        sb = SearchBox(initial_value="hello")
        sb.handle_key(key("backspace"))
        assert sb.value == "hell"
        assert sb.cursor == 4

    def test_backspace_empty(self):
        sb = SearchBox()
        result = sb.handle_key(key("backspace"))
        assert sb.value == ""
        assert result is True  # handled (no-op but consumed)

    def test_delete(self):
        sb = SearchBox(initial_value="hello")
        # move to start
        sb.handle_key(key("home"))
        sb.handle_key(key("delete"))
        assert sb.value == "ello"
        assert sb.cursor == 0

    def test_delete_at_end(self):
        sb = SearchBox(initial_value="hello")
        sb.handle_key(key("delete"))
        # cursor at end, nothing to delete
        assert sb.value == "hello"

    def test_left_right(self):
        sb = SearchBox(initial_value="hello")
        sb.handle_key(key("left"))
        assert sb.cursor == 4
        sb.handle_key(key("right"))
        assert sb.cursor == 5

    def test_left_boundary(self):
        sb = SearchBox()
        sb.handle_key(key("left"))
        assert sb.cursor == 0

    def test_right_boundary(self):
        sb = SearchBox(initial_value="hi")
        sb.handle_key(key("right"))
        assert sb.cursor == 2

    def test_home(self):
        sb = SearchBox(initial_value="hello")
        sb.handle_key(key("home"))
        assert sb.cursor == 0

    def test_end(self):
        sb = SearchBox(initial_value="hello")
        sb.handle_key(key("home"))
        sb.handle_key(key("end"))
        assert sb.cursor == 5

    def test_ctrl_a(self):
        sb = SearchBox(initial_value="hello")
        sb.handle_key(key("a", ctrl=True))
        assert sb.cursor == 0

    def test_ctrl_e(self):
        sb = SearchBox(initial_value="hello")
        sb.handle_key(key("home"))
        sb.handle_key(key("e", ctrl=True))
        assert sb.cursor == 5

    def test_ctrl_k(self):
        sb = SearchBox(initial_value="hello world")
        # move to position 5
        sb.handle_key(key("home"))
        for _ in range(5):
            sb.handle_key(key("right"))
        sb.handle_key(key("k", ctrl=True))
        assert sb.value == "hello"
        assert sb.cursor == 5

    def test_ctrl_u(self):
        sb = SearchBox(initial_value="hello world")
        # cursor at end
        sb.handle_key(key("u", ctrl=True))
        assert sb.value == ""
        assert sb.cursor == 0

    def test_ctrl_w(self):
        sb = SearchBox(initial_value="hello world")
        # cursor at end, delete previous word "world"
        sb.handle_key(key("w", ctrl=True))
        assert sb.value == "hello "
        assert sb.cursor == 6

    def test_ctrl_w_with_leading_spaces(self):
        sb = SearchBox(initial_value="hello   ")
        # cursor at end, should delete spaces and word before them... or just spaces
        sb.handle_key(key("w", ctrl=True))
        assert sb.value == "hello"
        assert sb.cursor == 5

    def test_on_change_callback(self):
        changes = []
        sb = SearchBox(on_change=lambda v: changes.append(v))
        sb.handle_key(key("a", char="a"))
        sb.handle_key(key("b", char="b"))
        assert changes == ["a", "ab"]

    def test_on_change_not_called_for_navigation(self):
        changes = []
        sb = SearchBox(initial_value="hello", on_change=lambda v: changes.append(v))
        sb.handle_key(key("left"))
        sb.handle_key(key("right"))
        sb.handle_key(key("home"))
        sb.handle_key(key("end"))
        assert changes == []

    def test_render_returns_text(self):
        sb = SearchBox(initial_value="hi")
        result = sb.render()
        assert isinstance(result, Text)

    def test_render_contains_value(self):
        sb = SearchBox(initial_value="hello")
        result = sb.render()
        plain = result.plain
        assert "hello" in plain

    def test_render_has_prompt(self):
        sb = SearchBox(initial_value="hello")
        result = sb.render()
        plain = result.plain
        assert ">" in plain

    def test_unhandled_key_returns_false(self):
        sb = SearchBox()
        # F-keys should not be handled
        result = sb.handle_key(key("f1"))
        assert result is False

    def test_char_insert_in_middle(self):
        sb = SearchBox(initial_value="hllo")
        sb.handle_key(key("home"))
        sb.handle_key(key("right"))  # cursor at 1
        sb.handle_key(key("e", char="e"))
        assert sb.value == "hello"
        assert sb.cursor == 2

    def test_placeholder(self):
        sb = SearchBox(placeholder="Search...")
        assert sb.value == ""
        result = sb.render()
        assert isinstance(result, Text)
