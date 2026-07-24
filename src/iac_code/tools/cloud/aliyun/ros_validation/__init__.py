"""Extensible local validation for Alibaba Cloud ROS templates.

Imports are intentionally lazy: ``ros_yaml`` derives its short tags from the
function registry, while the positioned parser reuses ``ros_yaml``'s loader.
Eager package imports would therefore create a module initialization cycle.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ACTION_POLICIES",
    "FUNCTION_SPECS",
    "TEMPLATE_BODY_ACTIONS",
    "MaterializedTemplateSource",
    "RequestValidationContext",
    "RosPreflightOutcome",
    "ValidationPolicy",
    "ValidationReport",
    "attach_ros_validation",
    "outcome_from_report",
    "validate_ros_template",
]


def __getattr__(name: str) -> Any:
    if name in {"ACTION_POLICIES", "TEMPLATE_BODY_ACTIONS"}:
        from iac_code.tools.cloud.aliyun.ros_validation import action_policy

        return getattr(action_policy, name)
    if name == "FUNCTION_SPECS":
        from iac_code.tools.cloud.aliyun.ros_validation.function_specs import FUNCTION_SPECS

        return FUNCTION_SPECS
    if name in {
        "MaterializedTemplateSource",
        "RequestValidationContext",
        "ValidationPolicy",
        "ValidationReport",
    }:
        from iac_code.tools.cloud.aliyun.ros_validation import model

        return getattr(model, name)
    if name in {"RosPreflightOutcome", "attach_ros_validation", "outcome_from_report"}:
        from iac_code.tools.cloud.aliyun.ros_validation import outcome

        return getattr(outcome, name)
    if name == "validate_ros_template":
        from iac_code.tools.cloud.aliyun.ros_validation.validator import validate_ros_template

        return validate_ros_template
    raise AttributeError(name)
