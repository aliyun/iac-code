from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from iac_code.services.providers.aliyun import AliyunCredential, use_aliyun_credential
from iac_code.services.telemetry import use_session_id, use_user_id


@contextlib.contextmanager
def a2a_request_context(
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    aliyun_credential: AliyunCredential | None = None,
) -> Iterator[None]:
    with contextlib.ExitStack() as stack:
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

    register_cloud_tools(tool_registry, CloudCredentials())


def configure_runtime_model(
    runtime: Any,
    model: str,
    *,
    from_metadata: bool,
    metadata_api_key: str | None = None,
) -> None:
    provider_manager = getattr(runtime, "provider_manager", None)
    reconfigure = getattr(provider_manager, "reconfigure", None)
    if not callable(reconfigure):
        return
    was_metadata_model = bool(getattr(runtime, "_iac_code_a2a_metadata_model_applied", False))
    has_metadata_api_key = metadata_api_key is not None
    was_metadata_api_key = bool(getattr(runtime, "_iac_code_a2a_metadata_api_key_applied", False))
    if not from_metadata and not was_metadata_model and not has_metadata_api_key and not was_metadata_api_key:
        return

    from iac_code.config import load_credentials

    provider_key_override = getattr(provider_manager, "_provider_key_override", None)
    base_url_override = getattr(provider_manager, "_base_url_override", None)
    credentials = getattr(provider_manager, "_credentials", None)
    if (
        not isinstance(credentials, dict)
        or provider_key_override is None
        or has_metadata_api_key
        or was_metadata_api_key
    ):
        credentials = load_credentials(model=model)
    if metadata_api_key is not None:
        credentials = credentials_with_metadata_api_key(
            model=model,
            credentials=credentials,
            provider_key_override=provider_key_override,
            metadata_api_key=metadata_api_key,
        )
    reconfigure(model, credentials, provider_key_override, base_url_override)
    setattr(runtime, "_iac_code_a2a_metadata_model_applied", from_metadata)
    setattr(runtime, "_iac_code_a2a_metadata_api_key_applied", has_metadata_api_key)


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
