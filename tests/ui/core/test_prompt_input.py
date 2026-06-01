"""Tests for PromptInput._handle_key (unit tests; no terminal required)."""

from __future__ import annotations

import os
from io import StringIO
from types import SimpleNamespace

from iac_code.ui.core.key_event import KeyEvent
from iac_code.ui.core.prompt_input import PromptInput
from iac_code.ui.keybindings.manager import KeybindingManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_input(**kwargs) -> PromptInput:
    """Return a PromptInput with a fresh KeybindingManager."""
    km = KeybindingManager()
    return PromptInput(keybinding_manager=km, **kwargs)


class DummyAggregator:
    def __init__(self) -> None:
        self.suggestions = []
        self.ghost_text = ""
        self.visible_suggestions = []
        self.visible_selected_index = 0
        self.has_more_above = False
        self.has_more_below = False
        self.updated = []
        self.dismissed = False

    def update(self, text: str, cursor: int) -> None:
        self.updated.append((text, cursor))

    def dismiss(self) -> None:
        self.dismissed = True

    def accept_selected(self):
        return None

    def accept_ghost_text(self):
        return None

    def move_selection(self, delta: int) -> None:
        self.visible_selected_index += delta


def _key(key: str, *, ctrl: bool = False, alt: bool = False, shift: bool = False) -> KeyEvent:
    """Build a KeyEvent from a key name."""
    char = key if len(key) == 1 else ""
    return KeyEvent(key=key, char=char, ctrl=ctrl, alt=alt, shift=shift)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPromptInputHandleKey:
    def test_char_input(self):
        inp = make_input()
        inp._handle_key(_key("h"))
        inp._handle_key(_key("i"))
        assert inp._get_text() == "hi"
        assert inp._cursor == 2

    def test_backspace(self):
        inp = make_input()
        inp._handle_key(_key("a"))
        inp._handle_key(_key("b"))
        inp._handle_key(_key("backspace"))
        assert inp._get_text() == "a"
        assert inp._cursor == 1

    def test_backspace_at_start_no_op(self):
        inp = make_input()
        inp._handle_key(_key("backspace"))
        assert inp._get_text() == ""
        assert inp._cursor == 0

    def test_delete_key(self):
        inp = make_input()
        inp._handle_key(_key("a"))
        inp._handle_key(_key("b"))
        # Move cursor to position 1 (between a and b)
        inp._handle_key(_key("left"))
        inp._handle_key(_key("delete"))
        assert inp._get_text() == "a"

    def test_cursor_movement(self):
        inp = make_input()
        for ch in "hello":
            inp._handle_key(_key(ch))
        assert inp._cursor == 5
        inp._handle_key(_key("left"))
        assert inp._cursor == 4
        inp._handle_key(_key("right"))
        assert inp._cursor == 5

    def test_left_at_start_no_op(self):
        inp = make_input()
        inp._handle_key(_key("left"))
        assert inp._cursor == 0

    def test_right_at_end_no_op(self):
        inp = make_input()
        inp._handle_key(_key("a"))
        inp._handle_key(_key("right"))
        assert inp._cursor == 1  # already at end

    def test_home_end(self):
        inp = make_input()
        for ch in "hello":
            inp._handle_key(_key(ch))
        inp._handle_key(KeyEvent(key="a", char="\x01", ctrl=True))  # Ctrl+A = home
        assert inp._cursor == 0
        inp._handle_key(KeyEvent(key="e", char="\x05", ctrl=True))  # Ctrl+E = end
        assert inp._cursor == 5

    def test_home_key(self):
        inp = make_input()
        for ch in "hello":
            inp._handle_key(_key(ch))
        inp._handle_key(_key("home"))
        assert inp._cursor == 0

    def test_end_key(self):
        inp = make_input()
        for ch in "hello":
            inp._handle_key(_key(ch))
        inp._handle_key(_key("home"))
        inp._handle_key(_key("end"))
        assert inp._cursor == 5

    def test_ctrl_k(self):
        inp = make_input()
        for ch in "hello world":
            inp._handle_key(_key(ch))
        # Move cursor to position 5 (between "hello" and " world")
        for _ in range(6):
            inp._handle_key(_key("left"))
        assert inp._cursor == 5
        inp._handle_key(KeyEvent(key="k", char="\x0b", ctrl=True))
        assert inp._get_text() == "hello"
        assert inp._cursor == 5

    def test_ctrl_u(self):
        inp = make_input()
        for ch in "hello world":
            inp._handle_key(_key(ch))
        # Move cursor to position 5
        for _ in range(6):
            inp._handle_key(_key("left"))
        assert inp._cursor == 5
        inp._handle_key(KeyEvent(key="u", char="\x15", ctrl=True))
        assert inp._get_text() == " world"
        assert inp._cursor == 0

    def test_ctrl_w(self):
        inp = make_input()
        for ch in "hello world":
            inp._handle_key(_key(ch))
        inp._handle_key(KeyEvent(key="w", char="\x17", ctrl=True))
        assert inp._get_text() == "hello "

    def test_ctrl_w_at_start_no_op(self):
        inp = make_input()
        inp._handle_key(KeyEvent(key="w", char="\x17", ctrl=True))
        assert inp._get_text() == ""

    def test_multiline_esc_enter(self):
        """Escape followed by Enter inserts a newline."""
        inp = make_input()
        inp._handle_key(_key("a"))
        inp._handle_key(_key("escape"))
        inp._handle_key(_key("enter"))
        assert "\n" in inp._get_text()
        assert inp._submitted is False

    def test_shift_enter_inserts_newline(self):
        inp = make_input()
        inp._handle_key(_key("a"))
        inp._handle_key(KeyEvent(key="enter", char="", shift=True))

        assert inp._get_text() == "a\n"
        assert inp._submitted is False

    def test_shift_enter_with_suggestions_inserts_newline_without_accepting(self):
        from unittest.mock import MagicMock

        aggregator = MagicMock()
        aggregator.suggestions = [MagicMock()]
        aggregator.accept_selected.return_value = ("/model ", 0, 4)
        inp = make_input(suggestion_aggregator=aggregator)

        inp._handle_key(KeyEvent(key="enter", char="", shift=True))

        assert inp._get_text() == "\n"
        assert inp._submitted is False
        aggregator.accept_selected.assert_not_called()

    def test_enter_submits(self):
        inp = make_input()
        for ch in "hello":
            inp._handle_key(_key(ch))
        inp._handle_key(_key("enter"))
        assert inp._submitted is True

    def test_ctrl_c_clears_buffer_when_non_empty(self):
        inp = make_input()
        for ch in "hello":
            inp._handle_key(_key(ch))
        inp._handle_key(KeyEvent(key="c", char="\x03", ctrl=True))
        assert inp._get_text() == ""
        assert inp._cursor == 0
        assert inp._cancelled is False

    def test_ctrl_c_cancels_when_empty(self):
        inp = make_input()
        inp._handle_key(KeyEvent(key="c", char="\x03", ctrl=True))
        assert inp._cancelled is True

    def test_ctrl_d_ignored(self):
        inp = make_input()
        inp._handle_key(KeyEvent(key="d", char="\x04", ctrl=True))
        assert inp._cancelled is False

    def test_insert_at_cursor(self):
        """Characters typed mid-buffer are inserted at cursor position."""
        inp = make_input()
        for ch in "ac":
            inp._handle_key(_key(ch))
        inp._handle_key(_key("left"))  # cursor between a and c
        inp._handle_key(_key("b"))
        assert inp._get_text() == "abc"
        assert inp._cursor == 2

    def test_history_navigate_up(self, tmp_path):
        """Up arrow navigates to previous history entry when no suggestions."""
        from iac_code.ui.core.input_history import InputHistory

        hf = str(tmp_path / "hist.txt")
        history = InputHistory(hf)
        history.append("prev command")

        inp = make_input(history=history)
        inp._handle_key(_key("up"))
        assert inp._get_text() == "prev command"

    def test_history_navigate_multiline_entry_sets_full_buffer(self, tmp_path):
        from iac_code.ui.core.input_history import InputHistory

        history = InputHistory(str(tmp_path / "hist.txt"))
        history.append("line1\nline2")
        inp = make_input(history=history)

        inp._handle_key(_key("up"))

        assert inp._get_text() == "line1\nline2"

    def test_history_navigate_down(self, tmp_path):
        """Down arrow after navigating up restores None (clears buffer signal)."""
        from iac_code.ui.core.input_history import InputHistory

        hf = str(tmp_path / "hist.txt")
        history = InputHistory(hf)
        history.append("prev command")

        inp = make_input(history=history)
        inp._handle_key(_key("up"))
        # Down past newest → None → clear buffer
        inp._handle_key(_key("down"))
        assert inp._get_text() == ""

    def test_suggestions_up_down_moves_selection(self):
        """Up/Down with active suggestions delegates to aggregator.move_selection."""
        from unittest.mock import MagicMock

        aggregator = MagicMock()
        aggregator.suggestions = [MagicMock()]  # non-empty
        aggregator.accept_selected.return_value = None

        inp = make_input(suggestion_aggregator=aggregator)
        inp._handle_key(_key("up"))
        aggregator.move_selection.assert_called_with(-1)

        inp._handle_key(_key("down"))
        aggregator.move_selection.assert_called_with(1)

    def test_tab_accepts_ghost_text(self):
        """Tab calls aggregator.accept_ghost_text and applies the completion."""
        from unittest.mock import MagicMock

        aggregator = MagicMock()
        aggregator.suggestions = [MagicMock()]
        aggregator.accept_ghost_text.return_value = ("/model ", 0, 4)

        inp = make_input(suggestion_aggregator=aggregator)
        for ch in "/mod":
            inp._handle_key(_key(ch))
        inp._handle_key(_key("tab"))
        aggregator.accept_ghost_text.assert_called_once()
        assert inp._get_text() == "/model "

    def test_enter_accepts_selected_suggestion_and_submits(self):
        """Enter with active suggestions accepts and submits immediately."""
        from unittest.mock import MagicMock

        aggregator = MagicMock()
        aggregator.suggestions = [MagicMock()]
        aggregator.accept_selected.return_value = ("/model ", 0, 4)

        inp = make_input(suggestion_aggregator=aggregator)
        for ch in "/mod":
            inp._handle_key(_key(ch))
        inp._handle_key(_key("enter"))
        aggregator.accept_selected.assert_called_once()
        assert inp._submitted is True
        assert inp._get_text() == "/model "

    def test_paste_inserts_multiline_text(self):
        """Bracket paste event inserts all content (including newlines) into buffer."""
        inp = make_input()
        inp._handle_key(KeyEvent(key="paste", char="line1\nline2\nline3"))
        assert inp._get_text() == "line1\nline2\nline3"
        assert inp._submitted is False

    def test_paste_appends_to_existing_text(self):
        """Paste appends at cursor position within existing text."""
        inp = make_input()
        for ch in "hello ":
            inp._handle_key(_key(ch))
        inp._handle_key(KeyEvent(key="paste", char="world\nfoo"))
        assert inp._get_text() == "hello world\nfoo"

    def test_escape_alone_resolves_through_keybinding_manager(self):
        """Standalone Escape (not followed by Enter) goes through KeybindingManager."""
        handled = []
        km = KeybindingManager()
        from iac_code.ui.keybindings.manager import KeyBinding

        km.push_context("global")
        km.register(
            KeyBinding(
                key="escape",
                action="test_escape",
                context="global",
                handler=lambda: handled.append(True) or True,
            )
        )
        inp = PromptInput(keybinding_manager=km)
        inp._handle_key(_key("escape"))
        assert handled == [True]

    def test_escape_with_suggestions_dismisses_aggregator(self):
        aggregator = DummyAggregator()
        aggregator.suggestions = [SimpleNamespace()]
        inp = make_input(suggestion_aggregator=aggregator)

        inp._handle_key(_key("escape"))

        assert aggregator.dismissed is True


class TestPromptInputInsertText:
    def test_insert_text_at_cursor(self):
        inp = make_input()
        for ch in "hello":
            inp._handle_key(_key(ch))
        inp._handle_key(_key("left"))
        inp._handle_key(_key("left"))

        inp.insert_text(" brave")

        assert inp._get_text() == "hel bravelo"
        assert inp._cursor == len("hel brave")
        assert inp._text_changed is True

    def test_insert_empty_text_does_not_mark_changed(self):
        inp = make_input()
        inp._handle_key(_key("h"))
        inp._text_changed = False

        inp.insert_text("")

        assert inp._get_text() == "h"
        assert inp._cursor == 1
        assert inp._text_changed is False


class TestPromptInputHelpers:
    def test_update_suggestions_sync_uses_current_buffer_and_cursor(self):
        aggregator = DummyAggregator()
        inp = make_input(suggestion_aggregator=aggregator)
        inp._set_text("hello")
        inp._cursor = 3

        inp._update_suggestions_sync()

        assert aggregator.updated == [("hello", 3)]

    def test_update_suggestions_sync_without_aggregator_is_noop(self):
        inp = make_input()
        inp._set_text("hello")
        inp._update_suggestions_sync()
        assert inp._get_text() == "hello"


class TestPromptInputRender:
    def test_render_single_line_with_ghost_and_suggestions(self, monkeypatch):
        import iac_code.ui.core.prompt_input as prompt_mod

        aggregator = DummyAggregator()
        aggregator.ghost_text = " world"
        aggregator.suggestions = [
            SimpleNamespace(display_text="hello", description="greeting"),
            SimpleNamespace(display_text="help", description="command"),
        ]
        aggregator.visible_suggestions = aggregator.suggestions
        aggregator.visible_selected_index = 1
        aggregator.has_more_below = True

        out = StringIO()
        monkeypatch.setattr(prompt_mod, "sys", SimpleNamespace(stdout=out))
        monkeypatch.setattr(
            prompt_mod.shutil,
            "get_terminal_size",
            lambda *args, **kwargs: os.terminal_size((80, 24)),
        )

        inp = make_input(suggestion_aggregator=aggregator)
        inp._prompt = "❯ "
        inp._set_text("hel")
        inp._cursor = 3
        inp._render()

        output = out.getvalue()
        assert "hel" in output
        assert "world" in output
        assert "hello" in output
        assert "help" in output
        assert "Enter" in output
        assert inp._prev_suggestion_lines == 3

    def test_render_multiline_disables_ghost_text(self, monkeypatch):
        import iac_code.ui.core.prompt_input as prompt_mod

        aggregator = DummyAggregator()
        aggregator.ghost_text = " should-not-render"
        out = StringIO()
        monkeypatch.setattr(prompt_mod, "sys", SimpleNamespace(stdout=out))
        monkeypatch.setattr(
            prompt_mod.shutil,
            "get_terminal_size",
            lambda *args, **kwargs: os.terminal_size((80, 24)),
        )

        inp = make_input(suggestion_aggregator=aggregator)
        inp._prompt = "❯ "
        inp._set_text("line1\nline2")
        inp._cursor = len(inp._get_text())
        inp._render()

        output = out.getvalue()
        assert "line1" in output
        assert "line2" in output
        assert "should-not-render" not in output
        assert inp._prev_content_extra_lines == 1

    def test_clear_suggestions_erases_previous_overlay(self, monkeypatch):
        import iac_code.ui.core.prompt_input as prompt_mod

        out = StringIO()
        monkeypatch.setattr(prompt_mod, "sys", SimpleNamespace(stdout=out))

        inp = make_input()
        inp._prev_content_extra_lines = 1
        inp._prev_suggestion_lines = 2
        inp._clear_suggestions()

        output = out.getvalue()
        assert "\033[s" in output
        assert "\033[u" in output
        assert inp._prev_suggestion_lines == 0


class TestPromptInputLoop:
    def test_input_loop_submits_text_and_updates_suggestions(self, monkeypatch):
        import iac_code.ui.core.prompt_input as prompt_mod

        events = iter([_key("h"), _key("i"), _key("enter")])

        class FakeCapture:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read_key(self):
                return next(events, None)

        out = StringIO()
        aggregator = DummyAggregator()
        monkeypatch.setattr(prompt_mod, "sys", SimpleNamespace(stdout=out))
        monkeypatch.setattr(
            prompt_mod.shutil,
            "get_terminal_size",
            lambda *args, **kwargs: os.terminal_size((40, 24)),
        )
        monkeypatch.setattr("iac_code.ui.core.raw_input.RawInputCapture", FakeCapture)

        inp = make_input(suggestion_aggregator=aggregator)
        result = inp._input_loop("❯ ")

        assert result == "hi"
        assert aggregator.updated == [("h", 1), ("hi", 2)]
        assert out.getvalue().endswith("\n")

    def test_input_loop_returns_none_on_ctrl_c_with_empty_buffer(self, monkeypatch):
        import iac_code.ui.core.prompt_input as prompt_mod

        events = iter([KeyEvent(key="c", char="\x03", ctrl=True)])

        class FakeCapture:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read_key(self):
                return next(events, None)

        out = StringIO()
        monkeypatch.setattr(prompt_mod, "sys", SimpleNamespace(stdout=out))
        monkeypatch.setattr(
            prompt_mod.shutil,
            "get_terminal_size",
            lambda *args, **kwargs: os.terminal_size((40, 24)),
        )
        monkeypatch.setattr("iac_code.ui.core.raw_input.RawInputCapture", FakeCapture)

        inp = make_input()
        assert inp._input_loop("❯ ") is None

    def test_input_loop_returns_none_on_keyboard_interrupt_with_empty_buffer(self, monkeypatch):
        import iac_code.ui.core.prompt_input as prompt_mod

        class FakeCapture:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read_key(self):
                raise KeyboardInterrupt

        out = StringIO()
        monkeypatch.setattr(prompt_mod, "sys", SimpleNamespace(stdout=out))
        monkeypatch.setattr(
            prompt_mod.shutil,
            "get_terminal_size",
            lambda *args, **kwargs: os.terminal_size((40, 24)),
        )
        monkeypatch.setattr("iac_code.ui.core.raw_input.RawInputCapture", FakeCapture)

        inp = make_input()
        assert inp._input_loop("❯ ") is None

    def test_input_loop_keyboard_interrupt_clears_non_empty_buffer_first(self, monkeypatch):
        import iac_code.ui.core.prompt_input as prompt_mod

        events = iter([_key("a"), KeyboardInterrupt(), _key("enter")])

        class FakeCapture:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read_key(self):
                event = next(events)
                if isinstance(event, KeyboardInterrupt):
                    raise event
                return event

        out = StringIO()
        monkeypatch.setattr(prompt_mod, "sys", SimpleNamespace(stdout=out))
        monkeypatch.setattr(
            prompt_mod.shutil,
            "get_terminal_size",
            lambda *args, **kwargs: os.terminal_size((40, 24)),
        )
        monkeypatch.setattr("iac_code.ui.core.raw_input.RawInputCapture", FakeCapture)

        inp = make_input()
        assert inp._input_loop("❯ ") == ""

    def test_input_loop_runs_pending_action_outside_raw_mode(self, monkeypatch):
        import iac_code.ui.core.prompt_input as prompt_mod

        action_calls = []
        events = iter([_key("a"), _key("b")])
        state = {"scheduled": False}

        class FakeCapture:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read_key(self):
                try:
                    event = next(events)
                except StopIteration:
                    if not state["scheduled"] and inp._pending_action is None:
                        state["scheduled"] = True
                        inp.schedule_action(lambda: action_calls.append("ran"))
                    else:
                        inp._submitted = True
                    return None
                return event

        out = StringIO()
        monkeypatch.setattr(prompt_mod, "sys", SimpleNamespace(stdout=out))
        monkeypatch.setattr(
            prompt_mod.shutil,
            "get_terminal_size",
            lambda *args, **kwargs: os.terminal_size((40, 24)),
        )
        monkeypatch.setattr("iac_code.ui.core.raw_input.RawInputCapture", FakeCapture)

        inp = make_input()
        result = inp._input_loop("❯ ")

        assert result == "ab"
        assert action_calls == ["ran"]


# ---------------------------------------------------------------------------
# OSC 8 hyperlink tests for [Image #N]
# ---------------------------------------------------------------------------


class TestImageRefOSC8Hyperlink:
    """Tests for OSC 8 hyperlink rendering on [Image #N] references."""

    def test_image_ref_renders_osc8_hyperlink(self):
        """[Image #N] should be wrapped in OSC 8 hyperlink when image_store has path."""
        from unittest.mock import MagicMock

        store = MagicMock()
        store.get_path.return_value = "/tmp/test-image.png"

        pi = make_input(image_store=store)
        pi._pasted_contents[1] = MagicMock()

        line = "Look at [Image #1] here"
        result = pi._highlight_image_refs(line)

        # Verify OSC 8 hyperlink
        assert "\033]8;;file:///tmp/test-image.png\033\\" in result
        assert "[Image #1]" in result
        assert "\033]8;;\033\\" in result  # closing sequence

    def test_image_ref_fallback_without_store(self):
        """[Image #N] should only have color when image_store is None."""
        from unittest.mock import MagicMock

        pi = make_input(image_store=None)
        pi._pasted_contents[1] = MagicMock()

        line = "[Image #1]"
        result = pi._highlight_image_refs(line)

        assert "\033[36m" in result  # cyan
        assert "\033]8;;" not in result  # no OSC 8

    def test_image_ref_fallback_without_path(self):
        """[Image #N] should only have color when store has no path for that ID."""
        from unittest.mock import MagicMock

        store = MagicMock()
        store.get_path.return_value = None  # No path cached

        pi = make_input(image_store=store)
        pi._pasted_contents[1] = MagicMock()

        line = "[Image #1]"
        result = pi._highlight_image_refs(line)

        assert "\033[36m" in result
        assert "\033]8;;" not in result
