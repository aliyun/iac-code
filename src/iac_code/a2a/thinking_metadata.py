from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from google.protobuf.json_format import MessageToDict

from iac_code.providers.request_policy import ProviderRequestPolicy, bool_or_none, positive_int_or_none


class A2AThinkingMetadata:
    @classmethod
    def request_policy_from_metadata(cls, metadata: Any | None) -> ProviderRequestPolicy | None:
        if metadata is not None and hasattr(metadata, "DESCRIPTOR"):
            metadata = MessageToDict(metadata, preserving_proto_field_name=False)
        if not isinstance(metadata, Mapping):
            return None
        raw_iac_meta = metadata.get("iac_code")
        if not isinstance(raw_iac_meta, Mapping):
            return None

        raw_thinking = raw_iac_meta.get("thinking")
        thinking_meta = raw_thinking if isinstance(raw_thinking, Mapping) else {}
        enabled = cls._bool_value(thinking_meta, "enabled", "thinking_enabled", "thinkingEnabled")
        if enabled is None:
            enabled = cls._bool_value(raw_iac_meta, "thinking_enabled", "thinkingEnabled")
        effort = cls._string_value(thinking_meta, "effort", "thinking_effort", "thinkingEffort")
        if effort is None:
            effort = cls._string_value(raw_iac_meta, "thinking_effort", "thinkingEffort")
        budget = cls._positive_int_value(thinking_meta, "budget", "thinking_budget", "thinkingBudget")
        if budget is None:
            budget = cls._positive_int_value(raw_iac_meta, "thinking_budget", "thinkingBudget")

        policy = ProviderRequestPolicy(thinking_enabled=enabled, effort=effort, thinking_budget=budget)
        return policy if policy.has_values else None

    @staticmethod
    def _string_value(metadata: Mapping[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _positive_int_value(metadata: Mapping[str, Any], *keys: str) -> int | None:
        for key in keys:
            value = positive_int_or_none(metadata.get(key))
            if value is not None:
                return value
        return None

    @staticmethod
    def _bool_value(metadata: Mapping[str, Any], *keys: str) -> bool | None:
        for key in keys:
            value = bool_or_none(metadata.get(key))
            if value is not None:
                return value
        return None
