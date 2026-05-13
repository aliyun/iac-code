"""Model command — switch or display current model."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from iac_code.commands.auth import (
    _BACK,
    PROVIDERS,
    LLMProvider,
    _classify_base_url,
    get_configured_providers,
    save_active_provider_config,
    select_model_interactive,
)
from iac_code.config import _load_yaml, get_active_provider_key, get_settings_path
from iac_code.i18n import _
from iac_code.services.telemetry import log_event
from iac_code.services.telemetry.names import Events

if TYPE_CHECKING:
    from iac_code.ui.repl import CommandContext


def _get_active_provider() -> LLMProvider | None:
    """Get the currently active provider config from settings."""
    key_name = get_active_provider_key()
    if not key_name:
        return None
    for p in PROVIDERS:
        if str(p["key_name"]) == key_name:
            return p
    return None


def _get_active_provider_models() -> list[str]:
    """Get model list for the currently active provider."""
    provider = _get_active_provider()
    if provider:
        return list(provider["models"])
    return []


async def model_command(context: "CommandContext | None" = None, args: list[str] | None = None, **kwargs) -> str | None:
    """Switch or display current model."""
    store = context.store if context else kwargs.get("store")
    args = args or []

    if args:
        new_model = args[0]
        provider = _get_active_provider()
        if provider:
            # Get current custom base URL if any
            settings = _load_yaml(get_settings_path())
            active = settings.get("activeProvider")
            custom_base_url = None
            if isinstance(active, dict):
                custom_base_url = active.get("apiBase")

            save_active_provider_config(provider, new_model)

            # Log telemetry event
            log_event(
                Events.AUTH_CONFIGURED,
                {
                    "provider": provider["name"],
                    "has_custom_base_url": bool(custom_base_url),
                    "custom_base_url_host_kind": _classify_base_url(custom_base_url),
                },
            )

        if store:
            store.set_state(model=new_model)
        return _("Model switched to: {model}").format(model=new_model)

    if not context or not context.console:
        state = store.get_state() if store else None
        return _("Current model: {model}").format(model=state.model if state else "")

    if not get_configured_providers():
        return _("No configured providers. Run /auth first.")

    provider = _get_active_provider()
    if not provider:
        return _("No configured providers. Run /auth first.")

    current_model = store.get_state().model if store else ""
    models = list(provider["models"])

    # Use alternate screen for clean UI
    sys.stdout.write("\033[?1049h")
    sys.stdout.flush()
    try:
        selected = select_model_interactive(
            models,
            current_model=current_model,
            provider_display_name=str(provider["display_name"]),
        )
    finally:
        sys.stdout.write("\033[?1049l")
        sys.stdout.flush()

    if selected is _BACK or selected is None:
        return _("Kept model as {model}").format(model=current_model)

    new_model = str(selected)

    # Get current custom base URL if any
    settings = _load_yaml(get_settings_path())
    active = settings.get("activeProvider")
    custom_base_url = None
    if isinstance(active, dict):
        custom_base_url = active.get("apiBase")

    save_active_provider_config(provider, new_model)

    # Log telemetry event
    log_event(
        Events.AUTH_CONFIGURED,
        {
            "provider": provider["name"],
            "has_custom_base_url": bool(custom_base_url),
            "custom_base_url_host_kind": _classify_base_url(custom_base_url),
        },
    )

    if store:
        store.set_state(model=new_model)
    return _("Model switched to: {model}").format(model=new_model)
