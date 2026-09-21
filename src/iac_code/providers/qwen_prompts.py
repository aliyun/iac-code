"""Small Qwen tool-use hints generated only from tools available this turn."""

from __future__ import annotations

import json

from iac_code.agent.system_prompt import DYNAMIC_BOUNDARY
from iac_code.providers.base import ToolDefinition
from iac_code.providers.model_family import normalized_model_name

_START = "<!-- iac-code:qwen-tools:start -->"
_END = "<!-- iac-code:qwen-tools:end -->"
_MAX_HINT_CHARS = 900


def prepare_qwen_system_prompt(system: str, model: str, tools: list[ToolDefinition] | None) -> str:
    if not tools or _START in system:
        return system
    names = [tool.name for tool in tools if tool.name]
    if not names:
        return system
    example_tool = next(tool for tool in tools if tool.name)
    example_name = example_tool.name
    properties = example_tool.input_schema.get("properties")
    parameter_name = next(iter(properties), None) if isinstance(properties, dict) else None
    normalized = normalized_model_name(model)
    if "coder" in normalized:
        parameter = f"\n<parameter={parameter_name}>VALUE</parameter>" if isinstance(parameter_name, str) else ""
        example = f"<tool_call><function={example_name}>{parameter}</function></tool_call>"
    elif "-vl" in normalized or normalized.endswith("vl") or "qwen-vl" in normalized:
        arguments = {parameter_name: "VALUE"} if isinstance(parameter_name, str) else {}
        example = (
            "<tool_call>"
            + json.dumps({"name": example_name, "arguments": arguments}, ensure_ascii=False, separators=(",", ":"))
            + "</tool_call>"
        )
    else:
        example = json.dumps(
            {
                "name": example_name,
                "arguments": {parameter_name: "VALUE"} if isinstance(parameter_name, str) else {},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    hint = (
        f"{_START}\nWhen a tool is needed, use the native function-calling channel and only supplied schemas. "
        f"Model-style format illustration: {example}\n{_END}"
    )
    if len(hint) > _MAX_HINT_CHARS:
        hint = f"{_START}\nUse only supplied native tools; for example, {example_name!r}.\n{_END}"
    if DYNAMIC_BOUNDARY in system:
        static, dynamic = system.split(DYNAMIC_BOUNDARY, 1)
        return f"{static}{DYNAMIC_BOUNDARY}\n\n{hint}{dynamic}"
    separator = "\n\n" if system else ""
    return f"{system}{separator}{hint}"
