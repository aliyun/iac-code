"""Stable human and machine rendering for ROS validation reports."""

from __future__ import annotations

from iac_code.i18n import _
from iac_code.tools.cloud.aliyun.ros_validation.model import Diagnostic, ValidationReport, display_path

_CONDENSED_LIMITATION_CODES = frozenset({"ROS5304", "ROS5305"})
_CONDENSE_THRESHOLD = 10
_EXAMPLE_LIMIT = 3


def _severity_label(value: str) -> str:
    if value == "ERROR":
        return _("ERROR")
    if value == "WARNING":
        return _("WARNING")
    return _("LIMITATION")


def _render_items(diagnostics: tuple[Diagnostic, ...]) -> list[tuple[Diagnostic, ...]]:
    grouped = {code: tuple(item for item in diagnostics if item.code == code) for code in _CONDENSED_LIMITATION_CODES}
    condensed_codes = {code for code, items in grouped.items() if len(items) > _CONDENSE_THRESHOLD}
    emitted: set[str] = set()
    result: list[tuple[Diagnostic, ...]] = []
    for item in diagnostics:
        if item.code not in condensed_codes:
            result.append((item,))
            continue
        if item.code not in emitted:
            result.append(grouped[item.code])
            emitted.add(item.code)
    return result


def render_validation_report(report: ValidationReport, *, blocking: bool | None = None) -> str:
    if blocking is None:
        blocking = report.has_errors
    title = _("ROS local validation failed") if blocking else _("ROS local validation completed")
    lines = [
        _("{}: {} errors, {} warnings, {} limitations.").format(
            title,
            report.error_count,
            report.warning_count,
            report.limitation_count,
        )
    ]
    if report.analysis_incomplete:
        lines.append(_("Analysis did not complete; this call was blocked to avoid missed errors."))
    if not report.diagnostics:
        return "\n".join(lines)
    rendered_items = _render_items(report.diagnostics)
    lines.extend(("", _("Summary:")))
    for index, items in enumerate(rendered_items, 1):
        item = items[0]
        if len(items) > 1:
            lines.append(
                "{}. [{}] {}".format(
                    index,
                    item.code,
                    _("{} local-analysis limitations; details condensed.").format(len(items)),
                )
            )
            continue
        location = ""
        if item.source_span is not None:
            prefix = _("generated JSON ") if item.source_span.synthetic else ""
            location = _("{}line {}:").format(prefix, item.source_span.line)
        lines.append("{}. [{}] {}{}".format(index, item.code, location, item.summary))
    lines.extend(("", _("Details:")))
    for index, items in enumerate(rendered_items, 1):
        item = items[0]
        if len(items) > 1:
            examples = items[:_EXAMPLE_LIMIT]
            lines.append(
                "{}. [{} {}] {}".format(
                    index,
                    _severity_label(item.severity.value),
                    item.code,
                    _("{} occurrences; showing {} examples.").format(len(items), len(examples)),
                )
            )
            for example in examples:
                lines.append(_("   example path: {} — {}").format(display_path(example.path), example.summary))
            lines.append(
                _("   {} additional occurrences are available in structured diagnostics.").format(
                    len(items) - len(examples)
                )
            )
            continue
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
