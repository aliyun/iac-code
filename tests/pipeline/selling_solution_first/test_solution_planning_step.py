"""Step 1 ``solution_planning_and_selection``（设计文档 §18.2）。

Step 1 首次提交 ``awaiting_selection`` 后必须真的等待用户选择；恢复时结构化候选输入由 runner
在保存最终 conclusion 前固化为权威选择，但忽略部署参数覆盖；非法结构化选择不消耗等待态；
用户要求改架构或从 ``ask_user_question`` 恢复后再次输出 ``awaiting_selection`` 时不得误前进。
原 ``selling.confirm_and_select``（没有 ``status`` 字段）的恢复行为保持不变。
"""

from __future__ import annotations

import copy
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
from iac_code.pipeline.engine.types import StepResult, StepStatus
from iac_code.pipeline.engine.ui_contract import encode_selected_candidate
from iac_code.pipeline.selling_solution_first.tools.show_candidate_detail_tool import ShowCandidateDetailTool

STEP_ID = "solution_planning_and_selection"
CANDIDATES = [
    {
        "candidate_id": "cand-eco",
        "name": "方案A：单机经济型",
        "summary": "单台 ECS 自建 Nginx + 本地 MySQL",
        "output_path": "templates/1-single-ecs.yml",
        "rough_cost": {"currency": "CNY", "monthly_range": "¥120 - ¥180", "confidence": "medium"},
    },
    {
        "candidate_id": "cand-ha",
        "name": "方案B：高可用三层",
        "summary": "SLB + 2 台 ECS + RDS 高可用版",
        "output_path": "templates/2-high-availability-slb.yml",
        "rough_cost": {"currency": "CNY", "monthly_range": "¥1,100 - ¥1,500", "confidence": "medium"},
    },
]
OPTIONS = [
    {"name": CANDIDATES[0]["name"], "summary": CANDIDATES[0]["summary"], "candidate_index": 0},
    {"name": CANDIDATES[1]["name"], "summary": CANDIDATES[1]["summary"], "candidate_index": 1},
]


def _pipeline_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "src" / "iac_code" / "pipeline" / "selling_solution_first"


def _selling_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "src" / "iac_code" / "pipeline" / "selling"


def _awaiting(candidates=None, options=None) -> dict:
    return {
        "status": "awaiting_selection",
        "continue_pipeline": True,
        "is_infra_intent": True,
        "intent": {"business": "website"},
        "candidates": copy.deepcopy(candidates if candidates is not None else CANDIDATES),
        "user_prompt": "请选择要实现并部署的方案：",
        "options": copy.deepcopy(options if options is not None else OPTIONS),
    }


def _selected(*, index: int, name: str, candidate: dict, overrides: dict | None = None) -> dict:
    conclusion = _awaiting()
    conclusion.update(
        {
            "status": "selected",
            "selected_candidate_index": index,
            "selected_candidate_name": name,
            "selected_candidate": copy.deepcopy(candidate),
        }
    )
    if overrides is not None:
        conclusion["parameter_overrides"] = copy.deepcopy(overrides)
    return conclusion


class _Storage:
    def __init__(self, root: Path):
        self.root = root
        self.meta_entries: list[dict] = []

    def append_meta(self, cwd, session_id, meta):
        self.meta_entries.append(meta)

    def session_dir(self, cwd, session_id):
        return self.root / session_id

    def load(self, cwd, session_id):
        return []

    @staticmethod
    def repair_interrupted(messages):
        return messages


class _ScriptedExecutor:
    """Replace ``StepExecutor.execute`` with scripted conclusions per invocation."""

    def __init__(self, conclusions: list[dict]):
        self._conclusions = list(conclusions)
        self.calls: list[dict] = []

    async def execute(self, step, context, session_id, user_message=None, **kwargs):
        resolved_step_result = kwargs.get("resolved_step_result")
        if isinstance(resolved_step_result, StepResult):
            conclusion = copy.deepcopy(resolved_step_result.conclusion or {})
        else:
            conclusion = copy.deepcopy(self._conclusions.pop(0)) if self._conclusions else {"continue_pipeline": False}
        self.calls.append(
            {
                "step_id": step.step_id,
                "user_message": user_message,
                "precompleted_tools": copy.deepcopy(kwargs.get("precompleted_tools")),
                "resume_messages": copy.deepcopy(kwargs.get("resume_messages")),
                "resolved_step_result": copy.deepcopy(resolved_step_result),
                "context_snapshot": copy.deepcopy(context.snapshot()),
            }
        )
        context.set_conclusion(step.conclusion_field, conclusion)
        if isinstance(resolved_step_result, StepResult):
            yield resolved_step_result
        else:
            yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)


def _build_runner(
    tmp_path: Path, pipeline_dir: Path, conclusions: list[dict]
) -> tuple[PipelineRunner, _ScriptedExecutor]:
    runner = PipelineRunner(
        pipeline_dir=pipeline_dir,
        provider_manager=MagicMock(),
        base_tool_registry=MagicMock(),
        session_storage=_Storage(tmp_path / "sessions"),
        session_id="solution-first",
        cwd=str(tmp_path),
    )
    executor = _ScriptedExecutor(conclusions)
    runner._step_executor.execute = executor.execute
    return runner, executor


async def _drain(stream) -> list:
    events = []
    async for event in stream:
        events.append(event)
    return events


def _input_required(events) -> list[PipelineEvent]:
    return [
        event
        for event in events
        if isinstance(event, PipelineEvent) and event.type == PipelineEventType.USER_INPUT_REQUIRED
    ]


def _finalize_confirmation_for_test(step, context, *, user_message, tool_input, **kwargs):
    del kwargs
    status = tool_input["conclusion"]["status"]
    if status == "confirmed":
        conclusion = copy.deepcopy(context.get_conclusion("selected_plan"))
        conclusion.update({"status": status, "continue_pipeline": True, "deployment_confirmed": True})
        conclusion.pop("user_prompt", None)
        conclusion.pop("options", None)
        conclusion["confirmation"] = {
            "action": "confirm",
            "input_type": "structured",
            "user_input": user_message,
            "parameter_overrides": copy.deepcopy(conclusion.get("parameter_overrides", {})),
        }
        return StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)
    if status == "cancelled":
        conclusion = {
            "status": status,
            "continue_pipeline": False,
            "deployment_confirmed": False,
            "cancellation_reason": user_message,
        }
        return StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)
    conclusion = {
        "status": status,
        "continue_pipeline": True,
        "deployment_confirmed": False,
        "reselect_reason": user_message,
    }
    return StepResult(
        step_id=step.step_id,
        status=StepStatus.COMPLETED,
        conclusion=conclusion,
        rollback_request=("solution_planning_and_selection", user_message),
    )


async def _run_to_selection_wait(runner) -> list:
    stream = runner.run("帮我把 Nginx 网站部署到阿里云，预算每月 1500")
    events = []
    try:
        async for event in stream:
            events.append(event)
            if (
                isinstance(event, PipelineEvent)
                and event.type == PipelineEventType.USER_INPUT_REQUIRED
                and event.step_id == STEP_ID
            ):
                return events
    finally:
        await stream.aclose()
    raise AssertionError("pipeline never waited for candidate selection")


@pytest.fixture
def exit_after_selection() -> dict:
    """Step 2 conclusion that ends the run, so tests observe Step 1 handoff only."""

    return {"status": "cancelled", "continue_pipeline": False, "deployment_confirmed": False}


class TestAwaitingSelection:
    @pytest.mark.asyncio
    async def test_first_conclusion_waits_for_the_user_choice(self, tmp_path):
        runner, executor = _build_runner(tmp_path, _pipeline_dir(), [_awaiting()])

        events = await _run_to_selection_wait(runner)

        waits = _input_required(events)
        assert [event.step_id for event in waits] == [STEP_ID]
        assert waits[0].data["options"] == OPTIONS
        assert waits[0].data["prompt"] == "请选择要实现并部署的方案："
        # 仍停在 Step 1，没有前进到实现步骤。
        assert runner.state_machine.current_step.step_id == STEP_ID
        assert [call["step_id"] for call in executor.calls] == [STEP_ID]
        assert runner._waiting_input_options_by_step[STEP_ID] == OPTIONS

    @pytest.mark.asyncio
    async def test_single_candidate_still_enters_selection(self, tmp_path):
        one = [CANDIDATES[0]]
        options = [{"name": CANDIDATES[0]["name"], "candidate_index": 0}]
        runner, _executor = _build_runner(tmp_path, _pipeline_dir(), [_awaiting(one, options)])

        events = await _run_to_selection_wait(runner)

        assert len(_input_required(events)) == 1
        assert runner.state_machine.current_step.step_id == STEP_ID

    @pytest.mark.asyncio
    async def test_invalid_structured_selection_does_not_consume_the_wait(self, tmp_path):
        runner, executor = _build_runner(tmp_path, _pipeline_dir(), [_awaiting()])
        await _run_to_selection_wait(runner)

        events = await _drain(runner.resume(encode_selected_candidate("方案A：单机经济型", 7)))

        waits = _input_required(events)
        assert len(waits) == 1
        assert waits[0].data["validation_error"] == "invalid_candidate_selection"
        assert waits[0].data["options"] == OPTIONS
        # 等待态没有被消耗：Step 1 没有被重新执行，选项仍然保留。
        assert [call["step_id"] for call in executor.calls] == [STEP_ID]
        assert runner._waiting_input_options_by_step[STEP_ID] == OPTIONS
        assert runner.state_machine.current_step.step_id == STEP_ID


class TestAuthoritativeSelection:
    @pytest.mark.asyncio
    async def test_structured_choice_overrides_a_different_candidate_written_back_by_the_model(
        self, tmp_path, exit_after_selection
    ):
        # 模型回写了另一个候选；runner 必须按结构化坐标固化权威候选。
        model_conclusion = _selected(index=0, name=CANDIDATES[0]["name"], candidate=CANDIDATES[0])
        runner, executor = _build_runner(
            tmp_path, _pipeline_dir(), [_awaiting(), model_conclusion, exit_after_selection]
        )
        await _run_to_selection_wait(runner)

        await _drain(
            runner.resume(
                encode_selected_candidate(
                    CANDIDATES[1]["name"], 1, {"ZoneId": "cn-hangzhou-k", "InstanceType": "ecs.g7.large"}
                )
            )
        )

        saved = runner.context.get_conclusion("solution_selection")
        assert saved["selected_candidate_index"] == 1
        assert saved["selected_candidate_name"] == CANDIDATES[1]["name"]
        assert saved["selected_candidate"] == CANDIDATES[1]
        assert "parameter_overrides" not in saved
        # 权威候选是候选列表项的副本，改它不会污染候选列表。
        saved["selected_candidate"]["name"] = "被改过"
        assert saved["candidates"][1]["name"] == CANDIDATES[1]["name"]

    @staticmethod
    def _waiting_plan(monthly_estimate: str = "¥1,024/月") -> dict:
        return {
            "status": "awaiting_confirmation",
            "continue_pipeline": True,
            "deployment_confirmed": False,
            "selection_valid": True,
            "selected_candidate": copy.deepcopy(CANDIDATES[1]),
            "selected_candidate_result": {
                "candidate": copy.deepcopy(CANDIDATES[1]),
                "solution_summary": "SLB + 双 ECS + RDS 高可用方案",
                "template": {"file_path": "templates/2-high-availability-slb.yml"},
                "cost": {
                    "monthly_estimate": monthly_estimate,
                    "resources": [{"type": "ECS", "spec": "ecs.g7.large x 2", "cost": "¥480/月"}],
                },
            },
            "template_url": "templates/2-high-availability-slb.yml",
            "parameter_overrides": {},
            "effective_deployment_parameters": {"ZoneId": "cn-hangzhou-h"},
            "preview_ready_for_create": True,
            "user_prompt": "请确认更新后的方案与 ROS 询价",
            "options": [
                {"action": "confirm", "name": "确认部署"},
                {"action": "reselect", "name": "重新选择方案"},
                {"action": "cancel", "name": "取消"},
            ],
        }

    @pytest.mark.asyncio
    async def test_step_two_emits_a_dedicated_confirmation_payload_and_waits_again_after_adjustment(self, tmp_path):
        selected = _selected(index=1, name=CANDIDATES[1]["name"], candidate=CANDIDATES[1])
        runner, executor = _build_runner(
            tmp_path,
            _pipeline_dir(),
            [_awaiting(), selected, self._waiting_plan(), self._waiting_plan("¥1,280/月")],
        )
        await _run_to_selection_wait(runner)

        first_resume = await _drain(runner.resume(encode_selected_candidate(CANDIDATES[1]["name"], 1)))
        confirmation_wait = next(
            event for event in _input_required(first_resume) if event.step_id == "materialize_selected_candidate"
        )
        assert confirmation_wait.data["kind"] == "deployment_confirmation"
        assert confirmation_wait.data["solution_summary"] == "SLB + 双 ECS + RDS 高可用方案"
        assert confirmation_wait.data["cost"]["monthly_estimate"] == "¥1,024/月"
        assert confirmation_wait.data["effective_deployment_parameters"] == {"ZoneId": "cn-hangzhou-h"}
        assert runner.state_machine.current_step.step_id == "materialize_selected_candidate"

        adjustment = '{"action":"adjust","parameter_overrides":{"InstanceType":"ecs.g7.xlarge"}}'
        second_resume = await _drain(runner.resume(adjustment))
        waits = [event for event in _input_required(second_resume) if event.step_id == "materialize_selected_candidate"]
        assert len(waits) == 1
        assert waits[0].data["cost"]["monthly_estimate"] == "¥1,280/月"
        assert executor.calls[-1]["user_message"] == adjustment
        received = next(
            event
            for event in second_resume
            if isinstance(event, PipelineEvent) and event.type == PipelineEventType.USER_INPUT_RECEIVED
        )
        assert received.data["kind"] == "deployment_confirmation"
        assert received.data["structured"] is True
        assert received.data["action"] == "adjust"
        assert received.data["parameter_overrides"] == {"InstanceType": "ecs.g7.xlarge"}

    @pytest.mark.asyncio
    async def test_unchanged_structured_confirm_is_resolved_once_and_advances_to_deployment(self, tmp_path):
        selected = _selected(index=1, name=CANDIDATES[1]["name"], candidate=CANDIDATES[1])
        deployment = {"status": "succeeded", "continue_pipeline": True}
        waiting_plan = self._waiting_plan()
        waiting_plan["parameter_overrides"] = {"ZoneId": "cn-hangzhou-h"}
        runner, executor = _build_runner(
            tmp_path,
            _pipeline_dir(),
            [_awaiting(), selected, waiting_plan, deployment],
        )
        await _run_to_selection_wait(runner)
        await _drain(runner.resume(encode_selected_candidate(CANDIDATES[1]["name"], 1)))
        runner._step_executor.finalize_completion_input_from_transcript = MagicMock(
            side_effect=_finalize_confirmation_for_test
        )

        events = await _drain(runner.resume('{"action":"confirm","parameter_overrides":{}}'))

        assert not [event for event in _input_required(events) if event.step_id == "materialize_selected_candidate"]
        assert [call["step_id"] for call in executor.calls] == [
            STEP_ID,
            STEP_ID,
            "materialize_selected_candidate",
            "materialize_selected_candidate",
            "deploying",
        ]
        confirmation_call = executor.calls[-2]
        assert confirmation_call["resolved_step_result"].conclusion["status"] == "confirmed"
        assert confirmation_call["resolved_step_result"].conclusion["confirmation"] == {
            "action": "confirm",
            "input_type": "structured",
            "user_input": '{"action":"confirm","parameter_overrides":{}}',
            "parameter_overrides": {"ZoneId": "cn-hangzhou-h"},
        }
        assert runner.context.get_conclusion("selected_plan")["status"] == "confirmed"
        assert runner.context.get_conclusion("deployment") == deployment

    @pytest.mark.asyncio
    async def test_natural_language_confirmation_input_is_forwarded_to_the_llm(self, tmp_path):
        selected = _selected(index=1, name=CANDIDATES[1]["name"], candidate=CANDIDATES[1])
        cancelled = {"status": "cancelled", "continue_pipeline": False, "deployment_confirmed": False}
        runner, executor = _build_runner(
            tmp_path,
            _pipeline_dir(),
            [_awaiting(), selected, self._waiting_plan(), cancelled],
        )
        await _run_to_selection_wait(runner)
        await _drain(runner.resume(encode_selected_candidate(CANDIDATES[1]["name"], 1)))

        events = await _drain(runner.resume("先不要部署了"))

        received = next(
            event
            for event in events
            if isinstance(event, PipelineEvent) and event.type == PipelineEventType.USER_INPUT_RECEIVED
        )
        assert received.data["kind"] == "deployment_confirmation"
        assert received.data["structured"] is False
        assert executor.calls[-1]["user_message"] == "先不要部署了"
        assert "user_input" not in executor.calls[-1]["context_snapshot"]["selected_plan"]

    @pytest.mark.asyncio
    async def test_numbered_confirmation_choice_is_sent_as_a_structured_action(self, tmp_path):
        selected = _selected(index=1, name=CANDIDATES[1]["name"], candidate=CANDIDATES[1])
        cancelled = {"status": "cancelled", "continue_pipeline": False, "deployment_confirmed": False}
        runner, executor = _build_runner(
            tmp_path,
            _pipeline_dir(),
            [_awaiting(), selected, self._waiting_plan(), cancelled],
        )
        await _run_to_selection_wait(runner)
        await _drain(runner.resume(encode_selected_candidate(CANDIDATES[1]["name"], 1)))
        runner._step_executor.finalize_completion_input_from_transcript = MagicMock(
            side_effect=_finalize_confirmation_for_test
        )

        events = await _drain(runner.resume("3"))

        received = next(
            event
            for event in events
            if isinstance(event, PipelineEvent) and event.type == PipelineEventType.USER_INPUT_RECEIVED
        )
        assert received.data["structured"] is True
        assert received.data["action"] == "cancel"
        assert executor.calls[-1]["user_message"] == '{"action": "cancel"}'
        assert executor.calls[-1]["resolved_step_result"].conclusion == {
            "status": "cancelled",
            "continue_pipeline": False,
            "deployment_confirmed": False,
            "cancellation_reason": '{"action": "cancel"}',
        }

    @pytest.mark.asyncio
    async def test_structured_reselect_is_resolved_once_and_rolls_back_without_llm_interpretation(self, tmp_path):
        selected = _selected(index=1, name=CANDIDATES[1]["name"], candidate=CANDIDATES[1])
        runner, executor = _build_runner(
            tmp_path,
            _pipeline_dir(),
            [_awaiting(), selected, self._waiting_plan(), _awaiting()],
        )
        await _run_to_selection_wait(runner)
        await _drain(runner.resume(encode_selected_candidate(CANDIDATES[1]["name"], 1)))
        runner._step_executor.finalize_completion_input_from_transcript = MagicMock(
            side_effect=_finalize_confirmation_for_test
        )

        events = await _drain(runner.resume('{"action":"reselect"}'))

        resolved = executor.calls[-2]["resolved_step_result"]
        assert resolved.conclusion == {
            "status": "reselect_requested",
            "continue_pipeline": True,
            "deployment_confirmed": False,
            "reselect_reason": '{"action":"reselect"}',
        }
        assert resolved.rollback_request == ("solution_planning_and_selection", '{"action":"reselect"}')
        waits = _input_required(events)
        assert [event.step_id for event in waits] == [STEP_ID]
        assert runner.state_machine.current_step.step_id == STEP_ID

    @pytest.mark.asyncio
    async def test_parameter_overrides_from_step_one_are_ignored(self, tmp_path, exit_after_selection):
        model_conclusion = _selected(index=1, name=CANDIDATES[1]["name"], candidate=CANDIDATES[1])
        runner, executor = _build_runner(
            tmp_path, _pipeline_dir(), [_awaiting(), model_conclusion, exit_after_selection]
        )
        await _run_to_selection_wait(runner)

        await _drain(runner.resume(encode_selected_candidate(CANDIDATES[1]["name"], 1, {"ZoneId": "cn-hangzhou-k"})))

        assert [call["step_id"] for call in executor.calls] == [
            STEP_ID,
            STEP_ID,
            "materialize_selected_candidate",
        ]
        handed_over = executor.calls[-1]["context_snapshot"]["solution_selection"]
        assert "parameter_overrides" not in handed_over
        assert handed_over["selected_candidate"] == CANDIDATES[1]
        assert handed_over["selected_candidate_index"] == 1

    @pytest.mark.asyncio
    async def test_structured_choice_by_name_only_is_resolved_against_the_candidate_list(
        self, tmp_path, exit_after_selection
    ):
        model_conclusion = _selected(index=0, name=CANDIDATES[0]["name"], candidate=CANDIDATES[0])
        runner, _executor = _build_runner(
            tmp_path, _pipeline_dir(), [_awaiting(), model_conclusion, exit_after_selection]
        )
        await _run_to_selection_wait(runner)

        await _drain(runner.resume(CANDIDATES[1]["name"]))

        saved = runner.context.get_conclusion("solution_selection")
        assert saved["selected_candidate_index"] == 1
        assert saved["selected_candidate"] == CANDIDATES[1]

    @pytest.mark.asyncio
    async def test_natural_language_preference_uses_the_validated_model_mapping(self, tmp_path, exit_after_selection):
        model_conclusion = _selected(index=1, name=CANDIDATES[1]["name"], candidate={"name": "模型自己拼的对象"})
        runner, _executor = _build_runner(
            tmp_path, _pipeline_dir(), [_awaiting(), model_conclusion, exit_after_selection]
        )
        await _run_to_selection_wait(runner)

        await _drain(runner.resume("我要高可用的那个"))

        saved = runner.context.get_conclusion("solution_selection")
        assert saved["selected_candidate_index"] == 1
        # 模型解析的下标经候选列表验证后，权威候选对象由 runner 从候选列表补齐。
        assert saved["selected_candidate"] == CANDIDATES[1]
        assert saved["selected_candidate_name"] == CANDIDATES[1]["name"]

    @pytest.mark.asyncio
    async def test_model_mapping_outside_the_candidate_list_is_not_fabricated(self, tmp_path, exit_after_selection):
        model_conclusion = _selected(index=9, name="不存在的方案", candidate={"name": "不存在的方案"})
        runner, _executor = _build_runner(
            tmp_path, _pipeline_dir(), [_awaiting(), model_conclusion, exit_after_selection]
        )
        await _run_to_selection_wait(runner)

        await _drain(runner.resume("随便挑一个吧"))

        saved = runner.context.get_conclusion("solution_selection")
        # runner 无法验证时不编造选择，交给 Step 2 的 on_enter 判定 selection_valid。
        assert saved["selected_candidate_index"] == 9
        assert saved["selected_candidate"] == {"name": "不存在的方案"}

    @pytest.mark.asyncio
    async def test_single_candidate_natural_language_choice_resolves_to_index_zero(
        self, tmp_path, exit_after_selection
    ):
        one = [CANDIDATES[0]]
        options = [{"name": CANDIDATES[0]["name"], "candidate_index": 0}]
        model_conclusion = _awaiting(one, options)
        model_conclusion.update({"status": "selected", "selected_candidate_name": "", "selected_candidate": {}})
        runner, _executor = _build_runner(
            tmp_path, _pipeline_dir(), [_awaiting(one, options), model_conclusion, exit_after_selection]
        )
        await _run_to_selection_wait(runner)

        await _drain(runner.resume("就用这个"))

        saved = runner.context.get_conclusion("solution_selection")
        assert saved["selected_candidate_index"] == 0
        assert saved["selected_candidate"] == CANDIDATES[0]


class TestReselectAndAskResume:
    @pytest.mark.asyncio
    async def test_architecture_change_request_waits_for_selection_again(self, tmp_path):
        new_candidates = [dict(CANDIDATES[0], name="方案A′：加 Redis"), CANDIDATES[1]]
        new_options = [{"name": new_candidates[0]["name"], "candidate_index": 0}, dict(OPTIONS[1])]
        runner, executor = _build_runner(
            tmp_path, _pipeline_dir(), [_awaiting(), _awaiting(new_candidates, new_options)]
        )
        await _run_to_selection_wait(runner)

        events = await _drain(runner.resume("加一个 Redis 缓存"))

        waits = _input_required(events)
        assert [event.step_id for event in waits] == [STEP_ID]
        assert waits[0].data["options"] == new_options
        # 重新规划后仍停在 Step 1，没有把 awaiting_selection 当成选择结果前进。
        assert runner.state_machine.current_step.step_id == STEP_ID
        assert [call["step_id"] for call in executor.calls] == [STEP_ID, STEP_ID]
        assert runner._waiting_input_options_by_step[STEP_ID] == new_options

    @pytest.mark.asyncio
    async def test_second_selection_after_replanning_still_fixes_the_authoritative_candidate(
        self, tmp_path, exit_after_selection
    ):
        new_candidates = [dict(CANDIDATES[0], name="方案A′：加 Redis"), CANDIDATES[1]]
        new_options = [{"name": new_candidates[0]["name"], "candidate_index": 0}, dict(OPTIONS[1])]
        model_conclusion = _awaiting(new_candidates, new_options)
        model_conclusion.update(
            {
                "status": "selected",
                "selected_candidate_index": 1,
                "selected_candidate_name": CANDIDATES[1]["name"],
                "selected_candidate": copy.deepcopy(CANDIDATES[1]),
            }
        )
        runner, _executor = _build_runner(
            tmp_path,
            _pipeline_dir(),
            [_awaiting(), _awaiting(new_candidates, new_options), model_conclusion, exit_after_selection],
        )
        await _run_to_selection_wait(runner)
        await _drain(runner.resume("加一个 Redis 缓存"))

        await _drain(runner.resume(encode_selected_candidate(new_candidates[0]["name"], 0)))

        saved = runner.context.get_conclusion("solution_selection")
        assert saved["selected_candidate_index"] == 0
        assert saved["selected_candidate"] == new_candidates[0]
        assert runner.state_machine.current_step.step_id == "materialize_selected_candidate"

    @pytest.mark.asyncio
    async def test_ask_user_question_resume_that_returns_awaiting_selection_does_not_advance(self, tmp_path):
        runner, executor = _build_runner(tmp_path, _pipeline_dir(), [_awaiting()])

        events = await _drain(
            runner.resume_ask_user_question(
                {"selected_id": "nginx", "selected_label": "Nginx 静态站", "free_text": ""},
                tool_use_id="ask-1",
            )
        )

        waits = _input_required(events)
        assert [event.step_id for event in waits] == [STEP_ID]
        assert runner.state_machine.current_step.step_id == STEP_ID
        # 真实回答以 precompleted tool result 注入，Step 1 在同一次尝试内继续。
        assert executor.calls[0]["precompleted_tools"] == {
            "ask_user_question": {"selected_id": "nginx", "selected_label": "Nginx 静态站", "free_text": ""}
        }


class TestSellingCandidateStepRegression:
    def test_selling_candidate_step_has_no_status_field(self):
        raw = yaml.safe_load((_selling_dir() / "pipeline.yaml").read_text(encoding="utf-8"))
        step = next(item for item in raw["steps"] if item.get("ui_mode") == "candidate_selection")

        assert step["id"] == "confirm_and_select"
        # 新的权威选择固化只在 status == "selected" 时生效，因此对原 selling 天然 no-op。
        assert "status" not in step["conclusion_schema"]["properties"]
        assert "candidates" not in step["conclusion_schema"]["properties"]

    @pytest.mark.asyncio
    async def test_selling_confirm_and_select_resume_keeps_the_model_written_selection(self, tmp_path):
        model_conclusion = {
            "user_prompt": "请选择方案",
            "options": copy.deepcopy(OPTIONS),
            "selected_candidate_index": 0,
            "selected_candidate_name": CANDIDATES[0]["name"],
        }
        runner, executor = _build_runner(tmp_path, _selling_dir(), [model_conclusion, {"continue_pipeline": False}])
        while runner.state_machine.current_step.step_id != "confirm_and_select":
            runner.state_machine.advance()
        step = runner.state_machine.current_step
        runner.context.set_conclusion(
            step.conclusion_field, {"user_prompt": "请选择方案", "options": copy.deepcopy(OPTIONS)}
        )
        runner._waiting_input_options_by_step[step.step_id] = copy.deepcopy(OPTIONS)

        await _drain(runner.resume(encode_selected_candidate(CANDIDATES[1]["name"], 1)))

        saved = runner.context.get_conclusion(step.conclusion_field)
        # 原行为：runner 不改写候选选择，也不注入 selected_candidate。
        assert saved["selected_candidate_index"] == 0
        assert saved["selected_candidate_name"] == CANDIDATES[0]["name"]
        assert "selected_candidate" not in saved
        assert [call["step_id"] for call in executor.calls] == ["confirm_and_select", "deploying"]


class TestStepOneContract:
    @pytest.fixture(scope="class")
    def raw_step(self) -> dict:
        raw = yaml.safe_load((_pipeline_dir() / "pipeline.yaml").read_text(encoding="utf-8"))
        return next(item for item in raw["steps"] if item["id"] == STEP_ID)

    @pytest.fixture(scope="class")
    def prompt_text(self) -> str:
        return (_pipeline_dir() / "prompts" / "solution_planning_and_selection.md").read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    def skill_text(self) -> str:
        return (_pipeline_dir() / "skills" / "iac-aliyun-solution-first" / "SKILL.md").read_text(encoding="utf-8")

    def test_conclusion_schema_covers_the_three_outcomes(self, raw_step):
        schema = raw_step["conclusion_schema"]

        assert schema["properties"]["status"]["enum"] == ["awaiting_selection", "selected", "rejected"]
        awaiting, selected, rejected = schema["allOf"]
        assert set(awaiting["then"]["required"]) >= {"candidates", "user_prompt", "options"}
        assert set(selected["then"]["required"]) >= {
            "selected_candidate_name",
            "selected_candidate_index",
            "selected_candidate",
        }
        assert rejected["then"]["properties"]["continue_pipeline"]["const"] is False
        assert rejected["then"]["properties"]["is_infra_intent"]["const"] is False

    def test_every_candidate_carries_inventory_graph_and_rough_cost(self, raw_step):
        candidate = raw_step["conclusion_schema"]["properties"]["candidates"]["items"]

        assert set(candidate["required"]) >= {
            "resource_intents",
            "hard_constraints",
            "topology_graph",
            "resource_inventory",
            "rough_cost",
            "why_recommended",
            "problems_solved",
            "pros",
            "cons",
        }
        rough_cost = candidate["properties"]["rough_cost"]
        assert set(rough_cost["required"]) == {
            "currency",
            "monthly_range",
            "items",
            "assumptions",
            "exclusions",
            "confidence",
        }
        assert rough_cost["properties"]["confidence"]["enum"] == ["high", "medium", "low"]

    def test_persuasion_fields_are_runtime_fields_owned_by_the_detail_tool(self, raw_step):
        public_candidate = raw_step["conclusion_schema"]["properties"]["candidates"]["items"]
        completion_fields = raw_step["completion_input_schema"]["properties"]
        detail_fields = ShowCandidateDetailTool().input_schema["properties"]
        notes = detail_fields["decision_notes"]

        # 公共 conclusion 把说服力字段摊平成候选顶层字段，界面直接渲染。
        for field in ("why_recommended", "problems_solved", "pros", "cons"):
            assert public_candidate["properties"][field]["type"] == "array"
        # complete_step 不再复制候选；逐候选详情工具仍用 required + minItems 保证完整说服力。
        assert "candidates" not in completion_fields
        assert "decision_notes" in ShowCandidateDetailTool().input_schema["required"]
        assert set(notes["required"]) == {"why_recommended", "problems_solved", "pros", "cons"}
        assert notes["properties"]["why_recommended"]["minItems"] == 1
        assert notes["properties"]["problems_solved"]["minItems"] == 1
        assert notes["properties"]["pros"]["minItems"] == 2
        assert notes["properties"]["cons"]["minItems"] == 1

    def test_compact_completion_requires_authoritative_resource_lifecycle(self, raw_step):
        intent = raw_step["completion_input_schema"]["properties"]["intent"]
        resource_intents = intent["properties"]["resource_intents"]

        assert set(intent["required"]) == {"resource_intents", "hard_constraints"}
        assert resource_intents["minItems"] == 1
        assert all(
            action in resource_intents["description"]
            for action in (
                "create",
                "use_existing",
                "reference",
                "forbid",
            )
        )
        assert "ECS:forbid" in resource_intents["description"]

    def test_options_require_the_candidate_index_coordinate(self, raw_step):
        options = raw_step["conclusion_schema"]["properties"]["options"]

        assert options["items"]["required"] == ["name", "candidate_index"]

    def test_skill_requires_clarification_before_planning(self, skill_text):
        assert "先调用 `ask_user_question`" in skill_text
        assert "本流程只支持阿里云" in skill_text
        assert "status: rejected" in skill_text
        assert "不通过回退或重启步骤做澄清" in skill_text

    def test_skill_pins_candidate_count_rules(self, skill_text):
        assert "只给 1 个方案" in skill_text
        assert "给出 2-3 个有实质差异的方案" in skill_text
        assert "不允许跳过选择直接实现方案" in skill_text

    def test_skill_uses_one_candidate_coordinate_for_both_display_tools(self, skill_text):
        assert "show_architecture_plan" in skill_text
        assert "show_candidate_detail" in skill_text
        assert "`options[i].candidate_index == i`" in skill_text
        assert "`candidate_id`、`output_path`" in skill_text
        assert "由 Python" in skill_text
        assert "`rough_cost.monthly_range`" in skill_text
        # decision_notes 现在是完整的说服力字段（见「方案说服力」），仍要支撑 Python 生成紧凑 options。
        assert "Python 生成紧凑 options" in skill_text

    def test_skill_requires_traceable_persuasion_content(self, skill_text):
        assert "### 方案说服力" in skill_text
        assert "`why_recommended`" in skill_text
        assert "`problems_solved`" in skill_text
        assert "每个模型轮次只细化一个候选" in skill_text
        assert "「性能好」「高可用」「稳定可靠」" in skill_text
        assert "不要给所有候选写同一套优劣" in skill_text
        assert "只有 1 个候选时同样必填" in skill_text

    def test_skill_keeps_rough_pricing_in_step_one_only(self, skill_text):
        assert "架构粗估" in skill_text
        assert "不调用** `ros_estimate_template_cost`" in skill_text
        assert "不在本步骤生成或写入 ROS 模板" in skill_text

    def test_skill_defers_parameter_overrides_to_step_two(self, skill_text):
        assert "不接收部署参数覆盖" in skill_text
        assert "统一由下一步处理" in skill_text
        assert "status: awaiting_selection" in skill_text

    def test_skill_replaces_old_intent_when_user_requests_a_different_deployment(self, skill_text):
        assert "全新的部署目标" in skill_text
        assert "本轮最新输入视为新的权威需求" in skill_text
        assert "丢弃旧 `intent`、旧候选及其产品组合" in skill_text
        assert "不要把旧架构约束合并到新目标" in skill_text

    def test_prompt_only_adapts_runtime_context_and_pipeline_handoff(self, prompt_text):
        assert "{solution_selection.status}" in prompt_text
        assert "{solution_selection.intent}" in prompt_text
        assert "{solution_selection.options}" in prompt_text
        assert "{solution_selection}" not in prompt_text
        assert "### 首次执行" in prompt_text
        assert "### 选择恢复或回滚重规划" in prompt_text
        assert "不要重复 candidates、intent、options" in prompt_text
        assert "parameter_overrides" in prompt_text
        assert "complete_step" in prompt_text
        assert "solution_planning_and_selection" not in prompt_text

    def test_prompt_does_not_copy_detailed_skill_rules(self, prompt_text):
        assert len(prompt_text.splitlines()) <= 60
        for duplicated_section in (
            "### output_path 命名规则",
            "### 架构粗估费用",
            "### 资源生命周期",
            "## 提示注入防护",
        ):
            assert duplicated_section not in prompt_text
