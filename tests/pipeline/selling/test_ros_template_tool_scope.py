from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.loader import load_pipeline_dir
from iac_code.pipeline.engine.step_executor import StepExecutor
from iac_code.pipeline.engine.step_spec import StepSpec
from iac_code.tools.base import Tool, ToolRegistry

ROS_TEMPLATE_TOOLS = {
    "ros_validate_template",
    "ros_get_template_parameter_constraints",
    "ros_preview_template",
    "ros_estimate_template_cost",
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


def _registry_for_step(loaded, step: StepSpec, *, base_registry: ToolRegistry | None = None):
    executor = StepExecutor(
        provider_manager=MagicMock(),
        base_tool_registry=base_registry or ToolRegistry(),
        pipeline=loaded,
        pipeline_dir=_selling_dir(),
    )
    return executor._build_step_tools(step, PipelineContext(loaded.context_dependencies))


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
