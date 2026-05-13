"""Exit command"""

from __future__ import annotations


async def exit_command(**kwargs) -> str:
    """Exit application by raising ExitREPLError."""
    from iac_code.ui.repl import ExitREPLError

    raise ExitREPLError()
