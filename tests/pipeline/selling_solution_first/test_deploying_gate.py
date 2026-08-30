"""部署门禁与 confirmed ``ros_deploy`` wrapper（设计文档 §18.5）。

Step 3 只允许在 Step 2 记录了真实用户确认之后写云资源：门禁是纯函数、`on_enter` 只就地
标注 ``selected_plan``，wrapper 在权限询问之前与 ``execute`` 里各拒绝一次，门禁通过时完整
委托既有 ``RosDeployTool``。
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.selling.tools import ros_deploy_tool as selling_ros_deploy
from iac_code.pipeline.selling_solution_first.hooks import deploying as deploying_hook
from iac_code.pipeline.selling_solution_first.tools.confirmed_ros_deploy_tool import ConfirmedRosDeployTool
from iac_code.tools.base import ToolContext, ToolResult
from iac_code.types.permissions import PermissionResult

CONFIRMED_PLAN = {
    "status": "confirmed",
    "continue_pipeline": True,
    "deployment_confirmed": True,
    "selection_valid": True,
    "template_url": "templates/solution-a.yml",
    "selected_candidate": {"name": "方案A"},
    "selected_candidate_result": {"failed": False},
    "effective_deployment_parameters": {"InstanceType": "ecs.g7.large"},
}


def _plan(**overrides):
    plan = copy.deepcopy(CONFIRMED_PLAN)
    plan.update(overrides)
    return plan


def _tool(plan, *, snapshot_present: bool = True):
    state = {}
    if snapshot_present:
        state["context_snapshot"] = {"selected_plan": plan}
    return ConfirmedRosDeployTool(completion_guard_state=state)


def _create_input():
    return {
        "action": "create",
        "stack_name": "solution-a",
        "template_url": "templates/solution-a.yml",
        "region_id": "cn-hangzhou",
        "parameters": {"InstanceType": "ecs.g7.large"},
    }


def _pipeline_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "src" / "iac_code" / "pipeline" / "selling_solution_first"


class TestDeployingPromptContract:
    def test_prompt_adapts_the_new_pipeline_gate_context_and_rollback_targets(self):
        text = (_pipeline_dir() / "prompts" / "deploying.md").read_text(encoding="utf-8")

        assert "{selected_plan}" in text
        assert "专用部署确认交互" in text
        assert "deployment_gate_valid" in text
        assert "{selected_plan.template_url}" in text
        assert "materialize_selected_candidate" in text
        assert "solution_planning_and_selection" in text

    def test_skill_separates_environment_errors_from_template_errors(self):
        text = (_pipeline_dir() / "skills" / "iac-aliyun-deploying" / "SKILL.md").read_text(encoding="utf-8")

        assert "**环境类错误**" in text
        assert "不要用标准库 PyYAML 自查模板" in text

    def test_prompt_does_not_copy_the_shared_deploying_skill(self):
        text = (_pipeline_dir() / "prompts" / "deploying.md").read_text(encoding="utf-8")

        assert len(text.splitlines()) <= 50
        for duplicated_section in (
            "## 可用性查询",
            "## 部署前参数补全",
            "## StackName",
            "## 创建流程",
            "## 失败恢复",
        ):
            assert duplicated_section not in text


class TestDeploymentGate:
    def test_confirmed_plan_passes(self):
        assert deploying_hook.evaluate_deployment_gate(_plan()) == ""

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"status": "cancelled"}, "status must be 'confirmed'"),
            ({"status": "reselect_requested"}, "status must be 'confirmed'"),
            ({"deployment_confirmed": False}, "did not confirm deployment"),
            ({"deployment_confirmed": "true"}, "did not confirm deployment"),
            ({"continue_pipeline": False}, "continue_pipeline is not true"),
            ({"selection_valid": False}, "selection_valid is not true"),
            ({"template_url": ""}, "template_url is empty"),
            ({"template_url": "   "}, "template_url is empty"),
            ({"template_url": None}, "template_url is empty"),
            ({"selected_candidate_result": {"failed": True}}, "failed is true"),
        ],
    )
    def test_unconfirmed_invalid_or_template_less_plans_are_rejected(self, overrides, expected):
        error = deploying_hook.evaluate_deployment_gate(_plan(**overrides))

        assert expected in error

    @pytest.mark.parametrize("preview_ready", [False, None])
    def test_stale_preview_does_not_block_the_normal_deployment_path(self, preview_ready):
        """参数在 Step 2 一次确认里变更后 ``preview_ready_for_create`` 会变成 false。

        门禁只校验真实的用户确认，不把旧 Preview 结论当作部署前置条件，Step 3 继续走既有常规
        部署校验路径（模板/参数校验由 ``ros_deploy`` 自身完成）。
        """
        assert deploying_hook.evaluate_deployment_gate(_plan(preview_ready_for_create=preview_ready)) == ""

    @pytest.mark.parametrize("missing", [None, "confirmed", [], 0])
    def test_non_dict_plan_is_rejected(self, missing):
        error = deploying_hook.evaluate_deployment_gate(missing)

        assert "selected_plan is missing" in error


class TestOnEnter:
    def test_annotates_gate_fields_in_place_without_new_context_fields(self):
        context = PipelineContext({"solution_selection": [], "selected_plan": [], "deployment": []})
        plan = _plan()
        context.set_conclusion("selected_plan", plan)

        deploying_hook.on_enter(context)

        stored = context.get_conclusion("selected_plan")
        assert stored is plan
        assert stored["deployment_gate_valid"] is True
        assert stored["deployment_gate_error"] == ""
        # 只在 selected_plan 内部就地归一化，不新增顶层 context field。
        assert set(context.snapshot()) == {"selected_plan"}
        assert context.get_conclusion("deployment") is None

    def test_records_the_blocking_reason_for_an_unconfirmed_plan(self):
        context = PipelineContext({"solution_selection": [], "selected_plan": [], "deployment": []})
        context.set_conclusion("selected_plan", _plan(deployment_confirmed=False))

        deploying_hook.on_enter(context)

        stored = context.get_conclusion("selected_plan")
        assert stored["deployment_gate_valid"] is False
        assert "did not confirm deployment" in stored["deployment_gate_error"]

    def test_missing_plan_does_not_raise_and_leaves_context_untouched(self):
        context = PipelineContext({"solution_selection": [], "selected_plan": [], "deployment": []})

        deploying_hook.on_enter(context)

        assert context.get_conclusion("selected_plan") is None

    def test_reuses_the_existing_selling_resource_and_cleanup_hooks(self):
        from iac_code.pipeline.selling.hooks import deploying as selling_deploying

        assert deploying_hook.on_resource_observed is selling_deploying.on_resource_observed
        assert deploying_hook.on_rollback_cleanup_required is selling_deploying.on_rollback_cleanup_required
        assert deploying_hook.contains_redaction_placeholder is selling_deploying.contains_redaction_placeholder


class TestConfirmedWrapperRejections:
    @pytest.mark.asyncio
    async def test_denies_before_the_permission_prompt_when_unconfirmed(self):
        tool = _tool(_plan(deployment_confirmed=False))

        decision = await tool.check_permissions(_create_input())

        assert isinstance(decision, PermissionResult)
        assert decision.behavior == "deny"
        assert "Deployment is not authorized" in (decision.message or "")
        assert "did not confirm deployment" in (decision.message or "")
        assert decision.reason is not None
        assert decision.reason.type == "unconfirmed_ros_deployment"
        assert decision.audit is not None

    @pytest.mark.asyncio
    async def test_execute_rejects_even_when_permissions_were_bypassed(self):
        tool = _tool(_plan(selection_valid=False))

        result = await tool.execute(tool_input=_create_input(), context=ToolContext(cwd="/proj"))

        assert result.is_error is True
        assert "Deployment is not authorized" in result.content
        assert "selection_valid is not true" in result.content
        assert "rollback_request to materialize_selected_candidate" in result.content

    @pytest.mark.asyncio
    async def test_missing_context_snapshot_blocks_deployment(self):
        tool = _tool(None, snapshot_present=False)

        decision = await tool.check_permissions(_create_input())
        result = await tool.execute(tool_input=_create_input(), context=ToolContext(cwd="/proj"))

        assert decision.behavior == "deny"
        assert "context snapshot is unavailable" in (decision.message or "")
        assert result.is_error is True
        assert "context snapshot is unavailable" in result.content

    @pytest.mark.asyncio
    async def test_no_action_bypasses_the_gate(self, monkeypatch):
        calls: list[str] = []

        async def fake_execute(self, *, tool_input, context):
            calls.append(tool_input.get("action"))
            return ToolResult.success("{}")

        monkeypatch.setattr(selling_ros_deploy.RosDeployTool, "execute", fake_execute)
        tool = _tool(_plan(status="cancelled", deployment_confirmed=False, continue_pipeline=False))

        for action, payload in (
            ("create", _create_input()),
            ("continue_create", {"action": "continue_create", "stack_id": "s-1", "template_url": "t.yml"}),
            (
                "delete_and_create",
                {"action": "delete_and_create", "stack_id": "s-1", "stack_name": "n", "template_url": "t.yml"},
            ),
            ("wait", {"action": "wait", "stack_id": "s-1"}),
        ):
            result = await tool.execute(tool_input=payload, context=ToolContext(cwd="/proj"))
            assert result.is_error is True, action
            assert "Deployment is not authorized" in result.content

        assert calls == []


class TestConfirmedWrapperDelegation:
    @pytest.mark.asyncio
    async def test_confirmed_plan_delegates_permissions_and_execution_unchanged(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_check_permissions(self, input, context=None):
            seen["permission_input"] = input
            return PermissionResult(behavior="allow")

        async def fake_execute(self, *, tool_input, context):
            seen["execute_input"] = tool_input
            seen["execute_context"] = context
            return ToolResult.success('{"stack_id": "stack-real", "status": "CREATE_COMPLETE"}')

        monkeypatch.setattr(selling_ros_deploy.RosDeployTool, "check_permissions", fake_check_permissions)
        monkeypatch.setattr(selling_ros_deploy.RosDeployTool, "execute", fake_execute)
        tool = _tool(_plan())
        tool_context = ToolContext(cwd="/proj")

        decision = await tool.check_permissions(_create_input())
        result = await tool.execute(tool_input=_create_input(), context=tool_context)

        assert decision.behavior == "allow"
        assert seen["permission_input"] == _create_input()
        assert seen["execute_input"] == _create_input()
        assert seen["execute_context"] is tool_context
        assert result.is_error is False
        assert "CREATE_COMPLETE" in result.content

    @pytest.mark.asyncio
    async def test_stale_preview_still_delegates_to_the_existing_deploy_tool(self, monkeypatch):
        """``preview_ready_for_create=false`` 不影响 Step 3：权限与执行照旧委托既有部署工具。"""

        async def fake_check_permissions(self, input, context=None):
            return PermissionResult(behavior="allow")

        async def fake_execute(self, *, tool_input, context):
            return ToolResult.success('{"stack_id": "stack-real", "status": "CREATE_COMPLETE"}')

        monkeypatch.setattr(selling_ros_deploy.RosDeployTool, "check_permissions", fake_check_permissions)
        monkeypatch.setattr(selling_ros_deploy.RosDeployTool, "execute", fake_execute)
        tool = _tool(_plan(preview_ready_for_create=False))

        decision = await tool.check_permissions(_create_input())
        result = await tool.execute(tool_input=_create_input(), context=ToolContext(cwd="/proj"))

        assert decision.behavior == "allow"
        assert result.is_error is False
        assert "CREATE_COMPLETE" in result.content

    def test_wrapper_inherits_the_existing_tool_surface(self):
        tool = _tool(_plan())

        assert tool.name == "ros_deploy"
        assert isinstance(tool, selling_ros_deploy.RosDeployTool)
        # 输入契约、超时与动作集合都来自既有实现，未在 wrapper 里重写。
        assert type(tool).input_schema is selling_ros_deploy.RosDeployTool.input_schema
        assert tool.timeout == selling_ros_deploy.RosDeployTool().timeout
