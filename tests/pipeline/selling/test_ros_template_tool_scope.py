from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.loader import load_pipeline_dir
from iac_code.pipeline.engine.step_executor import StepExecutor
from iac_code.pipeline.engine.step_spec import StepSpec
from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from iac_code.types.permissions import PermissionResult, ToolPermissionContext

ROS_TEMPLATE_TOOLS = {
    "ros_validate_template",
    "ros_get_template_parameter_constraints",
    "ros_preview_template",
    "ros_estimate_template_cost",
}
ROS_CONSOLE_LIFECYCLE_TOOLS = {
    "ros_stack_group",
    "ros_template",
    "ros_template_scratch",
    "ros_diagnostic",
    "ros_resource_type_registration",
    "ros_tag",
}


def _selling_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "src" / "iac_code" / "pipeline" / "selling"


def _step_by_id(steps: list[StepSpec], step_id: str) -> StepSpec:
    return next(step for step in steps if step.step_id == step_id)


class _NamedTool(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, *, tool_input: dict, context):
        raise AssertionError("test tool should not execute")


def _registry_for_step(
    loaded,
    step: StepSpec,
    *,
    base_registry: ToolRegistry | None = None,
    aliyun_delegated_executor_factory=None,
):
    executor = StepExecutor(
        provider_manager=MagicMock(),
        base_tool_registry=base_registry or ToolRegistry(),
        pipeline=loaded,
        pipeline_dir=_selling_dir(),
        aliyun_delegated_executor_factory=aliyun_delegated_executor_factory,
    )
    return executor._build_step_tools(step, PipelineContext(loaded.context_dependencies))


class _DelegatedExecutor:
    def __init__(self, action: str) -> None:
        self.action = action
        self.permission_contexts: list[ToolPermissionContext] = []
        self.execution_contexts: list[ToolContext] = []

    async def check_permissions(self, tool_input: dict, context: ToolPermissionContext) -> PermissionResult:
        self.permission_contexts.append(context)
        return PermissionResult(behavior="allow")

    async def execute(self, tool_input: dict, context: ToolContext) -> ToolResult:
        self.execution_contexts.append(context)
        return ToolResult(
            content=json.dumps({"Action": self.action, "TemplateURL": tool_input["template_url"]}),
            metadata={
                "aliyun_http": {
                    "contract_version": "aliyun_body_v1",
                    "product": "ros",
                    "version": "2019-09-10",
                    "action": self.action,
                    "status": 200,
                    "status_class": "2xx",
                    "response_mode": "json",
                    "body_format": "json",
                }
            },
        )


def test_ros_template_tools_are_only_injected_into_matching_pipeline_steps(monkeypatch):
    monkeypatch.setenv("IAC_CODE_PIPELINE_SELLING_ENABLE_REVIEWING", "true")
    loaded = load_pipeline_dir(_selling_dir())
    evaluate_steps = loaded.sub_pipelines["evaluate_candidate"].steps

    expected_by_step = {
        "template_generating": {"ros_validate_template"},
        "reviewing": {"ros_validate_template"},
        "cost_estimating": ROS_TEMPLATE_TOOLS,
        "deploying": {"ros_validate_template", "ros_get_template_parameter_constraints"},
    }

    for step_id, expected_tools in expected_by_step.items():
        step = (
            _step_by_id(evaluate_steps, step_id)
            if step_id in {"template_generating", "reviewing", "cost_estimating"}
            else _step_by_id(loaded.steps, step_id)
        )
        registry = _registry_for_step(loaded, step)
        assert {name for name in ROS_TEMPLATE_TOOLS if registry.get(name) is not None} == expected_tools

    for step_id in ["intent_parsing", "architecture_planning", "evaluate_candidates", "confirm_and_select"]:
        registry = _registry_for_step(loaded, _step_by_id(loaded.steps, step_id))
        assert all(registry.get(name) is None for name in ROS_TEMPLATE_TOOLS)


@pytest.mark.asyncio
async def test_selling_cost_step_executes_all_delegated_ros_template_tools(monkeypatch):
    monkeypatch.setenv("IAC_CODE_PIPELINE_SELLING_ENABLE_REVIEWING", "true")
    loaded = load_pipeline_dir(_selling_dir())
    step = _step_by_id(loaded.sub_pipelines["evaluate_candidate"].steps, "cost_estimating")
    executors: dict[str, _DelegatedExecutor] = {}

    def factory(action: str) -> _DelegatedExecutor:
        executor = _DelegatedExecutor(action)
        executors[action] = executor
        return executor

    registry = _registry_for_step(loaded, step, aliyun_delegated_executor_factory=factory)
    inputs = {
        "ros_validate_template": {"template_url": "template.yaml"},
        "ros_get_template_parameter_constraints": {
            "template_url": "template.yaml",
            "parameters": {"ZoneId": "cn-hangzhou-h"},
        },
        "ros_preview_template": {
            "template_url": "template.yaml",
            "stack_name": "preview-stack",
            "parameters": {"ZoneId": "cn-hangzhou-h"},
        },
        "ros_estimate_template_cost": {
            "template_url": "template.yaml",
            "parameters": {"ZoneId": "cn-hangzhou-h"},
        },
    }

    for tool_name, tool_input in inputs.items():
        tool = registry.get(tool_name)
        assert tool is not None
        permission = await tool.check_permissions(tool_input, ToolPermissionContext(pipeline_mode=True))
        result = await tool.execute(tool_input=tool_input, context=ToolContext(pipeline_mode=True))

        assert permission.behavior == "allow"
        assert result.is_error is False
        assert json.loads(result.content) == {"Action": tool.action, "TemplateURL": "template.yaml"}
        assert result.metadata is not None
        assert result.metadata["aliyun_http"]["action"] == tool.action
        assert executors[tool.action].permission_contexts[-1].pipeline_mode is True
        assert executors[tool.action].execution_contexts[-1].pipeline_mode is True


def test_ros_deploy_is_only_injected_into_deploying_step(monkeypatch):
    monkeypatch.setenv("IAC_CODE_PIPELINE_SELLING_ENABLE_REVIEWING", "true")
    loaded = load_pipeline_dir(_selling_dir())
    evaluate_steps = loaded.sub_pipelines["evaluate_candidate"].steps

    deploying_registry = _registry_for_step(loaded, _step_by_id(loaded.steps, "deploying"))
    assert deploying_registry.get("ros_deploy") is not None

    for step_id in ["template_generating", "reviewing", "cost_estimating"]:
        registry = _registry_for_step(loaded, _step_by_id(evaluate_steps, step_id))
        assert registry.get("ros_deploy") is None

    for step_id in ["intent_parsing", "architecture_planning", "evaluate_candidates", "confirm_and_select"]:
        registry = _registry_for_step(loaded, _step_by_id(loaded.steps, step_id))
        assert registry.get("ros_deploy") is None


def test_deploying_step_excludes_raw_ros_stack_from_base_registry(monkeypatch):
    monkeypatch.setenv("IAC_CODE_PIPELINE_SELLING_ENABLE_REVIEWING", "true")
    loaded = load_pipeline_dir(_selling_dir())
    base_registry = ToolRegistry()
    base_registry.register(_NamedTool("ros_stack"))
    base_registry.register(_NamedTool("aliyun_api"))

    deploying_registry = _registry_for_step(
        loaded,
        _step_by_id(loaded.steps, "deploying"),
        base_registry=base_registry,
    )

    assert deploying_registry.get("ros_deploy") is not None
    assert deploying_registry.get("ros_stack") is None
    assert deploying_registry.get("aliyun_api") is not None


def test_full_base_selling_steps_exclude_ros_console_lifecycle_tools(monkeypatch):
    monkeypatch.setenv("IAC_CODE_PIPELINE_SELLING_ENABLE_REVIEWING", "true")
    loaded = load_pipeline_dir(_selling_dir())
    evaluate_steps = loaded.sub_pipelines["evaluate_candidate"].steps
    base_registry = ToolRegistry()
    for tool_name in ROS_CONSOLE_LIFECYCLE_TOOLS:
        base_registry.register(_NamedTool(tool_name))

    steps = [
        _step_by_id(evaluate_steps, "template_generating"),
        _step_by_id(evaluate_steps, "cost_estimating"),
        _step_by_id(loaded.steps, "deploying"),
    ]

    for step in steps:
        registry = _registry_for_step(loaded, step, base_registry=base_registry)
        assert {name for name in ROS_CONSOLE_LIFECYCLE_TOOLS if registry.get(name) is not None} == set()


def test_cost_step_keeps_aliyun_api_for_external_hard_constraint_evidence(monkeypatch):
    monkeypatch.setenv("IAC_CODE_PIPELINE_SELLING_ENABLE_REVIEWING", "true")
    loaded = load_pipeline_dir(_selling_dir())
    base_registry = ToolRegistry()
    base_registry.register(_NamedTool("aliyun_api"))

    cost_registry = _registry_for_step(
        loaded,
        _step_by_id(loaded.sub_pipelines["evaluate_candidate"].steps, "cost_estimating"),
        base_registry=base_registry,
    )

    assert cost_registry.get("aliyun_api") is not None


def test_deploying_step_excludes_write_file_but_keeps_shell_and_in_place_editing(monkeypatch):
    monkeypatch.setenv("IAC_CODE_PIPELINE_SELLING_ENABLE_REVIEWING", "true")
    loaded = load_pipeline_dir(_selling_dir())
    base_registry = ToolRegistry()
    base_registry.register_default_tools()

    deploying_registry = _registry_for_step(
        loaded,
        _step_by_id(loaded.steps, "deploying"),
        base_registry=base_registry,
    )

    assert deploying_registry.get("read_file") is not None
    assert deploying_registry.get("edit_file") is not None
    assert deploying_registry.get("write_file") is None
    assert deploying_registry.get("bash") is not None
