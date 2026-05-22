"""Command module"""

from iac_code.commands.auth import auth_command
from iac_code.commands.clear import clear_command
from iac_code.commands.compact import compact_command
from iac_code.commands.debug import debug_command
from iac_code.commands.effort import effort_command
from iac_code.commands.exit import exit_command
from iac_code.commands.help import help_command
from iac_code.commands.model import model_command
from iac_code.commands.registry import Command, CommandRegistry, LocalCommand, PromptCommand
from iac_code.commands.resume import resume_command
from iac_code.i18n import _


def create_default_registry() -> CommandRegistry:
    """Create and register all default commands"""
    registry = CommandRegistry()
    registry.register(
        LocalCommand(
            name="help",
            description=_("Show available commands"),
            handler=help_command,
            aliases=["?"],
            history_mode="session",
        )
    )
    registry.register(
        LocalCommand(
            name="clear",
            description=_("Clear conversation history"),
            handler=clear_command,
            history_mode="session",
        )
    )
    registry.register(
        LocalCommand(
            name="model",
            description=_("Show or switch model"),
            handler=model_command,
            arg_names=["model_name"],
            history_mode="session",
        )
    )
    registry.register(
        LocalCommand(
            name="effort",
            description=_("Show or switch thinking effort"),
            handler=effort_command,
            arg_names=["level"],
            history_mode="session",
        )
    )
    registry.register(
        LocalCommand(
            name="compact",
            description=_("Compact conversation context"),
            handler=compact_command,
            progress_label=_("Compacting conversation"),
            history_mode="session",
        )
    )
    registry.register(
        LocalCommand(
            name="exit",
            description=_("Exit the application"),
            handler=exit_command,
            aliases=["quit", "q"],
            history_mode="none",
        )
    )
    registry.register(
        LocalCommand(
            name="auth",
            description=_("Authenticate with LLM provider"),
            handler=auth_command,
            aliases=["login"],
            history_mode="session",
        )
    )
    registry.register(
        LocalCommand(
            name="debug",
            description=_("Toggle debug logging"),
            handler=debug_command,
            arg_hint="[on|off]",
            history_mode="session",
        )
    )
    registry.register(
        LocalCommand(
            name="resume",
            description=_("Resume a previous session"),
            handler=resume_command,
            arg_hint=_("[conversation id or search term]"),
            history_mode="session",
        )
    )
    return registry


__all__ = ["Command", "CommandRegistry", "LocalCommand", "PromptCommand", "create_default_registry"]
