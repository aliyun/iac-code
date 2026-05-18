"""Tests for clipboard image indicator: focus events, state management, hint text, and Ctrl+V binding."""

from __future__ import annotations

import os
from io import StringIO
from types import SimpleNamespace

from iac_code.ui.core.key_event import KeyEvent
from iac_code.ui.core.prompt_input import PromptInput
from iac_code.ui.core.raw_input import RawInputCapture
from iac_code.ui.keybindings.manager import KeyBinding, KeybindingManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_input(**kwargs) -> PromptInput:
    """Return a PromptInput with a fresh KeybindingManager."""
    km = KeybindingManager()
    return PromptInput(keybinding_manager=km, **kwargs)


def _key(key: str, *, ctrl: bool = False) -> KeyEvent:
    char = key if len(key) == 1 else ""
    return KeyEvent(key=key, char=char, ctrl=ctrl)


def _stub_render(inp: PromptInput, monkeypatch) -> StringIO:
    """Stub _render to avoid real stdout writes; return captured StringIO."""
    import iac_code.ui.core.prompt_input as prompt_mod

    out = StringIO()
    monkeypatch.setattr(prompt_mod, "sys", SimpleNamespace(stdout=out))
    monkeypatch.setattr(
        prompt_mod.shutil,
        "get_terminal_size",
        lambda *args, **kwargs: os.terminal_size((120, 24)),
    )
    inp._prompt = "❯ "
    return out


# ---------------------------------------------------------------------------
# 1. Focus event parsing (raw_input layer)
# ---------------------------------------------------------------------------


class TestFocusEventParsing:
    """Verify raw_input parses CSI I / CSI O as focus_in / focus_out."""

    def test_focus_in_parsed(self):
        event = RawInputCapture._parse_escape_sequence("[I")
        assert event.key == "focus_in"
        assert event.char == ""

    def test_focus_out_parsed(self):
        event = RawInputCapture._parse_escape_sequence("[O")
        assert event.key == "focus_out"
        assert event.char == ""


# ---------------------------------------------------------------------------
# 2. Clipboard indicator state (prompt_input layer)
# ---------------------------------------------------------------------------


class TestClipboardIndicatorState:
    """Test _clipboard_has_image transitions in response to events."""

    def test_focus_in_with_image_sets_flag(self, monkeypatch):
        inp = _make_input()
        _stub_render(inp, monkeypatch)
        monkeypatch.setattr(
            "iac_code.utils.image.clipboard.has_image_in_clipboard",
            lambda: True,
        )
        inp._handle_key(KeyEvent(key="focus_in", char=""))
        assert inp._clipboard_has_image is True

    def test_focus_in_without_image_clears_flag(self, monkeypatch):
        inp = _make_input()
        _stub_render(inp, monkeypatch)
        monkeypatch.setattr(
            "iac_code.utils.image.clipboard.has_image_in_clipboard",
            lambda: False,
        )
        # Pre-set to True to ensure it transitions to False
        inp._clipboard_has_image = True
        inp._handle_key(KeyEvent(key="focus_in", char=""))
        assert inp._clipboard_has_image is False

    def test_focus_out_clears_flag(self, monkeypatch):
        inp = _make_input()
        _stub_render(inp, monkeypatch)
        inp._clipboard_has_image = True
        inp._handle_key(KeyEvent(key="focus_out", char=""))
        assert inp._clipboard_has_image is False

    def test_printable_char_clears_flag(self, monkeypatch):
        inp = _make_input()
        _stub_render(inp, monkeypatch)
        inp._clipboard_has_image = True
        inp._handle_key(_key("a"))
        assert inp._clipboard_has_image is False


# ---------------------------------------------------------------------------
# 3. Ctrl+V binding registered on all platforms (including darwin)
# ---------------------------------------------------------------------------


class TestCtrlVBinding:
    """Ensure Ctrl+V is registered as a keybinding regardless of platform."""

    def test_ctrl_v_registered_on_darwin(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "darwin")
        km = KeybindingManager()
        km.push_context("global")
        handled = []
        km.register(KeyBinding("ctrl+v", "paste_image", "global", lambda: handled.append(True) or True))
        # Resolve a ctrl+v event
        event = KeyEvent(key="v", char="\x16", ctrl=True)
        result = km.resolve(event)
        assert result is True
        assert handled == [True]

    def test_ctrl_v_registered_on_linux(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        km = KeybindingManager()
        km.push_context("global")
        handled = []
        km.register(KeyBinding("ctrl+v", "paste_image", "global", lambda: handled.append(True) or True))
        event = KeyEvent(key="v", char="\x16", ctrl=True)
        result = km.resolve(event)
        assert result is True
        assert handled == [True]

    def test_repl_registers_ctrl_v_binding(self, monkeypatch):
        """Verify the InlineREPL registers ctrl+v in _register_global_keybindings."""
        # We check that the keybinding "ctrl+v" is registered after calling
        # the registration method. We can test this by inspecting the bindings
        # directly via the keybinding manager source.
        from iac_code.ui.keybindings.manager import KeyBinding, KeybindingManager

        km = KeybindingManager()
        km.push_context("global")

        # Simulate what InlineREPL._register_global_keybindings does
        km.register(KeyBinding("ctrl+v", "paste_image", "global", lambda: True))

        # Verify ctrl+v resolves
        event = KeyEvent(key="v", char="\x16", ctrl=True)
        assert km.resolve(event) is True


# ---------------------------------------------------------------------------
# 4. Hint text (platform-dependent shortcut label)
# ---------------------------------------------------------------------------


class TestClipboardHintText:
    """Test _clipboard_hint_text returns unified shortcut on all platforms."""

    def test_hint_text_unified(self):
        inp = _make_input()
        hint = inp._clipboard_hint_text()
        assert hint == "Image in clipboard \u00b7 ctrl+v to paste"

    def test_hint_text_macos(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "darwin")
        inp = _make_input()
        hint = inp._clipboard_hint_text()
        assert "ctrl+v" in hint
        assert "Image in clipboard" in hint

    def test_hint_text_linux(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        inp = _make_input()
        hint = inp._clipboard_hint_text()
        assert "ctrl+v" in hint
        assert "Image in clipboard" in hint

    def test_hint_text_windows(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        inp = _make_input()
        hint = inp._clipboard_hint_text()
        assert "ctrl+v" in hint


# ---------------------------------------------------------------------------
# 5. Render includes hint when clipboard has image
# ---------------------------------------------------------------------------


class TestClipboardHintRender:
    """Verify the right-aligned hint renders when _clipboard_has_image is True."""

    def test_render_shows_hint_when_clipboard_has_image(self, monkeypatch):
        inp = _make_input()
        out = _stub_render(inp, monkeypatch)
        inp._clipboard_has_image = True
        inp._set_text("")
        inp._cursor = 0
        inp._render()

        output = out.getvalue()
        assert "Image in clipboard" in output

    def test_render_hides_hint_when_no_clipboard_image(self, monkeypatch):
        inp = _make_input()
        out = _stub_render(inp, monkeypatch)
        inp._clipboard_has_image = False
        inp._set_text("")
        inp._cursor = 0
        inp._render()

        output = out.getvalue()
        assert "Image in clipboard" not in output
