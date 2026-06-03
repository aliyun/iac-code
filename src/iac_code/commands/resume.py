"""/resume command — pick or jump to a saved session."""

from __future__ import annotations

from typing import Any

from iac_code.i18n import _
from iac_code.services.session_resolver import ResolutionStatus, resolve_session_argument


async def resume_command(context=None, args: list[str] | None = None, **_kwargs: Any) -> str:
    """Resume a previous session.

    With an argument, resolves the input as a session id (or unique id
    prefix). Without arguments, opens an interactive picker.

    Cross-project sessions never hot-swap — they print the
    ``cd ... && iac-code --resume <id>`` command and (best-effort) copy
    it to the clipboard.
    """
    if context is None or not hasattr(context, "repl"):
        return _("Resume is only available in interactive mode.")
    repl = context.repl
    arg_str = " ".join(args or []).strip()

    index = getattr(repl, "session_index", None)
    if index is None:
        return _("Resume is unavailable: session index not initialised.")

    if arg_str:
        resolution = resolve_session_argument(index, repl._original_cwd, arg_str)
        if resolution.status == ResolutionStatus.NOT_FOUND:
            return _("Session not found: {arg}").format(arg=arg_str)
        if resolution.status == ResolutionStatus.FOUND:
            if resolution.entry is None:
                return _("Session not found: {arg}").format(arg=arg_str)
            await repl.swap_or_announce_session(resolution.entry)
            return ""
        if resolution.status == ResolutionStatus.AMBIGUOUS_NAME:
            from iac_code.ui.dialogs.resume_picker import ResumePicker

            picker = ResumePicker(
                index=index,
                current_cwd=repl._original_cwd,
                current_session_id=repl.session_id,
                keybinding_manager=getattr(repl, "_keybinding_manager", None),
                renderer=getattr(repl, "renderer", None),
                entries=resolution.candidates,
            )
            selected = picker.run()
            if selected is None:
                return _("Resume cancelled")
            await repl.swap_or_announce_session(selected)
            return ""
        return _("Unable to resolve session: {arg}").format(arg=arg_str)

    from iac_code.ui.dialogs.resume_picker import ResumePicker

    picker = ResumePicker(
        index=index,
        current_cwd=repl._original_cwd,
        current_session_id=repl.session_id,
        keybinding_manager=getattr(repl, "_keybinding_manager", None),
        renderer=getattr(repl, "renderer", None),
    )
    selected = picker.run()
    if selected is None:
        return _("Resume cancelled")
    await repl.swap_or_announce_session(selected)
    return ""
