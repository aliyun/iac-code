"""Pre-call hook rejecting ROS stack actions that lack the required StackId."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from iac_code.i18n import _
from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.aliyun.api_hooks import before_call
from iac_code.tools.cloud.aliyun.ros_validation.model import (
    Category,
    Severity,
    ValidationReport,
    make_diagnostic,
)
from iac_code.tools.cloud.aliyun.ros_validation.outcome import (
    RosPreflightOutcome,
    outcome_from_report,
)

# Actions of the locked ROS 2019-09-10 API (SDK 3.6.0) whose request model
# requires StackId and does not accept StackName as an alternative target.
STACK_ID_REQUIRED_ACTIONS = (
    "CancelStackOperation",
    "CancelUpdateStack",
    "ContinueCreateStack",
    "DeleteStack",
    "DetectStackDrift",
    "DetectStackResourceDrift",
    "GetStack",
    "GetStackPolicy",
    "GetStackResource",
    "ListChangeSets",
    "ListStackEvents",
    "ListStackResourceDrifts",
    "ListStackResources",
    "SetDeletionProtection",
    "SetStackPolicy",
    "SignalResource",
    "UpdateStack",
    "UpdateStackTemplateByResources",
)


def _present(params: Mapping[str, Any], field: str) -> bool:
    value = params.get(field)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None and value != [] and value != {}


@before_call("ros", list(STACK_ID_REQUIRED_ACTIONS))
def check_stack_identifier(
    product: str,
    action: str,
    params: dict[str, Any],
    *,
    context: ToolContext | None = None,
) -> RosPreflightOutcome | None:
    """Block a stack action when the caller holds no StackId to target it with."""

    del product, context
    if _present(params, "StackId"):
        return None
    has_stack_name = _present(params, "StackName")
    diagnostic = make_diagnostic(
        code="ROS1203",
        severity=Severity.ERROR,
        category=Category.COMPATIBILITY,
        summary=_("{} requires StackId, but the request provides only StackName.").format(action)
        if has_stack_name
        else _("{} requires StackId, but the request provides no stack identifier.").format(action),
        detail=_("This operation targets a stack by StackId only; StackName is not an accepted target."),
        subject="stack-identifier",
        stable_args=(action, "stack-name-only" if has_stack_name else "no-stack-identifier"),
        expected="StackId",
        suggestion=_(
            "Call ListStacks filtered by StackName first, read StackId from the result, then call {} with it."
        ).format(action),
    )
    return outcome_from_report(ValidationReport.build([diagnostic], analysis_incomplete=False))
