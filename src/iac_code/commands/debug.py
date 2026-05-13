"""Debug command — toggle debug logging at runtime."""

from __future__ import annotations

from iac_code.i18n import _
from iac_code.utils.log import (
    current_log_file,
    disable_debug_at_runtime,
    enable_debug_at_runtime,
    is_debug_enabled,
)


async def debug_command(**kwargs) -> str:
    """Show or toggle debug logging.

    Usage: /debug [on|off]
    """
    context = kwargs.get("context")
    if context is None:
        return _("Debug command requires a context.")

    repl = getattr(context, "repl", None)
    session_id = getattr(repl, "_session_id", None) if repl else None
    if not session_id:
        return _("No active session.")

    args = kwargs.get("args") or []
    action = args[0].lower() if args else ""

    if action == "" or action == "status":
        if is_debug_enabled():
            log_path = current_log_file()
            return _("Debug logging is on. Log file: {path}").format(path=log_path)
        return _("Debug logging is off.")

    if action == "on":
        log_path = enable_debug_at_runtime(session_id)
        return _("Debug logging enabled. Log file: {path}").format(path=log_path)

    if action == "off":
        disable_debug_at_runtime()
        return _("Debug logging disabled.")

    return _("Usage: /debug [on|off]")
