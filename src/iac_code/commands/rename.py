"""/rename command - rename the current session."""

from __future__ import annotations

import inspect
from typing import Any

from iac_code.commands.registry import CommandResult
from iac_code.i18n import _
from iac_code.services.session_metadata import normalize_session_name


async def rename_command(context=None, args: list[str] | None = None, **_kwargs: Any) -> CommandResult:
    """Rename the current interactive session."""
    if context is None or getattr(context, "repl", None) is None:
        return CommandResult(_("Rename is only available in interactive mode."), is_error=True)

    repl = context.repl
    args = args or []
    if len(args) > 1:
        return CommandResult(_("Usage: /rename <name>"), is_error=True)

    if args:
        raw_name = args[0]
    else:
        prompt_for_session_name = getattr(repl, "prompt_for_session_name", None)
        if prompt_for_session_name is None:
            return CommandResult(_("Rename is only available in interactive mode."), is_error=True)
        raw_name = await prompt_for_session_name()
        if raw_name is None:
            return CommandResult(_("Rename cancelled"))

    try:
        name = normalize_session_name(raw_name)
        result = repl.rename_current_session(name)
        if inspect.isawaitable(result):
            result = await result
    except ValueError as exc:
        return CommandResult(str(exc), is_error=True)

    if result == "unchanged":
        return CommandResult(_("Session is already named {name}").format(name=name))
    return CommandResult(_("Renamed session to {name}").format(name=name), refresh_banner=True)
