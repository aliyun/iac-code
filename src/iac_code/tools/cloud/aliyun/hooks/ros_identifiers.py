"""Pre-call hook for ROS actions selected by mutually exclusive identifiers."""

from __future__ import annotations

from typing import Any

from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.aliyun.api_hooks import before_call
from iac_code.tools.cloud.aliyun.ros_validation.identifier_policy import (
    IDENTIFIER_SOURCE_ACTIONS,
    validate_identifier_request,
    validate_template_id_shape,
)
from iac_code.tools.cloud.aliyun.ros_validation.model import ValidationReport
from iac_code.tools.cloud.aliyun.ros_validation.outcome import (
    RosPreflightOutcome,
    outcome_from_report,
)


@before_call("ros", list(IDENTIFIER_SOURCE_ACTIONS))
def check_identifiers(
    product: str,
    action: str,
    params: dict[str, Any],
    *,
    context: ToolContext | None = None,
) -> RosPreflightOutcome | None:
    del product, context
    diagnostics = [
        *validate_identifier_request(action, params),
        *validate_template_id_shape(action, params),
    ]
    if not diagnostics:
        return None
    return outcome_from_report(ValidationReport.build(diagnostics, analysis_incomplete=False))
