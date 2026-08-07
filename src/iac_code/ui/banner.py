"""Welcome banner rendering."""

from __future__ import annotations

import getpass
import shlex
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from rich.align import Align
from rich.color import Color
from rich.console import Group
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

from iac_code.i18n import _

if TYPE_CHECKING:
    from rich.console import Console

    from iac_code.services.update_checker import PendingUpdate

# The app icon uses a broad stroke that turns into several parallel dot rows
# when encoded directly as Braille. This terminal-specific trace uses a
# single-dot stroke. Its outer modules are exact quarter-turns of one master,
# so the right corners join cleanly without isolated or protruding dots.
BRAILLE_LOGO_LINES = (
    " ⢀⠴⠒⠒⠒⠂⡠⠒⠒⠒⠦⡀",
    "⢰⠃   ⡠⠊     ⠘⡆",
    "⢸   ⠊ ⢠  ⠑⢄  ⡇",
    "⢀⠑⢄  ⢈⢾⡮⠤  ⠑⢄⠁",
    "⢸  ⠑⢄ ⠘⠈ ⡠   ⡇",
    "⠸⡄     ⡠⠊   ⢠⠇",
    " ⠈⠲⠤⠤⠤⠊⠠⠤⠤⠤⠖⠁",
)

BRAND_GRADIENT = (
    "#45b4ff",
    "#4388ff",
    "#5968f5",
    "#7d63f4",
    "#a969ef",
    "#e778cb",
)

ACCENT = "bright_cyan"


def _logo_color(row: int, column: int) -> Color:
    """Pick the nearest brand-gradient stop for a terminal logo cell."""
    width = max(len(line) for line in BRAILLE_LOGO_LINES)
    extent = len(BRAILLE_LOGO_LINES) + width - 2
    position = (row + column) / extent
    index = round(position * (len(BRAND_GRADIENT) - 1))
    return Color.parse(BRAND_GRADIENT[index])


def _render_pixel_logo() -> Text:
    """Render the compact Braille fallback logo."""
    logo = Text()
    width = max(len(line) for line in BRAILLE_LOGO_LINES)

    for row, line in enumerate(BRAILLE_LOGO_LINES):
        if row:
            logo.append("\n")
        logo.append("   ")
        for column, cell in enumerate(line.ljust(width)):
            if cell != " ":
                logo.append(cell, Style(color=_logo_color(row, column)))
            else:
                logo.append(" ")

    return logo


def _render_image_placeholder() -> Text:
    """Reserve the same terminal cells used by the Braille fallback."""
    width = max(len(line) for line in BRAILLE_LOGO_LINES) + 3
    return Text("\n".join(" " * width for _ in BRAILLE_LOGO_LINES))


def _format_update_command(command: Iterable[str]) -> str:
    return shlex.join(tuple(command))


def render_update_prompt_header(update: PendingUpdate) -> Group:
    """Render update information above the interactive update prompt."""
    command_text = _format_update_command(update.update_command)
    items = [
        Text(_("Update available! {} -> {}").format(update.current_version, update.version), style="bold bright_cyan"),
        Text("{}: {}".format(_("Update command"), command_text), style="bold"),
    ]
    if update.release_notes_url:
        items.append(Text("{}: {}".format(_("Release notes"), update.release_notes_url), style="dim"))
    return Group(*items)


def render_update_notice(update: PendingUpdate) -> Panel:
    """Render a notice for an update the user previously skipped."""
    command_text = _format_update_command(update.update_command)
    items = [
        Text(_("Update available! {} -> {}").format(update.current_version, update.version), style="bold bright_cyan"),
        Text(_("Run {} to update.").format(command_text)),
    ]
    if update.release_notes_url:
        items.append(Text("{}: {}".format(_("Release notes"), update.release_notes_url), style="dim"))
    return Panel(Group(*items), border_style=ACCENT, expand=True)


def _get_provider_display() -> str:
    """Get the active provider display name from settings."""
    try:
        from iac_code.config import PARTNER_SOURCES, get_active_provider_key, get_llm_source, get_provider_config
        from iac_code.i18n import _
        from iac_code.providers.registry import PROVIDER_REGISTRY

        key = get_active_provider_key()
        if not key:
            llm_source = get_llm_source()
            for ps in PARTNER_SOURCES:
                if ps.key == llm_source:
                    real_provider = ps.get_provider_display()
                    if real_provider:
                        return "{} / {}".format(ps.display_name, real_provider)
                    return ps.display_name
            return ""
        desc = PROVIDER_REGISTRY.get(key)
        if desc:
            return _(desc.display_name)
        name = get_provider_config(key).get("name", "")
        return name
    except Exception:
        return ""


def render_welcome_banner(
    model: str,
    cwd: str,
    session_id: str | None = None,
    session_name: str | None = None,
    *,
    _image_placeholder: bool = False,
) -> Panel:
    """Produce a Rich Panel for the welcome banner."""
    # Username
    try:
        username = getpass.getuser()
        username = username[0].upper() + username[1:] if username else "User"
    except Exception:
        username = "User"

    # Logo
    logo = _render_image_placeholder() if _image_placeholder else _render_pixel_logo()

    # Description (centered vertically beside the logo)
    desc_text = Text(_("Your AI-powered Infrastructure as Code assistant"), style="italic white")

    # Use a table for side-by-side layout with vertical centering
    logo_table = Table(show_header=False, show_edge=False, box=None, padding=0, expand=True)
    logo_table.add_column(ratio=1)
    logo_table.add_column(ratio=2)
    logo_table.add_row(logo, Align(desc_text, align="center", vertical="middle"))

    # Shorten cwd
    cwd_path = Path(cwd).resolve()
    try:
        cwd_display = "~/" + cwd_path.relative_to(Path.home()).as_posix()
    except ValueError:
        cwd_display = str(cwd_path)

    # Provider / model display
    provider_name = _get_provider_display()
    if provider_name and model:
        model_display = f"{provider_name} / {model}"
    else:
        model_display = model

    from iac_code import __version__

    session_display: Text
    if session_name and session_id:
        session_display = Text("  {}: {} ({})".format(_("Session"), session_name, session_id), style="dim")
    elif session_id:
        session_display = Text("  {}: {}".format(_("Session"), session_id), style="dim")
    else:
        session_display = Text()

    items = [
        Text(),
        Text("  {} {}!".format(_("Welcome back"), username), style="bold"),
        Text(),
        logo_table,
        Text(),
        Text(f"  iac-code v{__version__}", style="dim"),
        Text(f"  {model_display}", style="dim") if model_display else Text(),
        Text(f"  {cwd_display}", style="dim"),
        session_display,
    ]

    from iac_code.utils.log import is_debug_enabled

    if is_debug_enabled():
        from iac_code.config import get_config_dir

        log_path = get_config_dir() / "logs" / "latest.log"
        items.append(Text())
        items.append(Text("  {}".format(_("Debug mode")), style="bold yellow"))
        items.append(Text("  {}: {}".format(_("Log file"), log_path), style="dim yellow"))

    return Panel(Group(*items), border_style=ACCENT, expand=True)


def print_welcome_banner(
    console: Console,
    model: str,
    cwd: str,
    session_id: str | None = None,
    session_name: str | None = None,
) -> None:
    """Print the banner with a high-resolution logo when supported."""
    from iac_code.ui.terminal_image import (
        IMAGE_COLUMNS,
        IMAGE_ROWS,
        build_terminal_image_escape,
        detect_terminal_image_protocol,
        load_terminal_logo_png,
        terminal_cell_size,
    )

    protocol = detect_terminal_image_protocol(console)
    image_escape = ""
    if protocol is not None:
        try:
            image_escape = build_terminal_image_escape(
                protocol,
                load_terminal_logo_png(),
                columns=IMAGE_COLUMNS,
                rows=IMAGE_ROWS,
                cell_size=terminal_cell_size(console),
            )
        except (OSError, ValueError):
            protocol = None

    panel = render_welcome_banner(
        model,
        cwd,
        session_id=session_id,
        session_name=session_name,
        _image_placeholder=protocol is not None,
    )
    rendered_height = len(console.render_lines(panel, console.options, pad=False)) if protocol is not None else 0
    console.print(panel)

    if protocol is None or not image_escape:
        return

    # After Console.print(), the cursor is one line below the panel. The image
    # placeholder begins on panel row 4 and after five columns (border,
    # padding, and the three-cell logo indent). Drawing between DECSC/DECRC
    # keeps the prompt position unchanged for Kitty, iTerm2, and Sixel.
    logo_top_row = 4
    cursor_up = max(1, rendered_height - logo_top_row)
    overlay = "\0337\033[{}A\033[5C{}\0338".format(cursor_up, image_escape)
    try:
        console.file.write(overlay)
        console.file.flush()
    except (AttributeError, OSError, UnicodeError):
        # The text banner has already been printed. Output streams that reject
        # control sequences remain usable; the next redraw will use detection
        # again and can fall back to Braille.
        return
