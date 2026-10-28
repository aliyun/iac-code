"""Disallow explicit ROS stack roles while ROS agent awareness is enabled."""

from __future__ import annotations

import os
from typing import Any

from iac_code.i18n import _
from iac_code.tools.base import ToolResult
from iac_code.tools.cloud.aliyun.api_hooks import before_call


@before_call(
    "ros",
    [
        "CreateStack",
        "UpdateStack",
        "ContinueCreateStack",
        "DeleteStack",
        "CreateChangeSet",
        "ListStackOperationRisks",
    ],
)
def reject_ram_role_name(product: str, action: str, params: dict[str, Any]) -> ToolResult | None:
    del product
    enabled = os.environ.get("IAC_CODE_ROS_AGENT_AWARENESS", "").strip().casefold() in {"1", "true", "yes", "on"}
    if enabled and "RamRoleName" in params:
        return ToolResult.error(
            _("Fields are not supported for action '{action}': {fields}").format(
                action=action,
                fields="RamRoleName",
            )
        )
    return None
