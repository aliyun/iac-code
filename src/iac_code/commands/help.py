"""Help command"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text

from iac_code.i18n import _

if TYPE_CHECKING:
    from iac_code.commands.registry import CommandRegistry
    from iac_code.ui.repl import CommandContext


async def help_command(
    registry: "CommandRegistry",
    context: "CommandContext",
    **kwargs,
) -> str | None:
    """Show help information inline."""
    text = Text()

    text.append("iac-code", style="bold cyan")
    text.append(" - ")
    text.append(_("AI-powered infrastructure orchestration tool"), style="dim")
    text.append("\n\n")

    text.append(_("Commands:"), style="bold")
    text.append("\n")
    for cmd in registry.get_all():
        text.append(f"  /{cmd.name:<12}", style="cyan")
        text.append(f"  {cmd.description}\n")

    text.append("\n")
    text.append(_("Shortcuts:"), style="bold")
    text.append("\n")
    shortcuts = [
        ("Enter", _("Send message")),
        ("Esc+Enter", _("New line")),
        ("/", _("Show command suggestions")),
        ("Ctrl+C", _("Exit")),
    ]
    for key, description in shortcuts:
        text.append(f"  {key:<14}", style="cyan")
        text.append(f"  {description}\n")

    context.console.print(text)
    return None
