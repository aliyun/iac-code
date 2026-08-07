"""Tests for terminal image protocol detection and encoding."""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from PIL import Image
from rich.console import Console

from iac_code.ui.terminal_image import (
    TerminalImageProtocol,
    build_terminal_image_escape,
    detect_terminal_image_protocol,
    load_terminal_logo_png,
)


def _console(*, terminal: bool = True, width: int = 120) -> Console:
    return Console(file=io.StringIO(), force_terminal=terminal, width=width, _environ={})


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({"TERM": "xterm-kitty"}, TerminalImageProtocol.KITTY),
        ({"KITTY_WINDOW_ID": "1"}, TerminalImageProtocol.KITTY),
        ({"KITTY_PID": "123"}, TerminalImageProtocol.KITTY),
        ({"TERM_PROGRAM": "Kitty"}, TerminalImageProtocol.KITTY),
        ({"TERM": "xterm-ghostty"}, TerminalImageProtocol.KITTY),
        ({"GHOSTTY_RESOURCES_DIR": "/Applications/Ghostty.app/Contents/Resources"}, TerminalImageProtocol.KITTY),
        ({"TERM_PROGRAM": "Ghostty"}, TerminalImageProtocol.KITTY),
        ({"TERM_PROGRAM": "WezTerm"}, TerminalImageProtocol.KITTY),
        ({"WEZTERM_PANE": "1"}, TerminalImageProtocol.KITTY),
        ({"WEZTERM_UNIX_SOCKET": "/tmp/wezterm.sock"}, TerminalImageProtocol.KITTY),
        ({"LC_TERMINAL": "WezTerm"}, TerminalImageProtocol.KITTY),
        ({"TERM_PROGRAM": "WarpTerminal"}, TerminalImageProtocol.KITTY),
        ({"TERM_PROGRAM": "Rio"}, TerminalImageProtocol.KITTY),
        ({"TERM": "rio"}, TerminalImageProtocol.KITTY),
        ({"KONSOLE_VERSION": "240802"}, TerminalImageProtocol.KITTY),
        ({"TERM_PROGRAM": "iTerm.app"}, TerminalImageProtocol.ITERM2),
        ({"LC_TERMINAL": "iTerm2"}, TerminalImageProtocol.ITERM2),
        ({"TERM": "mintty"}, TerminalImageProtocol.ITERM2),
        ({"TERM_PROGRAM": "mintty"}, TerminalImageProtocol.ITERM2),
        ({"WT_SESSION": "58d56b0f"}, TerminalImageProtocol.SIXEL),
        ({"TERMINAL_NAME": "contour"}, TerminalImageProtocol.SIXEL),
        ({"TERM_PROGRAM": "contour"}, TerminalImageProtocol.SIXEL),
        ({"TERM": "foot"}, TerminalImageProtocol.SIXEL),
        ({"TERM": "foot-extra"}, TerminalImageProtocol.SIXEL),
        ({"TERM": "mlterm-256color"}, TerminalImageProtocol.SIXEL),
        ({"MLTERM": "3.9.3"}, TerminalImageProtocol.SIXEL),
        ({"TERM": "xterm-sixel"}, TerminalImageProtocol.SIXEL),
    ],
)
def test_detects_supported_protocols_from_terminal_environment(
    environment: dict[str, str],
    expected: TerminalImageProtocol,
):
    console = _console()
    assert detect_terminal_image_protocol(console, environment) is expected


@pytest.mark.parametrize(
    "environment",
    [
        {"TERM_PROGRAM": "Apple_Terminal"},
        {"TERM_PROGRAM": "Alacritty"},
        {"TERM_PROGRAM": "vscode", "TERM_PROGRAM_VERSION": "1.110.0"},
        {"TERM": "xterm-256color"},
        {"TERM": "xterm-256color", "VTE_VERSION": "7800"},
        {"TERM_PROGRAM": "Hyper"},
        {"TERM_PROGRAM": "Tabby"},
        {"KONSOLE_VERSION": "220303"},
        {"KONSOLE_VERSION": "not-a-version"},
    ],
)
def test_detection_uses_braille_when_image_capability_cannot_be_confirmed(environment: dict[str, str]):
    assert detect_terminal_image_protocol(_console(), environment) is None


def test_detection_uses_braille_for_redirects_narrow_terminals_and_multiplexers():
    assert detect_terminal_image_protocol(_console(terminal=False), {"TERM": "xterm-kitty"}) is None
    assert detect_terminal_image_protocol(_console(width=40), {"TERM": "xterm-kitty"}) is None
    assert detect_terminal_image_protocol(_console(), {"TERM": "xterm-kitty", "TMUX": "/tmp/tmux"}) is None
    assert detect_terminal_image_protocol(_console(), {"TERM": "screen-256color", "WEZTERM_PANE": "1"}) is None


def test_detection_supports_explicit_override_and_disable():
    console = _console()
    assert (
        detect_terminal_image_protocol(console, {"IAC_CODE_TERMINAL_IMAGE_PROTOCOL": "sixel"})
        is TerminalImageProtocol.SIXEL
    )
    assert detect_terminal_image_protocol(console, {"IAC_CODE_TERMINAL_IMAGE_PROTOCOL": "braille"}) is None
    assert detect_terminal_image_protocol(console, {"IAC_CODE_TERMINAL_IMAGE_PROTOCOL": "unknown"}) is None


def test_explicit_override_can_enable_images_for_conservative_or_multiplexed_terminals():
    console = _console()
    assert (
        detect_terminal_image_protocol(
            console,
            {"TERM_PROGRAM": "vscode", "IAC_CODE_TERMINAL_IMAGE_PROTOCOL": "iterm2"},
        )
        is TerminalImageProtocol.ITERM2
    )
    assert (
        detect_terminal_image_protocol(
            console,
            {"TERM": "screen-256color", "TMUX": "/tmp/tmux", "IAC_CODE_TERMINAL_IMAGE_PROTOCOL": "kitty"},
        )
        is TerminalImageProtocol.KITTY
    )


def test_bundled_terminal_logo_is_transparent_png():
    data = load_terminal_logo_png()
    image = Image.open(io.BytesIO(data))
    assert image.format == "PNG"
    assert image.mode == "RGBA"
    assert image.size == (512, 512)
    assert image.getpixel((0, 0))[3] == 0
    assert image.getchannel("A").getbbox() is not None


def test_kitty_encoding_is_chunked_and_keeps_cursor_stationary():
    data = load_terminal_logo_png()
    encoded = build_terminal_image_escape(TerminalImageProtocol.KITTY, data)
    assert encoded.startswith("\033_Ga=T,f=100,c=14,r=7,C=1,q=2,m=1;")
    assert encoded.endswith("\033\\")
    assert encoded.count("\033_G") > 1
    assert "\033_Gm=0;" in encoded


def test_iterm2_encoding_uses_cell_dimensions_and_inline_png():
    data = load_terminal_logo_png()
    encoded = build_terminal_image_escape(TerminalImageProtocol.ITERM2, data)
    assert encoded.startswith("\033]1337;File=")
    assert "inline=1" in encoded
    assert "width=14;height=7" in encoded
    assert "preserveAspectRatio=1" in encoded
    assert encoded.endswith("\a")


def test_sixel_encoding_uses_transparency_palette_and_requested_cell_size():
    data = load_terminal_logo_png()
    encoded = build_terminal_image_escape(
        TerminalImageProtocol.SIXEL,
        data,
        columns=4,
        rows=3,
        cell_size=(2, 4),
    )
    assert encoded.startswith("\033P0;1;0q")
    assert '"1;1;8;12' in encoded
    assert "#0;2;" in encoded
    assert encoded.endswith("\033\\")


def test_terminal_cell_size_falls_back_for_stream_without_fileno():
    from iac_code.ui.terminal_image import terminal_cell_size

    console = SimpleNamespace(file=io.StringIO())
    assert terminal_cell_size(console) == (8, 16)
