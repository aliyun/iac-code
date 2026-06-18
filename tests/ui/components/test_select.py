"""Tests for Select component."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

from rich.console import Console

from iac_code.ui.components.select import (
    InputOption,
    Select,
    SelectLayout,
    TextOption,
)
from iac_code.ui.core.key_event import KeyEvent


def key(k: str, ctrl: bool = False) -> KeyEvent:
    return KeyEvent(key=k, char=k, ctrl=ctrl)


def make_text_options():
    return [
        TextOption(label="Alpha", value="a"),
        TextOption(label="Beta", value="b"),
        TextOption(label="Gamma", value="c"),
    ]


def make_options_with_disabled():
    return [
        TextOption(label="Alpha", value="a"),
        TextOption(label="Disabled", value="d", disabled=True),
        TextOption(label="Gamma", value="c"),
    ]


class TestSelectInitial:
    def test_initial_focus_default(self):
        sel = Select(make_text_options())
        assert sel.state.focused_index == 0

    def test_default_value_sets_focus(self):
        sel = Select(make_text_options(), default_value="b")
        assert sel.state.focused_index == 1

    def test_default_value_missing_focuses_first(self):
        sel = Select(make_text_options(), default_value="z")
        assert sel.state.focused_index == 0


class TestSelectNavigation:
    def test_move_down(self):
        sel = Select(make_text_options())
        sel.handle_key(key("down"))
        assert sel.state.focused_index == 1

    def test_move_up(self):
        sel = Select(make_text_options())
        sel.state.focused_index = 1
        sel.handle_key(key("up"))
        assert sel.state.focused_index == 0

    def test_no_wrap_top(self):
        sel = Select(make_text_options())
        sel.handle_key(key("up"))
        assert sel.state.focused_index == 0

    def test_no_wrap_bottom(self):
        sel = Select(make_text_options())
        sel.state.focused_index = 2
        sel.handle_key(key("down"))
        assert sel.state.focused_index == 2

    def test_ctrl_n_moves_down(self):
        sel = Select(make_text_options())
        sel.handle_key(key("n", ctrl=True))
        assert sel.state.focused_index == 1

    def test_ctrl_p_moves_up(self):
        sel = Select(make_text_options())
        sel.state.focused_index = 1
        sel.handle_key(key("p", ctrl=True))
        assert sel.state.focused_index == 0

    def test_skip_disabled_down(self):
        sel = Select(make_options_with_disabled())
        sel.handle_key(key("down"))
        # index 1 is disabled, should skip to 2
        assert sel.state.focused_index == 2

    def test_skip_disabled_up(self):
        sel = Select(make_options_with_disabled())
        sel.state.focused_index = 2
        sel.handle_key(key("up"))
        # index 1 is disabled, should skip to 0
        assert sel.state.focused_index == 0


class TestSelectPageNavigation:
    def test_pagedown(self):
        options = [TextOption(label=f"Item {i}", value=i) for i in range(20)]
        sel = Select(options, visible_count=5)
        sel.handle_key(key("pagedown"))
        assert sel.state.focused_index == 5

    def test_pagedown_clamps_at_bottom(self):
        options = [TextOption(label=f"Item {i}", value=i) for i in range(5)]
        sel = Select(options, visible_count=10)
        sel.handle_key(key("pagedown"))
        assert sel.state.focused_index == 4

    def test_pageup(self):
        options = [TextOption(label=f"Item {i}", value=i) for i in range(20)]
        sel = Select(options, visible_count=5)
        sel.state.focused_index = 10
        sel.handle_key(key("pageup"))
        assert sel.state.focused_index == 5

    def test_pageup_clamps_at_top(self):
        options = [TextOption(label=f"Item {i}", value=i) for i in range(20)]
        sel = Select(options, visible_count=5)
        sel.state.focused_index = 2
        sel.handle_key(key("pageup"))
        assert sel.state.focused_index == 0


class TestSelectSelection:
    def test_enter_selects_text_option(self):
        results = []
        sel = Select(make_text_options())
        sel._on_select = lambda v: results.append(v)
        sel.handle_key(key("enter"))
        assert results == ["a"]

    def test_escape_cancels(self):
        cancelled = []
        sel = Select(make_text_options())
        sel._on_cancel = lambda: cancelled.append(True)
        sel.handle_key(key("escape"))
        assert cancelled == [True]

    def test_enter_on_disabled_does_nothing(self):
        results = []
        sel = Select(make_options_with_disabled())
        sel.state.focused_index = 1  # disabled
        sel._on_select = lambda v: results.append(v)
        sel.handle_key(key("enter"))
        assert results == []


class TestSelectViewport:
    def test_viewport_scrolls_down(self):
        options = [TextOption(label=f"Item {i}", value=i) for i in range(20)]
        sel = Select(options, visible_count=5)
        # Move focus past visible range
        for _ in range(6):
            sel.handle_key(key("down"))
        assert sel.state.visible_from <= sel.state.focused_index
        assert sel.state.focused_index < sel.state.visible_to

    def test_viewport_scrolls_up(self):
        options = [TextOption(label=f"Item {i}", value=i) for i in range(20)]
        sel = Select(options, visible_count=5)
        # Move to bottom then back up
        sel.state.focused_index = 10
        sel._update_viewport()
        for _ in range(6):
            sel.handle_key(key("up"))
        assert sel.state.visible_from <= sel.state.focused_index
        assert sel.state.focused_index < sel.state.visible_to


class TestSelectInputOption:
    def test_enter_edit_mode_for_input_option(self):
        options = [
            InputOption(label="Name", value="name", placeholder="Enter name"),
        ]
        sel = Select(options)
        sel.handle_key(key("enter"))
        assert sel.state.is_in_input is True

    def test_escape_exits_edit_mode(self):
        options = [
            InputOption(label="Name", value="name", placeholder="Enter name"),
        ]
        sel = Select(options)
        sel.handle_key(key("enter"))  # enter edit mode
        sel.handle_key(key("escape"))  # should exit edit mode, not cancel
        assert sel.state.is_in_input is False


class TestSelectRender:
    def test_render_returns_renderable(self):
        sel = Select(make_text_options())
        result = sel.render()
        assert result is not None

    def test_render_contains_option_labels(self):
        sel = Select(make_text_options())
        result = sel.render()
        console = Console(file=io.StringIO(), force_terminal=False, width=80)
        console.print(result)
        output = console.file.getvalue()
        assert "Alpha" in output
        assert "Beta" in output
        assert "Gamma" in output


class TestSelectExtras:
    """Additional tests to cover previously missed branches."""

    # ---------------------------------------------------------------
    # Lines 106-134: run() method
    # ---------------------------------------------------------------

    def test_run_returns_selected_value(self):
        """run() should return the selected value when Enter is pressed."""
        options = make_text_options()
        sel = Select(options)

        mock_cap = MagicMock()
        # First call returns an "enter" key event, subsequent calls return None
        enter_event = KeyEvent(key="enter", char="enter", ctrl=False)
        mock_cap.__enter__ = MagicMock(return_value=mock_cap)
        mock_cap.__exit__ = MagicMock(return_value=False)
        mock_cap.read_key.side_effect = [enter_event, None]

        with patch("iac_code.ui.core.raw_input.RawInputCapture", return_value=mock_cap):
            with patch("rich.console.Console") as mock_console_cls:
                mock_console = MagicMock()
                mock_console_cls.return_value = mock_console
                result = sel.run()

        assert result == "a"

    def test_run_returns_none_on_cancel(self):
        """run() should return None when Escape is pressed."""
        options = make_text_options()
        sel = Select(options)

        escape_event = KeyEvent(key="escape", char="escape", ctrl=False)
        mock_cap = MagicMock()
        mock_cap.__enter__ = MagicMock(return_value=mock_cap)
        mock_cap.__exit__ = MagicMock(return_value=False)
        mock_cap.read_key.side_effect = [escape_event, None]

        with patch("iac_code.ui.core.raw_input.RawInputCapture", return_value=mock_cap):
            with patch("rich.console.Console") as mock_console_cls:
                mock_console = MagicMock()
                mock_console_cls.return_value = mock_console
                result = sel.run()

        assert result is None

    def test_run_raises_keyboard_interrupt_on_ctrl_c(self):
        """run() should treat Ctrl+C as a hard interrupt, not a silent cancel."""
        options = make_text_options()
        sel = Select(options)

        ctrl_c_event = KeyEvent(key="c", char="\x03", ctrl=True)
        mock_cap = MagicMock()
        mock_cap.__enter__ = MagicMock(return_value=mock_cap)
        mock_cap.__exit__ = MagicMock(return_value=False)
        mock_cap.read_key.side_effect = [ctrl_c_event, None]

        with patch("iac_code.ui.core.raw_input.RawInputCapture", return_value=mock_cap):
            with patch("rich.console.Console") as mock_console_cls:
                mock_console = MagicMock()
                mock_console_cls.return_value = mock_console
                try:
                    sel.run()
                except KeyboardInterrupt:
                    interrupted = True
                else:
                    interrupted = False

        assert interrupted is True

    def test_run_skips_none_key_events(self):
        """run() should continue looping when read_key returns None."""
        options = make_text_options()
        sel = Select(options)

        enter_event = KeyEvent(key="enter", char="enter", ctrl=False)
        mock_cap = MagicMock()
        mock_cap.__enter__ = MagicMock(return_value=mock_cap)
        mock_cap.__exit__ = MagicMock(return_value=False)
        # None then enter — covers the `if key_event is not None` branch
        mock_cap.read_key.side_effect = [None, enter_event]

        with patch("iac_code.ui.core.raw_input.RawInputCapture", return_value=mock_cap):
            with patch("rich.console.Console") as mock_console_cls:
                mock_console = MagicMock()
                mock_console_cls.return_value = mock_console
                result = sel.run()

        assert result == "a"

    # ---------------------------------------------------------------
    # Lines 158-167: input-mode enter and delegate-to-search-box
    # ---------------------------------------------------------------

    def test_enter_in_input_mode_commits_value_and_calls_on_select(self):
        """Enter in input mode should commit the typed value and call on_select."""
        options = [InputOption(label="Name", value="name", placeholder="Enter name")]
        sel = Select(options)
        results = []
        sel._on_select = lambda v: results.append(v)

        # Enter edit mode
        sel.handle_key(key("enter"))
        assert sel.state.is_in_input is True

        # Type some text via the search box
        sel._active_search_box._text = list("Alice")
        sel._active_search_box._cursor = 5

        # Press enter to commit
        consumed = sel.handle_key(key("enter"))
        assert consumed is True
        assert sel.state.is_in_input is False
        assert sel.state.input_values["name"] == "Alice"
        assert results == ["name"]

    def test_input_mode_delegates_other_keys_to_search_box(self):
        """Keys other than escape/enter in input mode should go to the search box."""
        options = [InputOption(label="Name", value="name")]
        sel = Select(options)

        # Enter edit mode
        sel.handle_key(key("enter"))
        assert sel.state.is_in_input is True

        # Send a printable character — should be handled by search box
        char_event = KeyEvent(key="x", char="x", ctrl=False)
        consumed = sel.handle_key(char_event)
        assert consumed is True
        assert sel._active_search_box.value == "x"

    def test_enter_in_input_mode_without_on_select(self):
        """Enter in input mode with no on_select callback should not raise."""
        options = [InputOption(label="Name", value="name")]
        sel = Select(options)
        # No on_select set

        sel.handle_key(key("enter"))  # enter edit mode
        sel.handle_key(key("enter"))  # commit — no on_select
        assert sel.state.is_in_input is False

    # ---------------------------------------------------------------
    # Line 195: return False for unrecognized key
    # ---------------------------------------------------------------

    def test_unrecognized_key_returns_false(self):
        """An unrecognized key should return False."""
        sel = Select(make_text_options())
        result = sel.handle_key(KeyEvent(key="f1", char="", ctrl=False))
        assert result is False

    # ---------------------------------------------------------------
    # Line 205: _move_focus with empty options
    # ---------------------------------------------------------------

    def test_move_focus_empty_options(self):
        """_move_focus on an empty list should return immediately without error."""
        sel = Select([])
        sel._move_focus(1)  # should be a no-op
        assert sel.state.focused_index == 0

    # ---------------------------------------------------------------
    # Line 227: _handle_enter with empty options
    # ---------------------------------------------------------------

    def test_handle_enter_empty_options(self):
        """_handle_enter on an empty list should return False."""
        sel = Select([])
        result = sel._handle_enter()
        assert result is False

    # ---------------------------------------------------------------
    # Lines 258-260: _update_viewport with count == 0
    # ---------------------------------------------------------------

    def test_update_viewport_empty_options(self):
        """_update_viewport with no options should set both indices to 0."""
        sel = Select([])
        sel._update_viewport()
        assert sel.state.visible_from == 0
        assert sel.state.visible_to == 0

    # ---------------------------------------------------------------
    # Lines 302-313: _render_option branches
    # ---------------------------------------------------------------

    def test_render_text_option_with_description(self):
        """TextOption with a description should include the description in output."""
        options = [TextOption(label="Alpha", value="a", description="First letter")]
        sel = Select(options)
        result = sel.render()
        console = Console(file=io.StringIO(), force_terminal=False, width=80)
        console.print(result)
        output = console.file.getvalue()
        assert "First letter" in output

    def test_render_input_option_active_search_box(self):
        """InputOption in active edit mode should render the search box."""
        options = [InputOption(label="Name", value="name", placeholder="Enter name")]
        sel = Select(options)
        sel.handle_key(key("enter"))  # enter edit mode
        result = sel.render()
        console = Console(file=io.StringIO(), force_terminal=False, width=80)
        console.print(result)
        output = console.file.getvalue()
        assert "Name" in output

    def test_render_input_option_with_existing_value(self):
        """InputOption with a stored value should display it."""
        options = [InputOption(label="Name", value="name")]
        sel = Select(options)
        # Pre-set a value
        sel.state.input_values["name"] = "Bob"
        result = sel.render()
        console = Console(file=io.StringIO(), force_terminal=False, width=80)
        console.print(result)
        output = console.file.getvalue()
        assert "Bob" in output

    def test_render_input_option_with_placeholder(self):
        """InputOption with placeholder and no current value should show placeholder."""
        options = [InputOption(label="Name", value="name", placeholder="Enter name")]
        sel = Select(options)
        result = sel.render()
        console = Console(file=io.StringIO(), force_terminal=False, width=80)
        console.print(result)
        output = console.file.getvalue()
        assert "Enter name" in output

    def test_render_input_option_no_value_no_placeholder(self):
        """InputOption with no value and no placeholder should render just the label."""
        options = [InputOption(label="Email", value="email")]
        sel = Select(options)
        result = sel.render()
        console = Console(file=io.StringIO(), force_terminal=False, width=80)
        console.print(result)
        output = console.file.getvalue()
        assert "Email" in output

    def test_layout_compact_is_stored(self):
        """Ensure SelectLayout values can be passed without error."""
        sel = Select(make_text_options(), layout=SelectLayout.COMPACT)
        assert sel._layout == SelectLayout.COMPACT

    def test_escape_without_on_cancel_does_not_raise(self):
        """Escape with no on_cancel callback should return True without error."""
        sel = Select(make_text_options())
        result = sel.handle_key(key("escape"))
        assert result is True

    def test_ctrl_c_marks_interrupt_and_cancels(self):
        """Ctrl+C should end the select loop distinctly from Escape."""
        cancelled = []
        sel = Select(make_text_options())
        sel._on_cancel = lambda: cancelled.append(True)

        result = sel.handle_key(key("c", ctrl=True))

        assert result is True
        assert cancelled == [True]
        assert sel._interrupted is True
