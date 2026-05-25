"""Pre-call hook: validate ROS template syntax and structure before API calls."""

from __future__ import annotations

import json
from typing import Any

import yaml

from iac_code.i18n import _
from iac_code.tools.base import ToolResult
from iac_code.tools.cloud.aliyun.api_hooks import before_call
from iac_code.tools.cloud.aliyun.ros_yaml import ros_yaml_load

_RESOURCE_TYPE_CORRECTIONS: dict[str, str] = {
    "ALIYUN::VPC::VPC": "ALIYUN::ECS::VPC",
    "ALIYUN::VPC::VSwitch": "ALIYUN::ECS::VSwitch",
}

_TERRAFORM_TRANSFORM_PREFIXES = ("Aliyun::Terraform-", "Aliyun::OpenTofu-")

_TEMPLATE_BODY_ACTIONS = [
    "ValidateTemplate",
    "CreateStack",
    "UpdateStack",
    "PreviewStack",
    "CreateChangeSet",
    "GetTemplateEstimateCost",
    "GetTemplateSummary",
    "GenerateTemplatePolicy",
    "GetTemplateParameterConstraints",
    "CreateStackGroup",
    "UpdateStackGroup",
    "CreateTemplate",
    "UpdateTemplate",
]


def _is_json(text: str) -> bool:
    return text.lstrip().startswith("{")


def _format_yaml_error(exc: yaml.YAMLError, text: str) -> str:
    lines = text.splitlines()
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", str(exc))
    if mark is None:
        return _("Template YAML syntax error: {}").format(problem)
    line_num = mark.line + 1
    col_num = mark.column + 1
    context_lines: list[str] = []
    start = max(0, mark.line - 2)
    end = min(len(lines), mark.line + 2)
    for i in range(start, end):
        prefix = "> " if i == mark.line else "  "
        context_lines.append("{}{:>4} | {}".format(prefix, i + 1, lines[i]))
        if i == mark.line:
            context_lines.append("  " + " " * (4 + 3 + mark.column) + "^")
    context_str = "\n".join(context_lines)
    return _("Template YAML syntax error (line {line}, column {col}): {problem}\nContext:\n{context}").format(
        line=line_num, col=col_num, problem=problem, context=context_str
    )


def _format_json_error(exc: json.JSONDecodeError, text: str) -> str:
    return _("Template JSON syntax error (line {line}, column {col}): {msg}").format(
        line=exc.lineno, col=exc.colno, msg=exc.msg
    )


def _parse_template(template_body: str) -> tuple[dict | None, str | None]:
    """Parse template body, return (data, error_message). One of them is None."""
    if _is_json(template_body):
        try:
            data = json.loads(template_body)
        except json.JSONDecodeError as e:
            return None, _format_json_error(e, template_body)
    else:
        try:
            data = ros_yaml_load(template_body)
        except yaml.YAMLError as e:
            return None, _format_yaml_error(e, template_body)
    if not isinstance(data, dict):
        fmt = "JSON" if _is_json(template_body) else "YAML"
        return None, _("Template {fmt} parse result is not an object (dict), please check the template format").format(
            fmt=fmt
        )
    return data, None


def _is_terraform(data: dict) -> bool:
    transform = data.get("Transform", "")
    values = transform if isinstance(transform, list) else [transform]
    return any(isinstance(v, str) and v.startswith(_TERRAFORM_TRANSFORM_PREFIXES) for v in values)


def _validate_structure(data: dict) -> list[str]:
    """Validate ROS template structure. Return list of error messages."""
    errors: list[str] = []
    is_tf = _is_terraform(data)

    if "ROSTemplateFormatVersion" not in data:
        errors.append(
            _("Template is missing ROSTemplateFormatVersion (ROS templates must include this field, e.g. '2015-09-01')")
        )

    if not is_tf:
        resources = data.get("Resources")
        if resources is None:
            errors.append(_("Template is missing Resources (ROS templates must include Resources)"))
        elif not isinstance(resources, dict):
            errors.append(_("Resources must be an object (dict), current type is {}").format(type(resources).__name__))
        else:
            for name, resource in resources.items():
                if not isinstance(resource, dict):
                    errors.append(
                        _("Resource '{name}' definition must be an object (dict), current type is {type}").format(
                            name=name, type=type(resource).__name__
                        )
                    )
                    continue
                if "Type" not in resource:
                    errors.append(_("Resource '{name}' is missing the Type field").format(name=name))
                    continue
                rtype = resource["Type"]
                if rtype in _RESOURCE_TYPE_CORRECTIONS:
                    correct = _RESOURCE_TYPE_CORRECTIONS[rtype]
                    errors.append(
                        _("Resource '{name}' has incorrect type '{wrong}', should be '{correct}'").format(
                            name=name, wrong=rtype, correct=correct
                        )
                    )

    return errors


@before_call("ros", _TEMPLATE_BODY_ACTIONS)
def check_template(product: str, action: str, params: dict[str, Any]) -> ToolResult | None:
    template_body = params.get("TemplateBody", "")
    if not template_body:
        return None

    data, syntax_error = _parse_template(template_body)
    if syntax_error:
        return ToolResult.error(syntax_error)

    assert data is not None
    structure_errors = _validate_structure(data)
    if structure_errors:
        msg = (
            _("Template structure validation found the following issues, please fix and retry:")
            + "\n"
            + "\n".join("  - {}".format(e) for e in structure_errors)
        )
        return ToolResult.error(msg)

    return None
