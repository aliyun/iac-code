"""Minimal JSON Schema relaxation used only for Qwen wire copies."""

from __future__ import annotations

import copy
from typing import Any

_SCHEMA_MAP_KEYS = frozenset(
    {
        "properties",
        "$defs",
        "definitions",
        "patternProperties",
        "dependencies",
        "dependentSchemas",
        "dependentRequired",
    }
)
_SCHEMA_CHILD_KEYS = frozenset(
    {"items", "contains", "additionalProperties", "propertyNames", "if", "then", "else", "not"}
)
_SCHEMA_LIST_KEYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})


def relax_qwen_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    return _relax_schema_node(copy.deepcopy(schema))


def _relax_schema_node(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    result = dict(node)
    result.pop("$schema", None)
    result.pop("$id", None)
    result.pop("uniqueItems", None)
    properties = result.get("properties")
    required = result.get("required")
    if (
        result.get("additionalProperties") is False
        and isinstance(properties, dict)
        and set(properties) - set(required if isinstance(required, list) else [])
    ):
        result.pop("additionalProperties", None)
    for key in _SCHEMA_MAP_KEYS:
        value = result.get(key)
        if isinstance(value, dict):
            result[key] = {name: _relax_schema_node(child) for name, child in value.items()}
    for key in _SCHEMA_CHILD_KEYS:
        value = result.get(key)
        if isinstance(value, dict):
            result[key] = _relax_schema_node(value)
    for key in _SCHEMA_LIST_KEYS:
        value = result.get(key)
        if isinstance(value, list):
            result[key] = [_relax_schema_node(child) for child in value]
    return result
