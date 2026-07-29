"""Current-invocation propagation of an already generated ROS report."""

from __future__ import annotations

from dataclasses import dataclass

from iac_code.i18n import _
from iac_code.tools.base import ToolResult
from iac_code.tools.cloud.aliyun.ros_validation.model import ValidationReport
from iac_code.tools.cloud.aliyun.ros_validation.renderer import render_validation_report


@dataclass(frozen=True)
class RosPreflightOutcome:
    report: ValidationReport
    blocking_result: ToolResult | None
    template_analyzed: bool = False

    # Compatibility for callers/tests that previously consumed ToolResult
    # directly from check_template().
    @property
    def is_error(self) -> bool:
        return self.blocking_result is not None

    @property
    def content(self) -> str:
        if self.blocking_result is not None:
            return self.blocking_result.content
        return render_validation_report(self.report, blocking=False)


def outcome_from_report(report: ValidationReport, *, template_analyzed: bool = False) -> RosPreflightOutcome:
    blocking = report.has_errors or report.analysis_incomplete
    result = (
        ToolResult(
            content=render_validation_report(report, blocking=True),
            is_error=True,
            metadata={"ros_validation": report.to_dict()},
        )
        if blocking
        else None
    )
    return RosPreflightOutcome(report, result, template_analyzed=template_analyzed)


def _merge_report_payload(existing: dict, current: dict) -> dict:
    existing_diagnostics = existing.get("diagnostics")
    current_diagnostics = current.get("diagnostics")
    if not isinstance(existing_diagnostics, list) or not isinstance(current_diagnostics, list):
        merged = dict(existing)
        merged.update(current)
        return merged

    diagnostics: list[dict] = []
    diagnostic_ids: set[str] = set()
    for item in [*existing_diagnostics, *current_diagnostics]:
        if not isinstance(item, dict):
            continue
        identifier = item.get("diagnostic_id")
        if isinstance(identifier, str):
            if identifier in diagnostic_ids:
                continue
            diagnostic_ids.add(identifier)
        diagnostics.append(dict(item))

    counts_by_code: dict[str, int] = {}
    for item in diagnostics:
        code = item.get("code")
        if isinstance(code, str):
            counts_by_code[code] = counts_by_code.get(code, 0) + 1

    merged = dict(existing)
    merged.update(current)
    merged.update(
        {
            "diagnostics": diagnostics,
            "error_count": sum(item.get("severity") == "ERROR" for item in diagnostics),
            "warning_count": sum(item.get("severity") == "WARNING" for item in diagnostics),
            "limitation_count": sum(item.get("severity") == "LIMITATION" for item in diagnostics),
            "counts_by_code": counts_by_code,
            "analysis_incomplete": bool(existing.get("analysis_incomplete"))
            or bool(current.get("analysis_incomplete")),
        }
    )
    return merged


def attach_ros_validation(result: ToolResult, outcome: RosPreflightOutcome | None) -> ToolResult:
    if outcome is None or not outcome.report.diagnostics:
        return result
    report_dict = outcome.report.to_dict()
    existing_metadata = dict(result.metadata or {})
    existing = existing_metadata.get("ros_validation")
    if isinstance(existing, dict):
        existing_ids = {item.get("diagnostic_id") for item in existing.get("diagnostics", []) if isinstance(item, dict)}
        if all(item.diagnostic_id in existing_ids for item in outcome.report.diagnostics):
            return result
        report_dict = _merge_report_payload(existing, report_dict)
    existing_metadata["ros_validation"] = report_dict
    rendered = render_validation_report(outcome.report, blocking=False)
    marker = _("\n\n---\nROS local preflight diagnostics:\n")
    content = result.content if rendered in result.content else result.content + marker + rendered
    return ToolResult(
        content=content,
        is_error=result.is_error,
        new_messages=list(result.new_messages),
        context_modifier=result.context_modifier,
        metadata=existing_metadata,
    )
