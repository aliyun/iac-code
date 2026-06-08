"""Command module"""

from iac_code.commands.auth import auth_command
from iac_code.commands.clear import clear_command
from iac_code.commands.compact import compact_command
from iac_code.commands.debug import debug_command
from iac_code.commands.effort import effort_command
from iac_code.commands.exit import exit_command
from iac_code.commands.help import help_command
from iac_code.commands.memory import memory_command, memory_folder_command
from iac_code.commands.model import model_command
from iac_code.commands.prompt import prompt_command
from iac_code.commands.registry import Command, CommandRegistry, CommandResult, LocalCommand, PromptCommand
from iac_code.commands.rename import rename_command
from iac_code.commands.resume import resume_command
from iac_code.commands.skills import skills_command
from iac_code.commands.status import status_command
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
            name="memory",
            description=_("Edit IAC-CODE memory files"),
            handler=memory_command,
            history_mode="session",
        )
    )
    registry.register(
        LocalCommand(
            name="memory-folder",
            description=_("View and manage persistent memories"),
            handler=memory_folder_command,
            arg_hint=_("[<name>|search <query>|delete <name>|help]"),
            hidden=True,
            history_mode="session",
        )
    )
    registry.register(
        LocalCommand(
            name="prompt",
            description=_("Export current prompt snapshot"),
            handler=prompt_command,
            hidden=True,
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
    registry.register(
        LocalCommand(
            name="rename",
            description=_("Rename the current session"),
            handler=rename_command,
            arg_hint=_("<name>"),
            history_mode="session",
        )
    )
    registry.register(
        LocalCommand(
            name="skills",
            description=_("Manage skills"),
            handler=skills_command,
            history_mode="session",
        )
    )
    registry.register(
        LocalCommand(
            name="status",
            description=_("Show current session status"),
            handler=status_command,
            history_mode="session",
        )
    )
    return registry


__all__ = ["Command", "CommandRegistry", "CommandResult", "LocalCommand", "PromptCommand", "create_default_registry"]
