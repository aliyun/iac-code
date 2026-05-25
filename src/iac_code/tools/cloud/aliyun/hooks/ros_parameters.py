"""Pre-call hook: expand ROS Parameters from dict format to flat format."""

from __future__ import annotations

import json
from typing import Any

from iac_code.tools.base import ToolResult
from iac_code.tools.cloud.aliyun.api_hooks import before_call

_PARAMETERS_ACTIONS = [
    "CreateStack",
    "UpdateStack",
    "PreviewStack",
    "CreateChangeSet",
    "GetTemplateEstimateCost",
    "GetTemplateSummary",
    "GetTemplateParameterConstraints",
    "CreateStackGroup",
    "UpdateStackGroup",
]


def _value_to_str(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _normalize_parameters(parameters: Any) -> list[tuple[str, str]] | None:
    """Normalize various Parameters formats to [(key, value_str), ...].

    Supported formats:
      1. dict: {"key": value, ...}
      2. list of dicts: [{"ParameterKey": "k", "ParameterValue": "v"}, ...]
    Returns None if format is unrecognized.
    """
    if isinstance(parameters, dict):
        return [(str(k), _value_to_str(v)) for k, v in parameters.items() if v is not None]
    if isinstance(parameters, list):
        result: list[tuple[str, str]] = []
        for item in parameters:
            if not isinstance(item, dict):
                return None
            key = item.get("ParameterKey")
            if key is None:
                return None
            value = item.get("ParameterValue")
            if value is None:
                continue
            result.append((str(key), _value_to_str(value)))
        return result
    return None


@before_call("ros", _PARAMETERS_ACTIONS)
def expand_parameters(product: str, action: str, params: dict[str, Any]) -> ToolResult | None:
    parameters = params.get("Parameters")
    if parameters is None:
        return None

    if any(k.startswith("Parameters.") and k.endswith(".ParameterKey") for k in params):
        return None

    pairs = _normalize_parameters(parameters)
    if pairs is None:
        return None

    del params["Parameters"]
    for i, (key, value_str) in enumerate(pairs, start=1):
        params[f"Parameters.{i}.ParameterKey"] = key
        params[f"Parameters.{i}.ParameterValue"] = value_str

    return None
