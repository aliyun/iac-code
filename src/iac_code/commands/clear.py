"""Clear command — clears conversation history and screen."""

from __future__ import annotations


async def clear_command(context=None, **kwargs) -> str:
    """Clear conversation history and the terminal screen."""
    store = context.store if context else kwargs.get("store")
    if store:
        store.set_state(messages=[])

    if context and hasattr(context, "repl"):
        agent_loop = getattr(context.repl, "_agent_loop", None)
        if agent_loop:
            agent_loop.reset()
        if hasattr(context.repl, "_command_log"):
            context.repl._command_log.clear()

    if context and hasattr(context, "console"):
        console = context.console
        if console is None:
            return ""
        # ESC[H  — cursor home
        # ESC[2J — erase visible screen
        # ESC[3J — erase scrollback buffer
        console.file.write("\033[H\033[2J\033[3J")
        console.file.flush()

        # Re-render the welcome banner
        from iac_code.ui.banner import render_welcome_banner

        state = store.get_state() if store else None
        if state:
            console.print(render_welcome_banner(state.model, state.cwd))

    return ""
