"""/skills command — manage discovered skills."""

from __future__ import annotations

from typing import Any

from iac_code.i18n import _
from iac_code.skills.settings import save_disabled_skills


async def skills_command(context=None, args: list[str] | None = None, **_kwargs: Any) -> str:
    """Open the interactive skills management UI."""
    if context is None or not hasattr(context, "repl"):
        return _("Skills management is only available in interactive mode.")

    repl = context.repl
    from iac_code.ui.dialogs.skills_picker import SkillsPicker

    picker = SkillsPicker(
        list(getattr(repl, "skill_management_items", [])),
        keybinding_manager=getattr(repl, "_keybinding_manager", None),
    )
    disabled = picker.run()
    if disabled is None:
        return _("Skills update cancelled")

    save_disabled_skills(set(disabled), locked_skill_names=set(getattr(repl, "locked_skill_names", set())))
    repl.refresh_skills()
    return _("Skills updated")
