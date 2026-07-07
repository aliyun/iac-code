"""Thinking-enabled command -- show or change provider thinking mode."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from iac_code.commands.auth import PROVIDERS, LLMProvider, _select, save_active_provider_config
from iac_code.config import get_active_provider_key, get_provider_config
from iac_code.i18n import _
from iac_code.providers.request_policy import bool_or_none

if TYPE_CHECKING:
    from iac_code.ui.repl import CommandContext


class ThinkingEnabledCommand:
    """Handle the /thinking_enabled command for the active provider."""

    def __init__(self, context: "CommandContext | None", kwargs: dict) -> None:
        self.context = context
        self.store = context.store if context else kwargs.get("store")

    async def run(self, args: list[str] | None = None) -> str:
        args = args or []
        provider = self._active_provider()
        if provider is None:
            return _("No configured providers. Run /auth first.")

        current_model = self._current_model()
        if not current_model:
            return _("No model selected. Run /model first.")

        if args:
            enabled = self._parse_enabled(args[0])
            if enabled is None:
                return _("Invalid thinking_enabled. Allowed: on, off.")
            return self._apply_enabled(provider, current_model, enabled)

        saved = bool_or_none(get_provider_config(str(provider["key_name"])).get("thinkingEnabled"))
        if not self.context or not self.context.console:
            return self._current_message(saved)

        selected = self._select_enabled(saved)
        if selected is None:
            return self._current_message(saved)
        return self._apply_enabled(provider, current_model, selected)

    def _active_provider(self) -> LLMProvider | None:
        key = get_active_provider_key()
        if not key:
            return None
        for provider in PROVIDERS:
            if str(provider["key_name"]) == key:
                return provider
        return None

    def _current_model(self) -> str:
        if self.store is None:
            return ""
        state = self.store.get_state()
        model = getattr(state, "model", "")
        return model if isinstance(model, str) else ""

    @staticmethod
    def _parse_enabled(value: str) -> bool | None:
        return bool_or_none(value)

    def _select_enabled(self, current: bool | None) -> bool | None:
        options = [self._option_label(True), self._option_label(False)]
        default_idx = 1 if current is False else 0
        sys.stdout.write("\033[?1049h")
        sys.stdout.flush()
        try:
            idx = _select(
                self._current_message(current),
                options,
                default_index=default_idx,
            )
        finally:
            sys.stdout.write("\033[?1049l")
            sys.stdout.flush()
        if idx is None:
            return None
        if idx < 0 or idx >= len(options):
            return None
        return idx == 0

    @staticmethod
    def _option_label(enabled: bool) -> str:
        return _("on") if enabled else _("off")

    def _apply_enabled(self, provider: LLMProvider, model: str, enabled: bool) -> str:
        save_active_provider_config(provider, model, thinking_enabled=enabled)
        if self.store is not None:
            self.store.set_state(thinking_enabled=enabled)
        return _("thinking_enabled switched to: {state}").format(state=self._state_label(enabled))

    def _current_message(self, enabled: bool | None) -> str:
        return _("Current thinking_enabled: {state}").format(state=self._state_label(enabled))

    @staticmethod
    def _state_label(enabled: bool | None) -> str:
        if enabled is True:
            return _("enabled")
        if enabled is False:
            return _("disabled")
        return _("not configured")


async def thinking_enabled_command(
    context: "CommandContext | None" = None,
    args: list[str] | None = None,
    **kwargs,
) -> str:
    """Show or change whether the active provider should request thinking."""
    return await ThinkingEnabledCommand(context, kwargs).run(args)
