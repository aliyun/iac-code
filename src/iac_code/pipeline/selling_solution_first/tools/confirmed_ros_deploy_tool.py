"""``ros_deploy`` wrapper that refuses every action until Step 2 recorded a real user confirmation.

The wrapper adds exactly one thing to the existing selling deployment tool: a confirmation gate read
from the injected ``completion_guard_state.context_snapshot`` (design 9.1). It checks the gate in
``check_permissions`` — before any permission prompt is shown — and again in ``execute`` as a
defensive re-check, so a model that ignores the deploying prompt still cannot reach a cloud write.
Everything else (input validation, permission rules, create / wait / continue_create /
delete_and_create and stack result recording) is inherited unchanged via ``super()``.

The base class is reached through a module alias on purpose: pipeline-local tool discovery enumerates
every :class:`~iac_code.tools.base.Tool` subclass exposed by each module in ``tools/`` and registers
it under ``tool.name``. Importing ``RosDeployTool`` as a module-level attribute here would make the
original class a second resolution for ``ros_deploy`` and could overwrite this wrapper.
"""

from __future__ import annotations

from typing import Any

from iac_code.i18n import _
from iac_code.pipeline.selling.tools import ros_deploy_tool as _selling_ros_deploy
from iac_code.pipeline.selling_solution_first.hooks.deploying import evaluate_deployment_gate
from iac_code.tools.base import ToolContext, ToolResult
from iac_code.types.permissions import PermissionDecisionReason, PermissionResult


class ConfirmedRosDeployTool(_selling_ros_deploy.RosDeployTool):
    """Deploy ROS stacks only after ``materialize_selected_candidate`` confirmed the deployment."""

    def _deployment_gate_error(self) -> str:
        state = self._completion_guard_state if isinstance(self._completion_guard_state, dict) else {}
        snapshot = state.get("context_snapshot")
        if not isinstance(snapshot, dict):
            return "pipeline context snapshot is unavailable; deployment confirmation cannot be verified"
        return evaluate_deployment_gate(snapshot.get("selected_plan"))

    def _gate_message(self, error: str) -> str:
        return _(
            "Deployment is not authorized: {reason}\n"
            "Do not call ros_deploy. Use complete_step with a rollback_request to "
            "materialize_selected_candidate to obtain a valid confirmed deployment hand-off."
        ).format(reason=error)

    async def check_permissions(self, input: dict, context=None) -> PermissionResult:
        error = self._deployment_gate_error()
        if error:
            reason = PermissionDecisionReason(type="unconfirmed_ros_deployment", detail=error)
            return PermissionResult(
                behavior="deny",
                message=self._gate_message(error),
                reason=reason,
                audit=self._audit(input if isinstance(input, dict) else {}, scope="once", reason=reason),
            )
        return await super().check_permissions(input, context)

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        error = self._deployment_gate_error()
        if error:
            return ToolResult.error(self._gate_message(error))
        return await super().execute(tool_input=tool_input, context=context)
