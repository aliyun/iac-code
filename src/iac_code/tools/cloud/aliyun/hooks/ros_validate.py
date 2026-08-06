"""Pre-call hook for the shared, action-aware ROS local validator."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

from iac_code.i18n import _
from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.aliyun.api_hooks import before_call
from iac_code.tools.cloud.aliyun.ros_validation.action_policy import (
    TEMPLATE_BODY_ACTIONS,
    validate_action_request,
)
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
from iac_code.tools.cloud.aliyun.template_source import classify_local_template_source

_FLAT_PARAMETER = re.compile(r"^Parameters\.(\d+)\.(ParameterKey|ParameterValue)$")

_TEMPLATE_SOURCE_SUMMARIES: dict[str, Callable[[], str]] = {
    "MISSING": lambda: _("The local ROS template file does not exist."),
    "NOT_REGULAR": lambda: _("The local ROS template path is not a regular file."),
    "UNREADABLE": lambda: _("The local ROS template file cannot be read."),
    "TOO_LARGE": lambda: _("The local ROS template file exceeds the 32 MiB limit."),
    "UTF-8": lambda: _("The local ROS template file is not valid UTF-8."),
    "READ": lambda: _("The local ROS template file cannot be read."),
}

_TEMPLATE_SOURCE_SUGGESTIONS: dict[str, Callable[[], str]] = {
    "MISSING": lambda: _(
        "Check the template path for typos and write the template to that path before validating it again."
    ),
    "NOT_REGULAR": lambda: _("Point TemplateURL at the template file itself rather than a directory or device."),
    "UNREADABLE": lambda: _("Confirm that the file exists, is readable, and is saved as UTF-8."),
    "TOO_LARGE": lambda: _("Reduce the template size, or upload it and pass an OSS/HTTP TemplateURL instead."),
    "UTF-8": lambda: _("Save the template as UTF-8 text before validating it again."),
    "READ": lambda: _("Confirm that the file exists, is readable, and is saved as UTF-8."),
}


def _template_source_diagnostic_kind(error: BaseException | str) -> str:
    if isinstance(error, str):
        return error
    if isinstance(error, UnicodeError):
        return "UTF-8"
    if isinstance(error, FileNotFoundError):
        return "MISSING"
    if isinstance(error, IsADirectoryError):
        return "NOT_REGULAR"
    return "READ"


def local_template_source_error(
    error: BaseException | str,
    *,
    path: str | Path | None = None,
) -> RosPreflightOutcome:
    """Convert a local template read/decode failure into a normal ROS report.

    ``error`` may be the raised exception or a problem already classified by
    ``classify_local_template_source``. Pass ``path`` when the exception itself
    carries no filesystem cause -- ``_read_body_file`` raises a generic
    ``ApiContractError``, so the path has to be re-inspected to report why the
    template was unusable rather than emitting an unactionable message.
    """

    if path is not None and not isinstance(error, (str, OSError, UnicodeError)):
        error = classify_local_template_source(path) or error
    kind = _template_source_diagnostic_kind(error)
    detail_arg = kind if isinstance(error, str) else type(error).__name__
    summary = _TEMPLATE_SOURCE_SUMMARIES.get(kind, _TEMPLATE_SOURCE_SUMMARIES["READ"])()
    suggestion = _TEMPLATE_SOURCE_SUGGESTIONS.get(kind, _TEMPLATE_SOURCE_SUGGESTIONS["READ"])()
    diagnostic = make_diagnostic(
        code="ROS1202",
        severity=Severity.ERROR,
        category=Category.COMPATIBILITY,
        summary=summary,
        detail=_("TemplateURL/local TemplateBody failed before entering the Parser; the ROS API was not called."),
        subject="local-template-source",
        stable_args=(kind, detail_arg),
        suggestion=suggestion,
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
