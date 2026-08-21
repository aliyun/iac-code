from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator
from typing import Any

from iac_code.providers.request_policy import ProviderRequestPolicy
from iac_code.services.providers.aliyun import AliyunCredential, use_aliyun_credential
from iac_code.services.telemetry import use_session_id, use_telemetry_channel, use_user_id

_RUNTIME_OVERRIDE_UNSET = object()
_preferred_language: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "iac_code_a2a_preferred_language",
    default=None,
)


def get_a2a_preferred_language() -> str | None:
    """Return the request-local language requested by the A2A caller."""
    return _preferred_language.get()


@contextlib.contextmanager
def a2a_request_context(
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    aliyun_credential: AliyunCredential | None = None,
    preferred_language: str | None = None,
    telemetry_channel: str | None = None,
) -> Iterator[None]:
    with contextlib.ExitStack() as stack:
        if telemetry_channel:
            stack.enter_context(use_telemetry_channel(telemetry_channel))
        if preferred_language:
            token = _preferred_language.set(preferred_language)
            stack.callback(_preferred_language.reset, token)
        if session_id:
            stack.enter_context(use_session_id(session_id))
        if user_id:
            stack.enter_context(use_user_id(user_id))
        if aliyun_credential is not None:
            stack.enter_context(use_aliyun_credential(aliyun_credential))
        yield


def refresh_runtime_cloud_tools(runtime: Any) -> None:
    refresh_cloud_tools = getattr(runtime, "refresh_cloud_tools", None)
    if callable(refresh_cloud_tools):
        refresh_cloud_tools()
        return
    tool_registry = getattr(runtime, "tool_registry", None)
    if tool_registry is None:
        return

    from iac_code.services.cloud_credentials import CloudCredentials
    from iac_code.tools.cloud.registry import register_cloud_tools

    services = getattr(runtime, "aliyun_services", None)
    if services is None:
        return
    register_cloud_tools(tool_registry, CloudCredentials(), services)


def configure_runtime_model(
    runtime: Any,
    model: str,
    *,
    from_metadata: bool,
    metadata_api_key: str | None = None,
    request_policy_override: ProviderRequestPolicy | None = None,
    provider_key_override: str | None | object = _RUNTIME_OVERRIDE_UNSET,
    provider_api_key_override: str | None = None,
    provider_base_url_override: str | None = None,
    provider_config_frozen: bool = False,
    provider_config_override: dict[str, Any] | None = None,
    effort_override: str | None | object = _RUNTIME_OVERRIDE_UNSET,
) -> None:
    provider_manager = getattr(runtime, "provider_manager", None)
    reconfigure = getattr(provider_manager, "reconfigure", None)
    if not callable(reconfigure):
        return
    was_metadata_model = bool(getattr(runtime, "_iac_code_a2a_metadata_model_applied", False))
    has_metadata_api_key = metadata_api_key is not None
    was_metadata_api_key = bool(getattr(runtime, "_iac_code_a2a_metadata_api_key_applied", False))
    has_metadata_policy = request_policy_override is not None and request_policy_override.has_values
    was_metadata_policy = bool(getattr(runtime, "_iac_code_a2a_metadata_request_policy_applied", False))
    provider_override_supplied = provider_key_override is not _RUNTIME_OVERRIDE_UNSET
    effort_override_supplied = effort_override is not _RUNTIME_OVERRIDE_UNSET
    has_runtime_override = provider_override_supplied or effort_override_supplied
    if (
        not from_metadata
        and not was_metadata_model
        and not has_metadata_api_key
        and not was_metadata_api_key
        and not has_metadata_policy
        and not was_metadata_policy
        and not has_runtime_override
    ):
        return

    from iac_code.config import load_credentials

    previous_provider_key = getattr(provider_manager, "_provider_key_override", None)
    effective_provider_key = (
        (provider_key_override if isinstance(provider_key_override, str) else None)
        if provider_override_supplied
        else previous_provider_key
    )
    base_url_override = getattr(provider_manager, "_base_url_override", None)
    if provider_config_frozen:
        base_url_override = provider_base_url_override
    elif provider_override_supplied:
        base_url_override = None
    credentials = getattr(provider_manager, "_credentials", None)
    if provider_config_frozen:
        credentials = (
            {effective_provider_key: provider_api_key_override or ""} if effective_provider_key is not None else {}
        )
    elif (
        provider_override_supplied
        or not isinstance(credentials, dict)
        or effective_provider_key is None
        or has_metadata_api_key
        or was_metadata_api_key
    ):
        credentials = load_credentials(model=model)
    if metadata_api_key is not None:
        credentials = credentials_with_metadata_api_key(
            model=model,
            credentials=credentials,
            provider_key_override=effective_provider_key,
            metadata_api_key=metadata_api_key,
        )
    reconfigure_kwargs: dict[str, Any] = {}
    if provider_config_frozen and provider_config_override is not None:
        reconfigure_kwargs["provider_config_override"] = provider_config_override
    if has_metadata_policy or was_metadata_policy:
        reconfigure_kwargs["request_policy_override"] = request_policy_override if has_metadata_policy else None
    was_effort_override = bool(getattr(runtime, "_iac_code_a2a_effort_override_applied", False))
    has_effort_override = effort_override_supplied and isinstance(effort_override, str)
    if has_effort_override or was_effort_override:
        reconfigure_kwargs["effort_override"] = effort_override if has_effort_override else None
    reconfigure(model, credentials, effective_provider_key, base_url_override, **reconfigure_kwargs)
    if provider_override_supplied:
        setattr(provider_manager, "_ignore_llm_source", effective_provider_key is not None)
    setattr(runtime, "_iac_code_a2a_metadata_model_applied", from_metadata)
    setattr(runtime, "_iac_code_a2a_metadata_api_key_applied", has_metadata_api_key)
    setattr(runtime, "_iac_code_a2a_metadata_request_policy_applied", has_metadata_policy)
    setattr(runtime, "_iac_code_a2a_effort_override_applied", has_effort_override)


def credentials_with_metadata_api_key(
    *,
    model: str,
    credentials: dict[str, str],
    provider_key_override: str | None,
    metadata_api_key: str,
) -> dict[str, str]:
    provider_key = provider_key_override
    if provider_key is None:
        try:
            from iac_code.providers.manager import _detect_provider_name

            provider_key = _detect_provider_name(model)
        except ValueError:
            return credentials

    from iac_code.config import _KEY_NAME_TO_CRED_SLOT

    slot = _KEY_NAME_TO_CRED_SLOT.get(provider_key)
    if not slot:
        return credentials
    updated = dict(credentials)
    updated[slot] = metadata_api_key
    return updated
