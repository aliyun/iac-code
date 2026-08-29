"""Step 2 ``materialize_selected_candidate``（设计文档 §18.4）。

只实现用户选中的那一个方案：模板写入/校验、参数约束、Preview、询价与最终确认走同一个
``template_url``；结构化确认必须按专用交互的 action 确定性执行，自然语言由 LLM 判断；换方案必须真的回滚
到 Step 1。这里的 completion guard 都取自真实 pipeline.yaml，不再复述一份配置。
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
from iac_code.a2a.pipeline_executor import _pipeline_a2a_artifacts_by_step_id
from iac_code.pipeline.engine.complete_step_tool import CompleteStepTool
from iac_code.pipeline.engine.completion_guard_state import record_completion_guard_tool_result
from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.loader import load_pipeline_dir
from iac_code.pipeline.engine.step_executor import StepExecutor
from iac_code.pipeline.engine.types import StepConfig
from iac_code.pipeline.selling_solution_first.hooks import materialize_selected_candidate as materialize_hooks
from iac_code.tools.base import Tool, ToolRegistry

STEP_ID = "materialize_selected_candidate"
TEMPLATE_PATH = "solutions/solution-a.yml"
TEMPLATE_BODY = "ROSTemplateFormatVersion: '2015-09-01'\nResources: {}\n"
CANDIDATE_NAME = "方案A：经典三层"

HARD_CONSTRAINT = {
    "id": "hc-storage",
    "target": "rds",
    "property": "storage",
    "operator": "gte",
    "value": 100,
    "unit": "GB",
    "verification_mode": "tool",
    "source": "user",
    "source_text": "数据库磁盘至少 100GB",
}
HARD_CONSTRAINT_CHECK = {
    "constraint": HARD_CONSTRAINT,
    "status": "satisfied",
    "actual_value": 120,
    "actual_unit": "GB",
    "parameter_values": {"DBInstanceStorage": 120},
    "evidence": [
        {
            "type": "tool",
            "summary": "DescribeDBInstanceAttribute 返回 120GB",
            "actual_value": 120,
            "tool_name": "aliyun_api",
            "product": "rds",
            "action": "DescribeDBInstanceAttribute",
            "result_path": "Items.0.Storage",
        }
    ],
}
DEPLOYMENT_PARAMETERS = {"DBInstanceStorage": 120, "ZoneId": "cn-hangzhou-h"}
SELECTED_CANDIDATE = {
    "candidate_id": "cand-a",
    "name": CANDIDATE_NAME,
    "output_path": TEMPLATE_PATH,
    "hard_constraints": [HARD_CONSTRAINT],
}
CONFIRMATION_ANSWER = {
    "action": "confirm",
    "input_type": "natural_language",
    "user_input": "确认部署",
    "parameter_overrides": {},
}

CONFIRMED_CONCLUSION = {
    "status": "confirmed",
    "continue_pipeline": True,
    "deployment_confirmed": True,
    "selection_valid": True,
    "selected_candidate_result": {
        "solution_summary": "杭州地域的 SLB + 双 ECS + RDS 三层方案，ROS 询价合同价约 ¥1,024/月。",
        "template": {
            "file_path": TEMPLATE_PATH,
            "region": "cn-hangzhou",
        },
        "cost": {
            "quote_status": "succeeded",
            "monthly_estimate": "¥1,280.00/月（列表价，合同优惠后约¥1,024.00/月）",
            "currency": "CNY",
            "resources": [{"type": "ALIYUN::ECS::InstanceGroup", "spec": "ecs.g7.large x 2", "cost": "¥480.00"}],
            "deployment_parameters": DEPLOYMENT_PARAMETERS,
            "user_required_missing_parameters": [],
            "hard_constraint_checks": [HARD_CONSTRAINT_CHECK],
            "preview_validation": {
                "succeeded": True,
                "template_url": TEMPLATE_PATH,
                "parameters": DEPLOYMENT_PARAMETERS,
            },
        },
    },
    "template_url": TEMPLATE_PATH,
    "parameter_overrides": {},
    "effective_deployment_parameters": DEPLOYMENT_PARAMETERS,
    "preview_ready_for_create": True,
    "confirmation": dict(CONFIRMATION_ANSWER),
}

SOLUTION_SELECTION = {
    "status": "selected",
    "continue_pipeline": True,
    "is_infra_intent": True,
    "candidates": [{"name": "方案B", "output_path": "solutions/solution-b.yml"}, SELECTED_CANDIDATE],
    "options": [{"name": "方案B", "candidate_index": 0}, {"name": CANDIDATE_NAME, "candidate_index": 1}],
    "selected_candidate_name": CANDIDATE_NAME,
    "selected_candidate_index": 1,
    "selected_candidate": SELECTED_CANDIDATE,
}


def _pipeline_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "src" / "iac_code" / "pipeline" / "selling_solution_first"


@pytest.fixture(scope="module")
def loaded():
    return load_pipeline_dir(_pipeline_dir())


@pytest.fixture(scope="module")
def step(loaded):
    return next(item for item in loaded.steps if item.step_id == STEP_ID)


@pytest.fixture(scope="module")
def prompt_text() -> str:
    return (_pipeline_dir() / "prompts" / "materialize_selected_candidate.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def skill_text() -> str:
    return (
        _pipeline_dir() / "skills" / "iac-aliyun-materialize-selected-candidate" / "SKILL.md"
    ).read_text(encoding="utf-8")


def _conclusion(**overrides):
    conclusion = copy.deepcopy(CONFIRMED_CONCLUSION)
    conclusion.update(copy.deepcopy(overrides))
    return conclusion


def _record(state, tool_name, tool_input, content, *, cwd):
    record_completion_guard_tool_result(
        state,
        tool_name=tool_name,
        tool_input=tool_input,
        content=content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
        is_error=False,
        cwd=cwd,
    )


def _happy_guard_state(cwd: str) -> dict:
    """Replay the real tool sequence a compliant Step 2 run produces."""

    state: dict = {
        "context_snapshot": {
            "solution_selection": copy.deepcopy(SOLUTION_SELECTION),
            "selected_plan": {"status": "awaiting_confirmation", "parameter_overrides": {}},
        }
    }
    _record(state, "write_file", {"path": TEMPLATE_PATH, "content": TEMPLATE_BODY}, "wrote template", cwd=cwd)
    _record(state, "ros_validate_template", {"template_url": TEMPLATE_PATH}, {"Parameters": {}}, cwd=cwd)
    _record(
        state,
        "ros_get_template_parameter_constraints",
        {"template_url": TEMPLATE_PATH, "parameters": DEPLOYMENT_PARAMETERS},
        {"ParameterConstraints": []},
        cwd=cwd,
    )
    _record(
        state,
        "ros_preview_template",
        {"template_url": TEMPLATE_PATH, "stack_name": "solution-a", "parameters": DEPLOYMENT_PARAMETERS},
        {"Stack": {"Resources": []}},
        cwd=cwd,
    )
    _record(
        state,
        "aliyun_api",
        {"product": "rds", "action": "DescribeDBInstanceAttribute"},
        {"Items": [{"Storage": 120}]},
        cwd=cwd,
    )
    _record(
        state,
        "ros_estimate_template_cost",
        {"template_url": TEMPLATE_PATH, "parameters": DEPLOYMENT_PARAMETERS},
        {"OriginalAmount": 1280.0, "TradeAmount": 1024.0, "Currency": "CNY"},
        cwd=cwd,
    )
    return state


def _tool(step, state, *, user_message: str = "") -> CompleteStepTool:
    return CompleteStepTool(
        StepConfig(
            step_id=step.step_id,
            conclusion_field=step.conclusion_field,
            forward=step.forward,
            conclusion_schema=step.conclusion_schema,
            rollback_targets=["solution_planning_and_selection"],
            max_conclusion_retries=step.max_conclusion_retries,
            compact_completion_schema=step.config.get("compact_completion_schema") is True,
            compact_completion_errors=step.config.get("compact_completion_errors") is True,
            conclusion_merge_context_field=step.config.get("conclusion_merge_context_field"),
            conclusion_merge_statuses=tuple(step.config.get("conclusion_merge_statuses", [])),
            hydrate_selected_candidate=step.config.get("hydrate_selected_candidate") is True,
            authoritative_candidate_context_field=step.config.get("authoritative_candidate_context_field"),
            authoritative_candidate_targets=tuple(step.config.get("authoritative_candidate_targets", [])),
        ),
        completion_guards=step.completion_guards,
        completion_guard_state=state,
        user_message=user_message,
    )


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


def _registry_for_step(loaded, step) -> ToolRegistry:
    base_registry = ToolRegistry()
    base_registry.register_default_tools()
    for name in ("ros_stack", "ros_stack_instances", "write_memory", "aliyun_api"):
        base_registry.register(_NamedTool(name))
    executor = StepExecutor(
        provider_manager=MagicMock(),
        base_tool_registry=base_registry,
        pipeline=loaded,
        pipeline_dir=_pipeline_dir(),
    )
    return executor._build_step_tools(step, PipelineContext(loaded.context_dependencies))


class TestConfirmedCompletion:
    def test_the_documented_happy_path_passes_every_guard(self, step, tmp_path):
        tool = _tool(step, _happy_guard_state(str(tmp_path)))

        assert tool.validate_completion_input({"conclusion": _conclusion()}) is None

    def test_natural_language_confirmation_accepts_only_the_incremental_branch(self, step, tmp_path):
        state = _happy_guard_state(str(tmp_path))
        awaiting = _conclusion(
            status="awaiting_confirmation",
            deployment_confirmed=False,
            user_prompt="请选择下一步操作",
            options=[{"action": "confirm", "name": "确认部署"}, {"action": "cancel", "name": "取消"}],
        )
        awaiting.pop("confirmation")
        state["context_snapshot"]["selected_plan"] = awaiting
        tool = _tool(step, state, user_message="确认部署")
        tool_input = {
            "conclusion": {
                "status": "confirmed",
                "continue_pipeline": True,
                "deployment_confirmed": True,
                "confirmation": copy.deepcopy(CONFIRMATION_ANSWER),
            }
        }

        assert tool.validate_completion_input(tool_input) is None
        assert "selected_candidate" not in tool_input["conclusion"]
        assert "candidate" not in tool_input["conclusion"]["selected_candidate_result"]
        assert tool_input["conclusion"]["selected_candidate_result"]["cost"]["monthly_estimate"].startswith(
            "¥1,280"
        )

    def test_confirmation_cannot_skip_the_dedicated_waiting_state(self, step, tmp_path):
        state = _happy_guard_state(str(tmp_path))
        state["context_snapshot"]["selected_plan"] = {}
        tool = _tool(step, state, user_message="确认部署")

        error = tool.validate_completion_input({"conclusion": _conclusion()})

        assert error is not None
        assert "shown in the dedicated confirmation state" in error

    def test_template_url_must_be_the_materialized_template_path(self, step, tmp_path):
        tool = _tool(step, _happy_guard_state(str(tmp_path)))
        conclusion = _conclusion()
        # 校验过的模板与结论声明的模板必须是同一个文件。
        conclusion["selected_candidate_result"]["template"]["file_path"] = "solutions/other.yml"

        error = tool.validate_completion_input({"conclusion": conclusion})

        assert error is not None
        assert "template file that ros_validate_template validated last" in error

    def test_confirmation_without_a_validated_template_is_rejected(self, step, tmp_path):
        state = {
            "context_snapshot": {
                "solution_selection": copy.deepcopy(SOLUTION_SELECTION),
                "selected_plan": {"status": "awaiting_confirmation", "parameter_overrides": {}},
            }
        }
        _record(state, "ask_user_question", {"question": "是否部署？"}, CONFIRMATION_ANSWER, cwd=str(tmp_path))
        tool = _tool(step, state)

        error = tool.validate_completion_input({"conclusion": _conclusion()})

        assert error is not None
        assert "ros_validate_template" in error

    def test_rewriting_the_template_after_validation_invalidates_the_confirmation(self, step, tmp_path):
        state = _happy_guard_state(str(tmp_path))
        _record(state, "write_file", {"path": TEMPLATE_PATH, "content": TEMPLATE_BODY}, "rewrote", cwd=str(tmp_path))
        tool = _tool(step, state)

        error = tool.validate_completion_input({"conclusion": _conclusion()})

        assert error is not None
        assert "rewritten after ros_validate_template" in error


class TestConfirmationBinding:
    def test_structured_cancel_accepts_the_minimal_terminal_delta(self, step, tmp_path):
        user_message = '{"action":"cancel","parameter_overrides":{}}'
        conclusion = {
            "status": "cancelled",
            "continue_pipeline": False,
            "deployment_confirmed": False,
            "cancellation_reason": user_message,
        }
        tool = _tool(step, _happy_guard_state(str(tmp_path)), user_message=user_message)

        assert tool.validate_completion_input({"conclusion": conclusion}) is None

    def test_structured_confirm_is_bound_to_the_exact_input(self, step, tmp_path):
        user_message = '{"action":"confirm","parameter_overrides":{}}'
        conclusion = _conclusion(
            confirmation={
                "action": "confirm",
                "input_type": "structured",
                "user_input": user_message,
                "parameter_overrides": {},
            }
        )
        tool = _tool(step, _happy_guard_state(str(tmp_path)), user_message=user_message)

        assert tool.validate_completion_input({"conclusion": conclusion}) is None

    def test_structured_action_cannot_be_reinterpreted_as_confirm(self, step, tmp_path):
        user_message = '{"action":"cancel"}'
        tool = _tool(step, _happy_guard_state(str(tmp_path)), user_message=user_message)

        error = tool.validate_completion_input({"conclusion": _conclusion()})

        assert error is not None
        assert "submitted action was cancel" in error

    def test_fabricated_structured_confirmation_record_is_rejected(self, step, tmp_path):
        user_message = '{"action":"confirm","parameter_overrides":{}}'
        tool = _tool(step, _happy_guard_state(str(tmp_path)), user_message=user_message)

        error = tool.validate_completion_input({"conclusion": _conclusion()})

        assert error is not None
        assert "must record the exact structured input" in error

    def test_structured_confirm_with_changed_parameters_is_authorized_in_one_shot(self, step, tmp_path):
        # 界面自己用「模板正文 + 最新参数」询价，所以明确的 confirm 可以携带与上一次询价输入不同的参数。
        # 这仍然是一次最终授权：guard 不得要求重新询价或第二次确认。
        user_message = '{"action":"confirm","parameter_overrides":{"ZoneId":"cn-hangzhou-k"}}'
        conclusion = _conclusion(
            parameter_overrides={"ZoneId": "cn-hangzhou-k"},
            effective_deployment_parameters={**DEPLOYMENT_PARAMETERS, "ZoneId": "cn-hangzhou-k"},
            preview_ready_for_create=False,
            confirmation={
                "action": "confirm",
                "input_type": "structured",
                "user_input": user_message,
                "parameter_overrides": {"ZoneId": "cn-hangzhou-k"},
            },
        )
        tool = _tool(step, _happy_guard_state(str(tmp_path)), user_message=user_message)

        assert tool.validate_completion_input({"conclusion": conclusion}) is None

    def test_structured_confirm_with_empty_overrides_preserves_current_overrides(self, step, tmp_path):
        user_message = '{"action":"confirm","parameter_overrides":{}}'
        state = _happy_guard_state(str(tmp_path))
        state["context_snapshot"]["selected_plan"]["parameter_overrides"] = {"ZoneId": "cn-hangzhou-h"}
        conclusion = _conclusion(
            parameter_overrides={"ZoneId": "cn-hangzhou-h"},
            confirmation={
                "action": "confirm",
                "input_type": "structured",
                "user_input": user_message,
                "parameter_overrides": {"ZoneId": "cn-hangzhou-h"},
            }
        )
        tool = _tool(step, state, user_message=user_message)

        assert tool.validate_completion_input({"conclusion": conclusion}) is None

    def test_structured_confirm_without_override_payload_preserves_current_overrides(self, step, tmp_path):
        user_message = '{"action":"confirm"}'
        state = _happy_guard_state(str(tmp_path))
        state["context_snapshot"]["selected_plan"]["parameter_overrides"] = {"ZoneId": "cn-hangzhou-h"}
        conclusion = _conclusion(
            parameter_overrides={"ZoneId": "cn-hangzhou-h"},
            confirmation={
                "action": "confirm",
                "input_type": "structured",
                "user_input": user_message,
                "parameter_overrides": {"ZoneId": "cn-hangzhou-h"},
            },
        )
        tool = _tool(step, state, user_message=user_message)

        assert tool.validate_completion_input({"conclusion": conclusion}) is None

    def test_structured_adjust_returns_to_confirmation_wait(self, step, tmp_path):
        user_message = '{"action":"adjust","parameter_overrides":{"ZoneId":"cn-hangzhou-k"}}'
        conclusion = _conclusion(
            status="awaiting_confirmation",
            continue_pipeline=True,
            deployment_confirmed=False,
            parameter_overrides={"ZoneId": "cn-hangzhou-k"},
            user_prompt="请确认更新后的方案与 ROS 询价",
            options=[
                {"action": "confirm", "name": "确认部署"},
                {"action": "reselect", "name": "重新选择方案"},
                {"action": "cancel", "name": "取消"},
            ],
        )
        conclusion.pop("confirmation")
        tool = _tool(step, _happy_guard_state(str(tmp_path)), user_message=user_message)

        assert tool.validate_completion_input({"conclusion": conclusion}) is None

    def test_unchanged_structured_confirm_cannot_return_to_confirmation_wait(self, step, tmp_path):
        user_message = '{"action":"confirm"}'
        conclusion = _conclusion(
            status="awaiting_confirmation",
            continue_pipeline=True,
            deployment_confirmed=False,
            user_prompt="请确认更新后的方案与 ROS 询价",
            options=[
                {"action": "confirm", "name": "确认部署"},
                {"action": "reselect", "name": "重新选择方案"},
                {"action": "cancel", "name": "取消"},
            ],
        )
        conclusion.pop("confirmation")
        tool = _tool(step, _happy_guard_state(str(tmp_path)), user_message=user_message)

        error = tool.validate_completion_input({"conclusion": conclusion})

        assert error is not None
        assert "handled exactly as submitted" in error

    def test_structured_confirm_with_changed_parameters_cannot_return_to_confirmation_wait(self, step, tmp_path):
        # 携带新参数的 confirm 也不允许被降级成一次新的等待态：用户不能被要求确认两次。
        user_message = '{"action":"confirm","parameter_overrides":{"ZoneId":"cn-hangzhou-k"}}'
        conclusion = _conclusion(
            status="awaiting_confirmation",
            continue_pipeline=True,
            deployment_confirmed=False,
            parameter_overrides={"ZoneId": "cn-hangzhou-k"},
            user_prompt="请确认更新后的方案与 ROS 询价",
            options=[
                {"action": "confirm", "name": "确认部署"},
                {"action": "reselect", "name": "重新选择方案"},
                {"action": "cancel", "name": "取消"},
            ],
        )
        conclusion.pop("confirmation")
        tool = _tool(step, _happy_guard_state(str(tmp_path)), user_message=user_message)

        error = tool.validate_completion_input({"conclusion": conclusion})

        assert error is not None
        assert "handled exactly as submitted" in error

    def test_natural_language_confirmation_is_judged_by_the_llm(self, step, tmp_path):
        tool = _tool(step, _happy_guard_state(str(tmp_path)), user_message="按这个配置确认部署")

        assert tool.validate_completion_input({"conclusion": _conclusion()}) is None


class TestParameterAndConstraintCoverage:
    def test_missing_user_required_parameter_list_blocks_confirmation(self, step, tmp_path):
        tool = _tool(step, _happy_guard_state(str(tmp_path)))
        conclusion = _conclusion()
        conclusion["selected_candidate_result"]["cost"].pop("user_required_missing_parameters")

        assert tool.validate_completion_input({"conclusion": conclusion}) is not None

    def test_outstanding_user_required_parameters_block_confirmation(self, step, tmp_path):
        tool = _tool(step, _happy_guard_state(str(tmp_path)))
        conclusion = _conclusion()
        conclusion["selected_candidate_result"]["cost"]["user_required_missing_parameters"] = [
            {"name": "DBPassword", "reason": "只能由用户提供"}
        ]

        error = tool.validate_completion_input({"conclusion": conclusion})

        assert error is not None

    def test_missing_hard_constraint_check_blocks_confirmation(self, step, tmp_path):
        tool = _tool(step, _happy_guard_state(str(tmp_path)))
        conclusion = _conclusion()
        conclusion["selected_candidate_result"]["cost"]["hard_constraint_checks"] = []

        error = tool.validate_completion_input({"conclusion": conclusion})

        assert error is not None
        assert "missing_constraint_check" in error
        assert "hc-storage" in error

    def test_constraint_parameters_must_match_the_effective_deployment_parameters(self, step, tmp_path):
        tool = _tool(step, _happy_guard_state(str(tmp_path)))
        conclusion = _conclusion()
        check = conclusion["selected_candidate_result"]["cost"]["hard_constraint_checks"][0]
        check["status"] = "unresolved"
        check["parameter_values"] = {"DBInstanceStorage": 80}

        error = tool.validate_completion_input({"conclusion": conclusion})

        assert error is not None
        assert "constraint_parameter_mismatch" in error

    def test_tool_verified_constraints_need_a_matching_tool_record(self, step, tmp_path):
        state = _happy_guard_state(str(tmp_path))
        state["tool_result_records"] = [
            record for record in state["tool_result_records"] if record["tool_name"] != "aliyun_api"
        ]
        tool = _tool(step, state)
        conclusion = _conclusion()
        conclusion["selected_candidate_result"]["cost"]["hard_constraint_checks"][0]["status"] = "unresolved"

        error = tool.validate_completion_input({"conclusion": conclusion})

        assert error is not None
        assert "tool_evidence_not_found" in error

    def test_llm_satisfied_constraint_survives_missing_tool_record(self, step, tmp_path):
        state = _happy_guard_state(str(tmp_path))
        state["tool_result_records"] = [
            record for record in state["tool_result_records"] if record["tool_name"] != "aliyun_api"
        ]
        tool = _tool(step, state)

        error = tool.validate_completion_input({"conclusion": _conclusion()})

        assert error is None


class TestNonConfirmedOutcomes:
    def test_reselect_requires_a_rollback_to_step_one(self, step, tmp_path):
        tool = _tool(step, _happy_guard_state(str(tmp_path)))
        conclusion = {
            "status": "reselect_requested",
            "continue_pipeline": True,
            "deployment_confirmed": False,
            "reselect_reason": "用户想换成 Serverless 方案",
        }

        error = tool.validate_completion_input({"conclusion": conclusion})

        assert error is not None
        assert "roll back to the solution planning and selection step" in error
        assert "target_step solution_planning_and_selection" in error

    def test_reselect_rollback_to_a_wrong_target_is_rejected(self, step, tmp_path):
        tool = _tool(step, _happy_guard_state(str(tmp_path)))

        error = tool.validate_completion_input(
            {
                "conclusion": {
                    "status": "reselect_requested",
                    "continue_pipeline": True,
                    "deployment_confirmed": False,
                    "reselect_reason": "换方案",
                },
                "rollback_request": {"target_step": "deploying", "reason": "换方案"},
            }
        )

        assert error is not None
        assert "target_step must be solution_planning_and_selection" in error

    def test_reselect_with_a_proper_rollback_request_passes(self, step, tmp_path):
        tool = _tool(step, _happy_guard_state(str(tmp_path)))

        error = tool.validate_completion_input(
            {
                "conclusion": {
                    "status": "reselect_requested",
                    "continue_pipeline": True,
                    "deployment_confirmed": False,
                    "reselect_reason": "用户想换成 Serverless 方案",
                },
                "rollback_request": {
                    "target_step": "solution_planning_and_selection",
                    "reason": "用户想换成 Serverless 方案",
                },
            }
        )

        assert error is None

    def test_cancelled_outcome_needs_no_template_or_confirmation(self, step):
        tool = _tool(step, {"context_snapshot": {}})

        error = tool.validate_completion_input(
            {
                "conclusion": {
                    "status": "cancelled",
                    "continue_pipeline": False,
                    "deployment_confirmed": False,
                    "cancellation_reason": "用户暂时不部署",
                }
            }
        )

        assert error is None

    def test_cancelled_outcome_cannot_claim_deployment_confirmation(self, step):
        tool = _tool(step, {"context_snapshot": {}})

        error = tool.validate_completion_input(
            {
                "conclusion": {
                    "status": "cancelled",
                    "continue_pipeline": False,
                    "deployment_confirmed": True,
                }
            }
        )

        assert error is not None


class TestAuthoritativeSelectionHooks:
    def test_on_enter_pins_the_candidate_selected_in_step_one(self):
        context = PipelineContext({"solution_selection": [], "selected_plan": [], "deployment": []})
        selection = copy.deepcopy(SOLUTION_SELECTION)
        # Step 1 只给了序号，名字由 hook 补齐。
        selection.pop("selected_candidate")
        selection["selected_candidate_name"] = ""
        context.set_conclusion("solution_selection", selection)

        materialize_hooks.on_enter(context)

        stored = context.get_conclusion("solution_selection")
        assert stored["selection_valid"] is True
        assert stored["selected_candidate"]["name"] == CANDIDATE_NAME
        assert stored["selected_candidate_index"] == 1
        assert stored["selected_candidate_name"] == CANDIDATE_NAME
        assert "parameter_overrides" not in stored
        # 权威候选是副本，Step 2 改它不会污染候选清单。
        stored["selected_candidate"]["name"] = "被改过"
        assert stored["candidates"][1]["name"] == CANDIDATE_NAME

    @pytest.mark.parametrize(
        ("mutate", "expected"),
        [
            (lambda selection: selection.update({"status": "awaiting_selection"}), "must be 'selected'"),
            (lambda selection: selection.update({"candidates": []}), "candidates is empty"),
            (lambda selection: selection.update({"selected_candidate_index": 7}), "out of range"),
            (
                lambda selection: selection.update({"selected_candidate_name": "不存在的方案"}),
                "name mismatch",
            ),
            (
                lambda selection: selection.update({"selected_candidate_index": None, "selected_candidate_name": ""}),
                "neither selected_candidate_index nor selected_candidate_name",
            ),
        ],
    )
    def test_on_enter_reports_an_unresolvable_selection(self, mutate, expected):
        context = PipelineContext({"solution_selection": [], "selected_plan": [], "deployment": []})
        selection = copy.deepcopy(SOLUTION_SELECTION)
        mutate(selection)
        context.set_conclusion("solution_selection", selection)

        materialize_hooks.on_enter(context)

        stored = context.get_conclusion("solution_selection")
        assert stored["selection_valid"] is False
        assert expected in stored["selection_error"]

    def test_on_exit_leaves_a_cancelled_conclusion_alone(self):
        context = PipelineContext({"solution_selection": [], "selected_plan": [], "deployment": []})
        context.set_conclusion("solution_selection", copy.deepcopy(SOLUTION_SELECTION))
        conclusion = {"status": "cancelled", "continue_pipeline": False, "deployment_confirmed": False}

        materialize_hooks.on_exit(context, conclusion)

        assert conclusion == {"status": "cancelled", "continue_pipeline": False, "deployment_confirmed": False}


class TestStepToolScope:
    def test_solution_planning_step_exposes_read_only_cloud_query_without_write_tools(self, loaded):
        planning = next(item for item in loaded.steps if item.step_id == "solution_planning_and_selection")
        registry = _registry_for_step(loaded, planning)

        for expected in ("aliyun_api", "ask_user_question", "show_architecture_plan", "show_candidate_detail"):
            assert registry.get(expected) is not None, expected
        for blocked in ("write_file", "edit_file", "bash", "ros_deploy", "ros_estimate_template_cost"):
            assert registry.get(blocked) is None, blocked

    def test_materialize_step_has_no_stack_write_entry(self, loaded, step):
        registry = _registry_for_step(loaded, step)

        for blocked in ("ros_deploy", "ros_stack", "ros_stack_instances", "write_memory"):
            assert registry.get(blocked) is None, blocked

    def test_materialize_step_keeps_the_reused_template_toolchain(self, loaded, step):
        registry = _registry_for_step(loaded, step)

        for expected in (
            "write_file",
            "edit_file",
            "read_file",
            "bash",
            "aliyun_api",
            "ask_user_question",
            "ros_validate_template",
            "ros_get_template_parameter_constraints",
            "ros_preview_template",
            "ros_estimate_template_cost",
            "complete_step",
        ):
            assert registry.get(expected) is not None, expected

    def test_deploying_step_only_exposes_the_confirmed_wrapper(self, loaded):
        deploying = next(item for item in loaded.steps if item.step_id == "deploying")

        registry = _registry_for_step(loaded, deploying)

        deploy_tool = registry.get("ros_deploy")
        assert deploy_tool is not None
        assert type(deploy_tool).__name__ == "ConfirmedRosDeployTool"
        assert registry.get("ros_stack") is None
        assert registry.get("ros_stack_instances") is None
        assert registry.get("write_file") is None


class TestPromptContract:
    def test_single_template_url_is_reused_across_validate_preview_and_pricing(self, prompt_text):
        assert "{solution_selection.selected_candidate.output_path}" in prompt_text
        assert prompt_text.count("ros_validate_template") >= 1
        for tool_name in (
            "ros_get_template_parameter_constraints",
            "ros_preview_template",
            "ros_estimate_template_cost",
        ):
            assert tool_name in prompt_text
        assert "始终使用同一路径" in prompt_text

    def test_only_the_selected_candidate_is_materialized(self, skill_text):
        assert "只实现一个方案" in skill_text
        assert "不要生成第二份模板" in skill_text
        assert "不要为其它候选做模板、Preview 或询价" in skill_text

    def test_template_validation_forbids_a_stock_pyyaml_self_check(self, skill_text):
        assert "模板校验只用 `ros_validate_template`" in skill_text
        # 只有 `ros_validate_template` 的解析器认识 ROS 短标签，标准库 PyYAML 的报错与模板正确性无关，
        # 技能必须明确禁止这种自查，否则模型会把 `!Ref` 的 ConstructorError 当成模板问题。
        assert "yaml.safe_load" in skill_text
        assert "ConstructorError" in skill_text
        assert "不得用 bash 里的标准库 PyYAML 代替 `ros_validate_template` 校验模板" in skill_text

    def test_environment_errors_are_not_treated_as_template_errors(self, skill_text):
        assert "**环境类错误**" in skill_text
        assert "不得用重写模板的方式绕过" in skill_text

    def test_pricing_keeps_both_list_and_contract_amounts(self, skill_text):
        assert "OriginalAmount" in skill_text
        assert "TradeAmount" in skill_text
        assert "列表价" in skill_text
        # 不允许退回 Step 1 的粗估价格。
        assert "粗估" in skill_text

    def test_external_required_parameters_are_collected_before_confirmation(self, skill_text):
        assert "user_required_missing_parameters" in skill_text
        assert "ask_user_question" in skill_text

    def test_dedicated_confirmation_supports_structured_and_natural_language_input(self, prompt_text, skill_text):
        assert "deployment_confirmation" in prompt_text
        assert "不使用 `ask_user_question`" in prompt_text
        assert "结构化 `action`" in prompt_text
        assert "非结构化输入由 LLM" in prompt_text
        assert "不得重新执行物化工具或再次等待确认" in prompt_text
        assert "携带新参数覆盖的确认同样只需一次" in prompt_text
        assert 'action: "confirm"' in skill_text
        # 技能必须把「携带新参数的 confirm」写成一次授权，而不是调整请求。
        assert "无论是否携带参数覆盖" in skill_text
        assert "不重算、不重新询价、不再次等待确认" in skill_text

    def test_parameter_adjustment_reprices_and_rewrites_the_solution_summary(self, skill_text):
        assert "重新执行必要的参数约束查询、PreviewStack 和 ROS 精确询价" in skill_text
        assert "重新生成 `solution_summary`" in skill_text
        assert "再次提交 `status: \"awaiting_confirmation\"`" in skill_text

    def test_free_text_distinguishes_parameter_architecture_and_new_intent_changes(self, skill_text):
        assert "调整当前参数" in skill_text
        assert "重新规划当前架构" in skill_text
        assert "替换为全新部署意图" in skill_text
        assert "全新部署意图统一由用户直接输入自然语言" in skill_text
        assert "全新意图以最新输入替换旧部署目标" in skill_text

    def test_confirmation_summary_is_user_facing_and_not_duplicated_as_plain_text(self, prompt_text, skill_text):
        assert "通常控制在 2～5 句" in skill_text
        assert "不得写模板路径、StackName、PreviewStack/校验状态、参数 JSON" in skill_text
        assert "不使用 `ALIYUN::...` 资源类型" in skill_text
        assert "不要再用普通助手文本重复方案、价格" in prompt_text

    def test_prompt_only_adapts_runtime_context_and_pipeline_handoff(self, prompt_text):
        for placeholder in (
            "{solution_selection.selected_candidate}",
            "{solution_selection.intent}",
            "{selected_plan.status}",
            "{selected_plan.parameter_overrides}",
            "{selected_plan.selected_candidate_result.cost.monthly_estimate}",
        ):
            assert placeholder in prompt_text
        assert "{selected_plan}" not in prompt_text
        assert "### 选择无效" in prompt_text
        assert "### 首次物化" in prompt_text
        assert "### 确认恢复" in prompt_text
        assert "只提交 `status: confirmed`" in prompt_text
        assert "rollback_request" in prompt_text
        assert "complete_step" in prompt_text

    def test_prompt_does_not_copy_detailed_skill_rules(self, prompt_text):
        assert len(prompt_text.splitlines()) <= 80
        for duplicated_section in (
            "## 阶段 A：模板生成与校验",
            "## 阶段 B：参数求解",
            "### 价格口径",
            "### Preview 软门槛",
            "## 模板规范",
        ):
            assert duplicated_section not in prompt_text


class TestCompleteStepSchemaGuidance:
    def test_compact_tool_schema_defers_full_validation_without_reinjecting_descriptions(self, step, tmp_path):
        tool = _tool(step, _happy_guard_state(str(tmp_path)))
        tool_input = {
            "conclusion": {
                "status": "confirmed",
                "continue_pipeline": True,
                "deployment_confirmed": True,
            }
        }

        valid, input_error = tool.validate_input(copy.deepcopy(tool_input))
        completion_error = tool.validate_completion_input(tool_input)

        assert valid is True
        assert input_error == ""
        assert completion_error is not None
        assert "required property" in completion_error
        assert "Step 2 的完整物化与确认结论" not in completion_error
        assert len(completion_error) < 300


class TestWorkspaceTemplatePathContract:
    """新 Step 2 不再产生模板 artifact：前端用认证过的相对路径向 ros-ai-agent 下载模板正文。"""

    def _context(self, loaded, artifacts_by_step_id, workspace_root):
        return PipelineA2AContext(
            pipeline_run_id="run-1",
            task_id="task-1",
            context_id="ctx-1",
            pipeline_name="selling_solution_first",
            parent_step_order=[item.step_id for item in loaded.steps],
            a2a_artifacts_by_step_id=artifacts_by_step_id,
            trusted_workspace_root=str(workspace_root),
        )

    def _complete(self, conclusion):
        return PipelineEvent(
            type=PipelineEventType.STEP_COMPLETED,
            step_id=STEP_ID,
            timestamp=time.time(),
            data={"conclusion_field": "selected_plan", "conclusion": conclusion},
        )

    def test_no_step_declares_a_template_artifact_while_old_selling_keeps_its_own(self, loaded):
        assert _pipeline_a2a_artifacts_by_step_id(SimpleNamespace(_loaded=loaded)) == {}

        # 旧 selling 的模板 artifact 完全不动（reviewing 受 enable_reviewing 控制，默认未加载）。
        legacy = load_pipeline_dir(_pipeline_dir().parent / "selling")
        legacy_artifacts = _pipeline_a2a_artifacts_by_step_id(SimpleNamespace(_loaded=legacy))
        assert sorted(legacy_artifacts) == ["template_generating"]
        [legacy_artifact] = legacy_artifacts["template_generating"]
        assert legacy_artifact.path == "conclusion.file_path"
        assert legacy_artifact.content == "conclusion.template"

    def test_step_completion_carries_the_certified_path_without_the_template_body(self, loaded, tmp_path):
        artifacts_by_step_id = _pipeline_a2a_artifacts_by_step_id(SimpleNamespace(_loaded=loaded))
        template_file = tmp_path / TEMPLATE_PATH
        template_file.parent.mkdir(parents=True)
        template_file.write_text(TEMPLATE_BODY, encoding="utf-8")
        translator = PipelineEventTranslator(self._context(loaded, artifacts_by_step_id, tmp_path))

        envelopes = translator.translate(self._complete(_conclusion()))

        assert not [item for item in envelopes if item.get("artifact")]
        assert not [item for item in envelopes if item.get("eventType") == "pipeline_warning"]
        [completed] = [item for item in envelopes if item.get("eventType") == "step_completed"]
        conclusion = completed["data"]["conclusion"]
        assert conclusion["template_url"] == TEMPLATE_PATH
        assert conclusion["selected_candidate_result"]["template"]["file_path"] == TEMPLATE_PATH
        assert conclusion["selected_candidate_result"]["cost"]["preview_validation"]["template_url"] == TEMPLATE_PATH
        # 模板正文不进事件：界面只能拿相对路径去工作区下载接口取。
        payload = json.dumps(envelopes, ensure_ascii=False)
        assert "ROSTemplateFormatVersion" not in payload
        assert "template" not in conclusion["selected_candidate_result"]["template"]

    def test_a_traversal_path_neither_reads_a_file_nor_emits_an_artifact(self, loaded, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "outside.yml"
        outside.write_text(TEMPLATE_BODY, encoding="utf-8")
        artifacts_by_step_id = _pipeline_a2a_artifacts_by_step_id(SimpleNamespace(_loaded=loaded))
        conclusion = _conclusion()
        conclusion["template_url"] = "../outside.yml"
        conclusion["selected_candidate_result"]["template"]["file_path"] = "../outside.yml"
        translator = PipelineEventTranslator(self._context(loaded, artifacts_by_step_id, workspace))

        envelopes = translator.translate(self._complete(conclusion))

        assert not [item for item in envelopes if item.get("artifact")]
        payload = json.dumps(envelopes, ensure_ascii=False)
        assert "ROSTemplateFormatVersion" not in payload


class TestPipelineYamlIsTheSingleSourceOfGuards:
    def test_completion_guards_come_from_the_yaml_definition(self, step):
        raw = yaml.safe_load((_pipeline_dir() / "pipeline.yaml").read_text(encoding="utf-8"))
        raw_step = next(item for item in raw["steps"] if item["id"] == STEP_ID)

        assert step.completion_guards == raw_step["completion_guards"]
        guard_kinds = [
            key
            for guard in step.completion_guards
            for key in guard
            if key.startswith("require_") or key == "required_conclusion_field"
        ]
        assert guard_kinds == [
            "require_structured_user_input_action",
            "require_context_field_equals",
            "require_structured_user_input_action",
            "require_structured_user_input_action",
            "require_structured_user_input_action",
            "require_tool_result",
            "require_context_constraint_coverage",
            "require_rollback_request",
        ]
