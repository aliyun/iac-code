"""Pre-write ROS local validation for file-mutating tools.

``write_file`` persists whatever the model produced. Without a preflight the
agent can write a template whose resource types or attribute references are
invalid, and the defect only surfaces much later (or never, when the file is
consumed as a deliverable instead of being deployed). This module reuses the
shared local validator so a defective ROS template is rejected before it
reaches disk, and the agent gets actionable diagnostics instead of a silent
success.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

ROS_TEMPLATE_SUFFIXES = frozenset({".json", ".yaml", ".yml", ".template"})


def _has_ros_template_suffix(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in ROS_TEMPLATE_SUFFIXES


def _is_ros_template_mapping(data: Any) -> bool:
    """Return whether a parsed document looks like a ROS stack template.

    ``ROSTemplateFormatVersion`` is the unambiguous marker. A bare ``Resources``
    mapping is also accepted because ROS treats that section as required, but
    only when it is a mapping so that unrelated documents carrying a
    ``Resources`` list are not misread as templates.
    """

    if not isinstance(data, Mapping):
        return False
    if "ROSTemplateFormatVersion" in data:
        return True
    return isinstance(data.get("Resources"), Mapping)


def _mentions_ros_template_marker(content: str) -> bool:
    return "ROSTemplateFormatVersion" in content


def looks_like_ros_template(path: str, content: str) -> bool:
    """Return whether ``content`` should be validated as a ROS template."""

    if not content.strip() or not _has_ros_template_suffix(path):
        return False

    from iac_code.tools.cloud.aliyun.ros_validation.parser import parse_template_source

    result = parse_template_source(content, source_id=path)
    if result.template is None:
        # Syntax errors are only reported for documents that are recognizable as
        # ROS templates; otherwise every unparsable file would be blocked.
        return _mentions_ros_template_marker(content)
    return _is_ros_template_mapping(result.template.data)


def validate_template_before_write(path: str, content: str) -> Any | None:
    """Validate a pending ROS template write.

    Returns a ``RosPreflightOutcome`` when ``content`` is a ROS template, or
    ``None`` when the write is unrelated to ROS and must proceed untouched.
    """

    if not looks_like_ros_template(path, content):
        return None

    from iac_code.tools.cloud.aliyun.ros_validation.model import (
        EvaluationMode,
        MaterializedTemplateSource,
        RequestValidationContext,
        ValidationPolicy,
    )
    from iac_code.tools.cloud.aliyun.ros_validation.outcome import outcome_from_report
    from iac_code.tools.cloud.aliyun.ros_validation.validator import validate_ros_template

    report = validate_ros_template(
        MaterializedTemplateSource(
            content,
            kind="INLINE",
            origin=path,
            origin_kind="SOURCE_TEXT",
        ),
        RequestValidationContext(action="ValidateTemplate", evaluation_mode=EvaluationMode.DEPLOYMENT),
        policy=ValidationPolicy.STRICT,
    )
    return outcome_from_report(report, template_analyzed=True)
