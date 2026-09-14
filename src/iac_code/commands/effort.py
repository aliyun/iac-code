"""Effort command — show or change the thinking/reasoning effort level."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Collection

from iac_code.commands.auth import _BACK, PROVIDERS, LLMProvider, _select, save_active_provider_config
from iac_code.config import get_active_provider_key, get_provider_config
from iac_code.i18n import _

if TYPE_CHECKING:
    from iac_code.ui.repl import CommandContext


def _load_picker_module():
    """Lazy import to avoid a circular import through iac_code.ui.__init__."""
    import importlib

    return importlib.import_module("iac_code.ui.dialogs.model_picker")


def _active_provider() -> LLMProvider | None:
    key = get_active_provider_key()
    if not key:
        return None
    for p in PROVIDERS:
        if str(p["key_name"]) == key:
            return p
    return None


def _load_saved_effort(key_name: str, allowed: Collection[str]) -> str | None:
    saved = get_provider_config(key_name).get("effort")
    if isinstance(saved, str):
        normalized = saved.strip().lower()
        if normalized in allowed:
            return normalized
    return None


async def effort_command(
    context: "CommandContext | None" = None,
    args: list[str] | None = None,
    **kwargs,
) -> str | None:
    """Show or change the thinking effort level for the active model."""
    store = context.store if context else kwargs.get("store")
    args = args or []

    provider = _active_provider()
    if not provider:
        return _("No configured providers. Run /auth first.")

    current_model = store.get_state().model if store else ""
    if not current_model:
        return _("No model selected. Run /model first.")

    from iac_code.providers.thinking import get_thinking_spec

    picker = _load_picker_module()
    provider_key = str(provider["key_name"])
    spec = get_thinking_spec(provider_key, current_model)
    if not spec.supports_effort:
        return _("Model {model} does not support effort.").format(model=current_model)

    allowed = list(spec.effort_values)

    # Non-interactive: /effort <level>
    if args:
        token = args[0].strip().lower()
        if token not in allowed:
            labels = _allowed_effort_label(spec.effort_range, allowed)
            return _("Invalid effort. Allowed: {labels}").format(labels=labels)
        return _apply_effort(provider, current_model, token, store)

    saved_effort = _load_saved_effort(provider_key, allowed)
    has_effective_effort = saved_effort is not None or spec.default_effort_value is not None
    default_effort = spec.default_effort_value or allowed[0]
    current = saved_effort or default_effort

    # Interactive: show picker
    if not context or not context.console:
        if not has_effective_effort:
            return _("Current effort: {effort}").format(effort=_("not configured"))
        return _("Current effort: {effort}").format(effort=current)

    level_by_value = {level.value: level for level in picker.EffortLevel}
    options = [
        f"{picker.EFFORT_SYMBOLS[level_by_value[value]]}  {value}" if value in level_by_value else value
        for value in allowed
    ]
    default_idx = allowed.index(current) if current in allowed else 0

    sys.stdout.write("\033[?1049h")
    sys.stdout.flush()
    try:
        if len(options) <= 12:
            idx = _select(
                _("Select effort for {model}").format(model=current_model),
                options,
                default_index=default_idx,
            )
            selected = None if idx is None or idx is _BACK else allowed[idx]
        else:
            from iac_code.ui.components.select import Select, SelectLayout, TextOption

            context.console.print(_("Select effort for {model}").format(model=current_model))
            selected = Select(
                [TextOption(label=label, value=value) for label, value in zip(options, allowed)],
                default_value=current,
                layout=SelectLayout.COMPACT_VERTICAL,
                visible_count=12,
            ).run(console=context.console)
    finally:
        sys.stdout.write("\033[?1049l")
        sys.stdout.flush()

    if selected is None:
        if not has_effective_effort:
            return _("Kept effort as {effort}").format(effort=_("not configured"))
        return _("Kept effort as {effort}").format(effort=current)

    if selected == current and has_effective_effort:
        return _("Kept effort as {effort}").format(effort=current)

    return _apply_effort(provider, current_model, selected, store)


def _allowed_effort_label(effort_range, allowed: list[str]) -> str:
    if effort_range is not None and len(allowed) > 12:
        return "{}-{}".format(effort_range[0], effort_range[1])
    return ", ".join(allowed)


def _apply_effort(provider: LLMProvider, model: str, effort: str, store) -> str:
    save_active_provider_config(provider, model, effort=effort)
    if store is not None:
        picker = _load_picker_module()
        state_effort = next((level for level in picker.EffortLevel if level.value == effort), effort)
        store.set_state(effort_level=state_effort)
    return _("Effort switched to: {effort}").format(effort=effort)
