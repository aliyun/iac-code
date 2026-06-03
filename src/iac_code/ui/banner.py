"""Welcome banner rendering."""

from __future__ import annotations

import getpass
import shlex
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from iac_code.i18n import _

if TYPE_CHECKING:
    from iac_code.services.update_checker import PendingUpdate

# Cloud logo (same as components/logo.py)
LOGO_LINES = [
    "      ▄▄███▄▄      ",
    "   ▄██████████▄▄   ",
    " ▄█▀████████████▄  ",
    "████████████████████",
    " ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀ ",
]

ACCENT = "bright_cyan"


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
) -> Panel:
    """Produce a Rich Panel for the welcome banner."""
    # Username
    try:
        username = getpass.getuser()
        username = username[0].upper() + username[1:] if username else "User"
    except Exception:
        username = "User"

    # Logo
    logo = Text()
    for i, line in enumerate(LOGO_LINES):
        if i > 0:
            logo.append("\n")
        logo.append(f"   {line}", style="bright_cyan")

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
