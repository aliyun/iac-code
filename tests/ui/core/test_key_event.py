"""Tests for KeyEvent dataclass."""

import pytest

from iac_code.ui.core.key_event import KeyEvent


class TestKeyEventKeyId:
    def test_printable_char(self):
        event = KeyEvent(key="a", char="a")
        assert event.key_id == "a"

    def test_ctrl_modifier(self):
        event = KeyEvent(key="r", char="\x12", ctrl=True)
        assert event.key_id == "ctrl+r"

    def test_alt_modifier(self):
        event = KeyEvent(key="p", char="p", alt=True)
        assert event.key_id == "alt+p"

    def test_ctrl_alt_modifier(self):
        event = KeyEvent(key="x", char="x", ctrl=True, alt=True)
        assert event.key_id == "ctrl+alt+x"

    def test_special_key_up(self):
        event = KeyEvent(key="up", char="")
        assert event.key_id == "up"

    def test_enter(self):
        event = KeyEvent(key="enter", char="\r")
        assert event.key_id == "enter"

    def test_escape(self):
        event = KeyEvent(key="escape", char="\x1b")
        assert event.key_id == "escape"

    def test_tab(self):
        event = KeyEvent(key="tab", char="\t")
        assert event.key_id == "tab"

    def test_f1(self):
        event = KeyEvent(key="f1", char="")
        assert event.key_id == "f1"

    def test_pagedown(self):
        event = KeyEvent(key="pagedown", char="")
        assert event.key_id == "pagedown"

    def test_shift_not_included_for_printable(self):
        # shift=True for uppercase, but shift prefix is NOT added for printable chars
        event = KeyEvent(key="A", char="A", shift=True)
        assert event.key_id == "A"

    def test_frozen_dataclass(self):
        event = KeyEvent(key="a", char="a")
        with pytest.raises((AttributeError, TypeError)):
            event.key = "b"  # type: ignore

    def test_default_modifiers_false(self):
        event = KeyEvent(key="a", char="a")
        assert event.ctrl is False
        assert event.alt is False
        assert event.shift is False
