"""Pre-call hook for the shared, action-aware ROS local validator."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import yaml

from iac_code.i18n import _
from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.aliyun.api_hooks import before_call
from iac_code.tools.cloud.aliyun.ros_validation.action_policy import (
    TEMPLATE_BODY_ACTIONS,
    validate_action_request,
)
from iac_code.tools.cloud.aliyun.ros_validation.identifier_policy import validate_template_id_shape
from iac_code.tools.cloud.aliyun.ros_validation.model import (
    Category,
    MaterializedTemplateSource,
    Severity,
    ValidationPolicy,
    ValidationReport,
    make_diagnostic,
)
from iac_code.tools.cloud.aliyun.ros_validation.outcome import (
    RosPreflightOutcome,
    outcome_from_report,
)
from iac_code.tools.cloud.aliyun.ros_validation.parser import parse_template_source
from iac_code.tools.cloud.aliyun.ros_validation.validator import validate_ros_template

_FLAT_PARAMETER = re.compile(r"^Parameters\.(\d+)\.(ParameterKey|ParameterValue)$")


def local_template_source_error(error: BaseException) -> RosPreflightOutcome:
    """Convert an allowed local-file read/decode failure into a normal ROS report."""

    kind = "UTF-8" if isinstance(error, UnicodeError) else "READ"
    diagnostic = make_diagnostic(
        code="ROS1202",
        severity=Severity.ERROR,
        category=Category.COMPATIBILITY,
        summary=_("The local ROS template file cannot be read.")
        if kind == "READ"
        else _("The local ROS template file is not valid UTF-8."),
        detail=_("TemplateURL/local TemplateBody failed before entering the Parser; the ROS API was not called."),
        subject="local-template-source",
        stable_args=(kind, type(error).__name__),
        suggestion=_("Confirm that the file exists, is readable, and is saved as UTF-8."),
    )
    return outcome_from_report(ValidationReport.build([diagnostic], analysis_incomplete=False))


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False
    return True


def _format_yaml_error(exc: yaml.YAMLError, text: str) -> str:
    del text
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", str(exc))
    if mark is None:
        return _("Template YAML syntax error: {}").format(problem)
    return _("Template YAML syntax error (line {line}, column {col}): {problem}").format(
        line=mark.line + 1,
        col=mark.column + 1,
        problem=problem,
    )


def _format_json_error(exc: json.JSONDecodeError, text: str) -> str:
    del text
    return _("Template JSON syntax error (line {line}, column {col}): {msg}").format(
        line=exc.lineno,
        col=exc.colno,
        msg=exc.msg,
    )


def _parse_template(template_body: str) -> tuple[dict | None, str | None]:
    result = parse_template_source(template_body)
    if result.template is None:
        detail = result.diagnostics[0].detail if result.diagnostics else _("Template parse failed")
        return None, _("Template YAML syntax error: {}").format(detail)
    if not isinstance(result.template.data, dict):
        return None, _("Template parse result is not an object (dict), please check the template format")
    return result.template.data, None


def _validate_structure(data: dict) -> list[str]:
    """Compatibility helper retained for callers of the previous hook module."""

    body = json.dumps(data, ensure_ascii=False)
    from iac_code.tools.cloud.aliyun.ros_validation.model import EvaluationMode, RequestValidationContext

    report = validate_ros_template(
        MaterializedTemplateSource(body, origin_kind="SYNTHETIC_ADAPTER"),
        RequestValidationContext(action="ValidateTemplate", evaluation_mode=EvaluationMode.DEPLOYMENT),
    )
    return [
        " ".join(part for part in (item.summary, item.detail, item.suggestion) if part)
        for item in report.diagnostics
        if item.severity.value == "ERROR"
    ]


def _parameter_bindings(params: Mapping[str, Any]) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    raw = params.get("Parameters")
    if isinstance(raw, Mapping):
        result.update(raw)
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, Mapping) or "ParameterKey" not in item:
                continue
            result[item["ParameterKey"]] = item.get("ParameterValue")
    flat: dict[int, dict[str, Any]] = {}
    for key, value in params.items():
        match = _FLAT_PARAMETER.match(key)
        if match:
            flat.setdefault(int(match.group(1)), {})[match.group(2)] = value
    for item in flat.values():
        if "ParameterKey" in item:
            result[item["ParameterKey"]] = item.get("ParameterValue")
    return result


@before_call("ros", list(TEMPLATE_BODY_ACTIONS))
def check_template(
    product: str,
    action: str,
    params: dict[str, Any],
    *,
    context: ToolContext | None = None,
) -> RosPreflightOutcome | None:
    del product
    trusted_context = context.trusted_ros_account_context if context is not None else None
    action_policy, request_diagnostics, active_body = validate_action_request(
        action,
        params,
        trusted_ros_account_context=trusted_context,
    )
    if action_policy is None:
        return None
    diagnostics = list(request_diagnostics)
    diagnostics.extend(validate_template_id_shape(action, params))
    analysis_incomplete = False
    if active_body:
        template_body = params.get("TemplateBody")
        origin_kind = "SOURCE_TEXT"
        if isinstance(template_body, Mapping):
            template_body = json.dumps(template_body, ensure_ascii=False)
            origin_kind = "SYNTHETIC_ADAPTER"
        request = action_policy.request_context(params, trusted_ros_account_context=trusted_context)
        report = validate_ros_template(
            MaterializedTemplateSource(
                template_body,
                kind="INLINE",
                origin="TemplateBody",
                origin_kind=origin_kind,
            ),
            request,
            policy=ValidationPolicy.STRICT,
            parameter_bindings=_parameter_bindings(params),
        )
        diagnostics.extend(report.diagnostics)
        analysis_incomplete = report.analysis_incomplete
    final_report = ValidationReport.build(diagnostics, analysis_incomplete=analysis_incomplete)
    return outcome_from_report(final_report, template_analyzed=active_body)
