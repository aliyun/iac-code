"""Non-secret readiness checks for runtimes that embed iac-code."""

from __future__ import annotations

from typing import Any


def configuration_readiness(*, model: str) -> dict[str, Any]:
    """Return configuration completeness without exposing credential values.

    This deliberately performs no network request. It answers whether the same
    local configuration sources used by iac-code contain the fields needed to
    create an LLM provider and Alibaba Cloud credential.
    """
    try:
        llm = _llm_readiness(model)
    except Exception:
        llm = {
            "ready": False,
            "source": None,
            "provider": None,
            "providerDisplay": None,
            "model": model or None,
            "missing": ["configuration"],
        }
    try:
        cloud = _aliyun_readiness()
    except Exception:
        cloud = {
            "ready": False,
            "provider": "aliyun",
            "mode": None,
            "regionId": None,
            "missing": ["credentials"],
        }
    return {
        "schemaVersion": 1,
        "llm": llm,
        "cloud": cloud,
    }


def _llm_readiness(model: str) -> dict[str, Any]:
    from iac_code.config import get_llm_source, load_credentials
    from iac_code.providers.manager import _detect_provider_name
    from iac_code.providers.registry import PROVIDER_REGISTRY

    source = get_llm_source()
    effective_model = model
    provider_key: str | None = None
    api_key = ""
    missing: list[str] = []

    if source == "qwenpaw":
        from iac_code.services.qwenpaw_source import QwenPawError, load_from_qwenpaw

        try:
            partner = load_from_qwenpaw()
        except QwenPawError:
            partner = None
        if partner is None:
            missing.append("partner_configuration")
        else:
            provider_key = partner.provider_key
            effective_model = partner.model
            api_key = partner.api_key or ""
    else:
        try:
            provider_key = _detect_provider_name(effective_model)
        except ValueError:
            missing.append("provider")
        if provider_key is not None:
            api_key = load_credentials(model=effective_model).get(provider_key, "")

    descriptor = PROVIDER_REGISTRY.get(provider_key or "")
    if provider_key is not None and descriptor is None:
        missing.append("provider")
    if not effective_model:
        missing.append("model")
    if descriptor is not None and descriptor.require_api_key and not api_key:
        missing.append("api_key")

    return {
        "ready": not missing,
        "source": source,
        "provider": provider_key,
        "providerDisplay": descriptor.display_name if descriptor is not None else None,
        "model": effective_model or None,
        "missing": _deduplicate(missing),
    }


def _aliyun_readiness() -> dict[str, Any]:
    from iac_code.services.providers.aliyun import CREDENTIAL_MODES, MODE_REQUIRED_FIELDS, AliyunCredentials

    credential = AliyunCredentials.load()
    if credential is None:
        return {
            "ready": False,
            "provider": "aliyun",
            "mode": None,
            "regionId": None,
            "missing": ["credentials"],
        }

    mode = credential.mode
    missing: list[str] = []
    if mode not in CREDENTIAL_MODES:
        missing.append("credential_mode")
    else:
        missing.extend(
            sorted(
                field_name
                for field_name in MODE_REQUIRED_FIELDS.get(mode, set())
                if not getattr(credential, field_name, None)
            )
        )
    if not credential.region_id:
        missing.append("region_id")

    return {
        "ready": not missing,
        "provider": "aliyun",
        "mode": mode if mode in CREDENTIAL_MODES else None,
        "regionId": credential.region_id or None,
        "missing": _deduplicate(missing),
    }


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
