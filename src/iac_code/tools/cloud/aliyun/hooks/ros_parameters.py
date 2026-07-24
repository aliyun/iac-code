"""ROS Parameters adapters for the generic RPC request path."""

from __future__ import annotations

import json
from typing import Any

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


def normalize_ros_parameters(action: str, params: dict[str, Any]) -> None:
    """Materialize ROS ``Parameters`` into the canonical flat RPC shape."""

    if action not in _PARAMETERS_ACTIONS:
        return
    parameters = params.get("Parameters")
    if parameters is None:
        return
    if any(key.startswith("Parameters.") and key.endswith(".ParameterKey") for key in params):
        return
    pairs = _normalize_parameters(parameters)
    if pairs is None:
        return

    del params["Parameters"]
    for index, (key, value_str) in enumerate(pairs, start=1):
        params[f"Parameters.{index}.ParameterKey"] = key
        params[f"Parameters.{index}.ParameterValue"] = value_str


def expand_parameters(product: str, action: str, params: dict[str, Any]) -> None:
    """Compatibility wrapper for callers that explicitly request RPC materialization.

    This function is intentionally not a pre-call hook: stage-zero validation
    must be read-only so invocation bindings keep describing the approved input.
    """

    del product
    normalize_ros_parameters(action, params)
