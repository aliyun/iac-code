"""Inline terminal image protocol support for the REPL banner."""

from __future__ import annotations

import base64
import io
import os
import struct
from collections.abc import Iterable, Mapping
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, cast

from PIL import Image

if TYPE_CHECKING:
    from rich.console import Console

IMAGE_COLUMNS = 14
IMAGE_ROWS = 7
MIN_TERMINAL_COLUMNS = 60
_KITTY_CHUNK_SIZE = 4096
_DEFAULT_CELL_SIZE = (8, 16)


class TerminalImageProtocol(str, Enum):
    """Terminal protocols supported by the high-resolution banner logo."""

    KITTY = "kitty"
    ITERM2 = "iterm2"
    SIXEL = "sixel"


def detect_terminal_image_protocol(
    console: Console,
    environ: Mapping[str, str] | None = None,
) -> TerminalImageProtocol | None:
    """Detect a safe inline-image protocol, or return None for Braille."""
    env = os.environ if environ is None else environ
    requested = env.get("IAC_CODE_TERMINAL_IMAGE_PROTOCOL", "auto").strip().lower()

    if requested in {"none", "off", "braille"}:
        return None
    if not bool(getattr(console, "is_terminal", False)) or int(getattr(console, "width", 0)) < MIN_TERMINAL_COLUMNS:
        return None
    if requested != "auto":
        try:
            return TerminalImageProtocol(requested)
        except ValueError:
            return None

    # Multiplexers require protocol-specific passthrough and can silently eat
    # image payloads. Keep the dependable Braille fallback unless explicitly
    # overridden by the user.
    term = env.get("TERM", "").lower()
    if env.get("TMUX") or term.startswith("screen"):
        return None

    term_program = env.get("TERM_PROGRAM", "").strip().lower()
    lc_terminal = env.get("LC_TERMINAL", "").strip().lower()
    terminal_name = env.get("TERMINAL_NAME", "").strip().lower()

    # Prefer Kitty where a terminal supports more than one graphics protocol.
    # It preserves transparency and cell-based placement without converting the
    # source PNG. These environment markers are set by the terminal itself.
    if env.get("KITTY_WINDOW_ID") or env.get("KITTY_PID") or term == "xterm-kitty" or term_program == "kitty":
        return TerminalImageProtocol.KITTY
    if env.get("GHOSTTY_RESOURCES_DIR") or term == "xterm-ghostty" or term_program == "ghostty":
        return TerminalImageProtocol.KITTY
    if (
        env.get("WEZTERM_PANE")
        or env.get("WEZTERM_UNIX_SOCKET")
        or term_program == "wezterm"
        or lc_terminal == "wezterm"
    ):
        return TerminalImageProtocol.KITTY
    if term_program in {"warpterminal", "rio"} or term == "rio":
        return TerminalImageProtocol.KITTY
    if _konsole_supports_graphics(env.get("KONSOLE_VERSION", "")):
        return TerminalImageProtocol.KITTY

    # iTerm2's OSC 1337 format is also implemented by mintty. Keep iTerm2 on
    # its native protocol even though recent releases additionally understand
    # Kitty graphics.
    if term_program == "iterm.app" or lc_terminal == "iterm2":
        return TerminalImageProtocol.ITERM2
    if term_program == "mintty" or term == "mintty":
        return TerminalImageProtocol.ITERM2

    # These terminals expose reliable identity markers and implement Sixel.
    # Generic xterm/VTE/VS Code terminals are deliberately not inferred: Sixel
    # may be a build-time or user setting there, so explicit override is safer.
    if env.get("WT_SESSION"):
        return TerminalImageProtocol.SIXEL
    if terminal_name == "contour" or term_program == "contour":
        return TerminalImageProtocol.SIXEL
    if term in {"foot", "foot-extra"} or term.startswith("mlterm") or env.get("MLTERM"):
        return TerminalImageProtocol.SIXEL
    if "sixel" in term or env.get("IAC_CODE_SIXEL") == "1":
        return TerminalImageProtocol.SIXEL
    return None


def _konsole_supports_graphics(version: str) -> bool:
    """Return whether Konsole's numeric version includes image protocols."""
    try:
        return int(version) >= 220400
    except (TypeError, ValueError):
        return False


def load_terminal_logo_png() -> bytes:
    """Load the transparent, high-resolution terminal logo."""
    return (Path(__file__).with_name("assets") / "iac-code-terminal-logo.png").read_bytes()


def build_terminal_image_escape(
    protocol: TerminalImageProtocol,
    png_data: bytes,
    *,
    columns: int = IMAGE_COLUMNS,
    rows: int = IMAGE_ROWS,
    cell_size: tuple[int, int] = _DEFAULT_CELL_SIZE,
) -> str:
    """Encode PNG bytes for one supported terminal image protocol."""
    if protocol is TerminalImageProtocol.KITTY:
        return _build_kitty_escape(png_data, columns=columns, rows=rows)
    if protocol is TerminalImageProtocol.ITERM2:
        return _build_iterm2_escape(png_data, columns=columns, rows=rows)
    return _build_sixel_escape(png_data, columns=columns, rows=rows, cell_size=cell_size)


def terminal_cell_size(console: Console) -> tuple[int, int]:
    """Return terminal cell width and height in pixels when available."""
    try:
        fileno = console.file.fileno()
        if os.name != "nt":
            import fcntl
            import termios

            packed = fcntl.ioctl(fileno, termios.TIOCGWINSZ, b"\0" * 8)
            terminal_rows, terminal_columns, pixel_width, pixel_height = struct.unpack("HHHH", packed)
            if terminal_rows and terminal_columns and pixel_width and pixel_height:
                return max(1, pixel_width // terminal_columns), max(1, pixel_height // terminal_rows)
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return _DEFAULT_CELL_SIZE


def _build_kitty_escape(png_data: bytes, *, columns: int, rows: int) -> str:
    payload = base64.b64encode(png_data).decode("ascii")
    chunks = [payload[index : index + _KITTY_CHUNK_SIZE] for index in range(0, len(payload), _KITTY_CHUNK_SIZE)]
    if not chunks:
        return ""

    commands: list[str] = []
    for index, chunk in enumerate(chunks):
        more = 1 if index < len(chunks) - 1 else 0
        if index == 0:
            control = "a=T,f=100,c={},r={},C=1,q=2,m={}".format(columns, rows, more)
        else:
            control = "m={}".format(more)
        commands.append("\033_G{};{}\033\\".format(control, chunk))
    return "".join(commands)


def _build_iterm2_escape(png_data: bytes, *, columns: int, rows: int) -> str:
    payload = base64.b64encode(png_data).decode("ascii")
    name = base64.b64encode(b"iac-code.png").decode("ascii")
    arguments = "name={};size={};inline=1;width={};height={};preserveAspectRatio=1".format(
        name,
        len(png_data),
        columns,
        rows,
    )
    return "\033]1337;File={}:{}\a".format(arguments, payload)


def _build_sixel_escape(
    png_data: bytes,
    *,
    columns: int,
    rows: int,
    cell_size: tuple[int, int],
) -> str:
    cell_width, cell_height = cell_size
    width = max(1, columns * cell_width)
    height = max(1, rows * cell_height)

    source = Image.open(io.BytesIO(png_data)).convert("RGBA")
    source.thumbnail((width, height), Image.Resampling.LANCZOS)
    image = Image.new("RGBA", (width, height))
    image.alpha_composite(source, ((width - source.width) // 2, (height - source.height) // 2))

    alpha = image.getchannel("A")
    rgb = image.convert("RGB")
    quantized = rgb.quantize(colors=24, method=Image.Quantize.MEDIANCUT)
    palette = list(quantized.getpalette() or [])
    indexes = list(cast(Iterable[int], quantized.get_flattened_data()))
    alpha_pixels = list(cast(Iterable[int], alpha.get_flattened_data()))

    active_indexes = sorted(
        {palette_index for palette_index, alpha_value in zip(indexes, alpha_pixels, strict=True) if alpha_value >= 32}
    )
    color_numbers = {palette_index: number for number, palette_index in enumerate(active_indexes)}

    output = ["\033P0;1;0q", '"1;1;{};{}'.format(width, height)]
    for palette_index, color_number in color_numbers.items():
        offset = palette_index * 3
        red, green, blue = palette[offset : offset + 3]
        output.append(
            "#{};2;{};{};{}".format(
                color_number,
                round(red * 100 / 255),
                round(green * 100 / 255),
                round(blue * 100 / 255),
            )
        )

    for top in range(0, height, 6):
        planes: list[str] = []
        for palette_index, color_number in color_numbers.items():
            values: list[str] = []
            for x in range(width):
                bits = 0
                for bit in range(6):
                    y = top + bit
                    offset = y * width + x
                    if y < height and alpha_pixels[offset] >= 32 and indexes[offset] == palette_index:
                        bits |= 1 << bit
                values.append(chr(63 + bits))
            while values and values[-1] == "?":
                values.pop()
            if values:
                planes.append("#{}{}".format(color_number, _sixel_rle(values)))
        output.append("$".join(planes))
        if top + 6 < height:
            output.append("-")

    output.append("\033\\")
    return "".join(output)


def _sixel_rle(values: list[str]) -> str:
    encoded: list[str] = []
    index = 0
    while index < len(values):
        end = index + 1
        while end < len(values) and values[end] == values[index]:
            end += 1
        count = end - index
        if count >= 4:
            encoded.append("!{}{}".format(count, values[index]))
        else:
            encoded.append(values[index] * count)
        index = end
    return "".join(encoded)
