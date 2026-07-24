"""Stable human and machine rendering for ROS validation reports."""

from __future__ import annotations

from iac_code.i18n import _
from iac_code.tools.cloud.aliyun.ros_validation.model import ValidationReport, display_path


def _severity_label(value: str) -> str:
    if value == "ERROR":
        return _("ERROR")
    if value == "WARNING":
        return _("WARNING")
    return _("LIMITATION")


def render_validation_report(report: ValidationReport, *, blocking: bool | None = None) -> str:
    if blocking is None:
        blocking = report.has_errors
    limitations = sum(item.severity.value == "LIMITATION" for item in report.diagnostics)
    title = _("ROS local validation failed") if blocking else _("ROS local validation completed")
    lines = [
        _("{}: {} errors, {} warnings, {} limitations.").format(
            title,
            report.error_count,
            report.warning_count,
            limitations,
        )
    ]
    if report.analysis_incomplete:
        lines.append(_("Analysis did not complete; this call was blocked to avoid missed errors."))
    if not report.diagnostics:
        return "\n".join(lines)
    lines.extend(("", _("Summary:")))
    for index, item in enumerate(report.diagnostics, 1):
        location = ""
        if item.source_span is not None:
            prefix = _("generated JSON ") if item.source_span.synthetic else ""
            location = _("{}line {}:").format(prefix, item.source_span.line)
        lines.append("{}. [{}] {}{}".format(index, item.code, location, item.summary))
    lines.extend(("", _("Details:")))
    for index, item in enumerate(report.diagnostics, 1):
        if item.source_span is not None:
            prefix = _("generated JSON ") if item.source_span.synthetic else ""
            location = _("{}line {}:{}").format(prefix, item.source_span.line, item.source_span.column)
        else:
            location = _("no source location")
        lines.append("{}. [{} {}] {}".format(index, _severity_label(item.severity.value), item.code, location))
        lines.append(_("   path: {}").format(display_path(item.path)))
        for related in item.related_locations:
            if related.source_span is not None:
                prefix = _("generated JSON ") if related.source_span.synthetic else ""
                related_location = _("{}line {}:{}").format(
                    prefix,
                    related.source_span.line,
                    related.source_span.column,
                )
            else:
                related_location = _("no source location")
            lines.append(
                _("   Related location ({}): {}, path: {}").format(
                    related.label,
                    related_location,
                    display_path(related.path),
                )
            )
        lines.append("   {}".format(item.detail))
        if item.expected:
            lines.append(_("   expected: {}").format(item.expected))
        if item.actual:
            lines.append(_("   actual: {}").format(item.actual))
        if item.suggestion:
            lines.append(_("   Suggestion: {}").format(item.suggestion))
    return "\n".join(lines)
