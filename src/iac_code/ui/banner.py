"""Welcome banner rendering."""

from __future__ import annotations

import getpass
from pathlib import Path

from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from iac_code.i18n import _

# Cloud logo (same as components/logo.py)
LOGO_LINES = [
    "      ▄▄███▄▄      ",
    "   ▄██████████▄▄   ",
    " ▄█▀████████████▄  ",
    "████████████████████",
    " ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀ ",
]

ACCENT = "bright_cyan"


def _get_provider_display() -> str:
    """Get the active provider display name from settings, translated."""
    try:
        from iac_code.config import get_active_provider_key, get_provider_config

        key = get_active_provider_key()
        if not key:
            return ""
        name = get_provider_config(key).get("name", "")
        provider_display_names = {
            "DashScope": _("DashScope"),
            "DashScope Token Plan": _("DashScope Token Plan"),
            "OpenAI": _("OpenAI"),
            "Anthropic": _("Anthropic"),
            "DeepSeek": _("DeepSeek"),
            "OpenAPI Compatible": _("OpenAPI Compatible"),
        }
        return provider_display_names.get(name, name)
    except Exception:
        return ""


def render_welcome_banner(model: str, cwd: str, session_id: str | None = None) -> Panel:
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
        cwd_display = "~/" + str(cwd_path.relative_to(Path.home()))
    except ValueError:
        cwd_display = str(cwd_path)

    # Provider / model display
    provider_name = _get_provider_display()
    if provider_name and model:
        model_display = f"{provider_name} / {model}"
    else:
        model_display = model

    items = [
        Text(),
        Text("  {} {}!".format(_("Welcome back"), username), style="bold"),
        Text(),
        logo_table,
        Text(),
        Text(f"  {model_display}", style="dim") if model_display else Text(),
        Text(f"  {cwd_display}", style="dim"),
        Text("  {}: {}".format(_("Session"), session_id), style="dim") if session_id else Text(),
    ]

    from iac_code.utils.log import is_debug_enabled

    if is_debug_enabled():
        from iac_code.config import get_config_dir

        log_path = get_config_dir() / "logs" / "latest.log"
        items.append(Text())
        items.append(Text("  {}".format(_("Debug mode")), style="bold yellow"))
        items.append(Text("  {}: {}".format(_("Log file"), log_path), style="dim yellow"))

    return Panel(Group(*items), border_style=ACCENT, expand=True)
