"""Effort command — show or change the thinking/reasoning effort level."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from iac_code.commands.auth import _BACK, PROVIDERS, LLMProvider, _select, save_active_provider_config
from iac_code.config import get_active_provider_key, get_provider_config
from iac_code.i18n import _

if TYPE_CHECKING:
    from iac_code.ui.dialogs.model_picker import EffortLevel
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


def _load_current_effort(key_name: str, fallback: "EffortLevel") -> "EffortLevel":
    picker = _load_picker_module()
    level_by_value = {lvl.value: lvl for lvl in picker.EffortLevel}
    saved = get_provider_config(key_name).get("effort")
    if isinstance(saved, str) and saved in level_by_value:
        return level_by_value[saved]
    return fallback


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

    allowed = list(spec.allowed_efforts)
    level_by_value = {lvl.value: lvl for lvl in picker.EffortLevel}

    assert spec.default_effort is not None  # guarded by supports_effort above
    current = _load_current_effort(provider_key, spec.default_effort)

    # Non-interactive: /effort <level>
    if args:
        token = args[0].strip().lower()
        target = level_by_value.get(token)
        if target is None or target not in allowed:
            labels = ", ".join(lvl.value for lvl in allowed)
            return _("Invalid effort. Allowed: {labels}").format(labels=labels)
        return _apply_effort(provider, current_model, target, store)

    # Interactive: show picker
    if not context or not context.console:
        return _("Current effort: {effort}").format(effort=current.value)

    options = [f"{picker.EFFORT_SYMBOLS[lvl]}  {lvl.value}" for lvl in allowed]
    default_idx = allowed.index(current) if current in allowed else 0

    sys.stdout.write("\033[?1049h")
    sys.stdout.flush()
    try:
        idx = _select(
            _("Select effort for {model}").format(model=current_model),
            options,
            default_index=default_idx,
        )
    finally:
        sys.stdout.write("\033[?1049l")
        sys.stdout.flush()

    if idx is None or idx is _BACK:
        return _("Kept effort as {effort}").format(effort=current.value)

    selected = allowed[idx]
    if selected == current:
        return _("Kept effort as {effort}").format(effort=current.value)

    return _apply_effort(provider, current_model, selected, store)


def _apply_effort(provider: LLMProvider, model: str, effort: "EffortLevel", store) -> str:
    save_active_provider_config(provider, model, effort=effort.value)
    if store is not None:
        store.set_state(effort_level=effort)
    return _("Effort switched to: {effort}").format(effort=effort.value)
