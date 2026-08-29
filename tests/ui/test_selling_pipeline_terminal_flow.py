from __future__ import annotations

import asyncio
import json
import time
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.types.stream_events import (
    AskUserQuestionEvent,
    CandidateDetailEvent,
    DiagramEvent,
    PermissionRequestEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolInputDeltaEvent,
    ToolUseStartEvent,
)
from iac_code.ui.renderer import Renderer
from iac_code.ui.repl import InlineREPL


class ClosableAsyncStream:
    def __init__(self, events):
        self._events = list(events)
        self.closed = False

    def __aiter__(self):
        self._iter = iter(self._events)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def aclose(self):
        self.closed = True


def _make_repl_for_selection(monkeypatch, key_sequence: list[str] | None = None):
    from iac_code.ui.core.key_event import KeyEvent

    repl = InlineREPL.__new__(InlineREPL)
    repl.renderer = MagicMock()
    repl.renderer.console = Console(file=StringIO(), width=120, height=30, force_terminal=True)
    repl.store = MagicMock()
    repl._pipeline_waiting_input = False
    repl._render_interrupt_feedback_inline = MagicMock()
    repl._test_live_refreshes = 0

    resumed_payloads: list[str] = []

    async def fake_render_pipeline_stream(_stream):
        return None

    repl._render_pipeline_stream = fake_render_pipeline_stream

    pipeline = MagicMock()
    pipeline.resume.side_effect = lambda payload: resumed_payloads.append(payload) or ClosableAsyncStream([])
    pipeline.pause_agent_loops = MagicMock()
    pipeline.resume_agent_loops = MagicMock()
    repl._pipeline = pipeline

    class FakeLive:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def update(self, *args, **kwargs):
            pass

        def refresh(self):
            repl._test_live_refreshes += 1

    monkeypatch.setattr("iac_code.ui.repl.Live", FakeLive)
    keys = list(key_sequence or ["enter"])

    class FakeCapture:
        def __init__(self, *args, **kwargs):
            self._keys = keys

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read_key(self, timeout):
            if not self._keys:
                return None
            if repl._pipeline_waiting_input:
                next_key = self._keys.pop(0)
                return KeyEvent(key=next_key, char="")
            time.sleep(0.01)
            return None

    monkeypatch.setattr("iac_code.ui.core.raw_input.RawInputCapture", FakeCapture)

    return repl, resumed_payloads


def test_deployment_confirmation_event_renders_a_compact_ask_style_solution_and_quote():
    output = StringIO()
    repl = InlineREPL.__new__(InlineREPL)
    console = Console(file=output, width=120, force_terminal=False, color_system=None)
    repl.renderer = Renderer(console, MagicMock())
    repl._pipeline_step_names = []
    event = PipelineEvent(
        type=PipelineEventType.USER_INPUT_REQUIRED,
        step_id="materialize_selected_candidate",
        timestamp=time.time(),
        data={
            "kind": "deployment_confirmation",
            "prompt": "请确认更新后的方案",
            "solution_summary": "杭州双 ECS 高可用方案",
            "cost": {
                "monthly_estimate": "¥1280/月（列表价，合同优惠后约¥1024/月）",
                "resources": [{"type": "ECS", "spec": "ecs.g7.large x 2", "cost": "¥480/月"}],
            },
            "effective_deployment_parameters": {"ZoneId": "cn-hangzhou-h"},
            "options": [
                {"action": "confirm", "name": "确认部署", "summary": "按当前方案创建资源"},
                {"action": "adjust", "name": "调整参数", "summary": "修改规格后重新询价"},
                {"action": "reselect", "name": "重新选择方案"},
                {"action": "cancel", "name": "取消"},
            ],
        },
    )

    repl._render_pipeline_event(event)

    rendered = output.getvalue()
    assert "杭州双 ECS 高可用方案" in rendered
    assert "¥1280/月（列表价，合同优惠后约¥1024/月）" in rendered
    assert "- ECS · ecs.g7.large x 2 · ¥480/月" in rendered
    assert "确认部署" not in rendered
    assert '"ZoneId": "cn-hangzhou-h"' not in rendered
    assert "parameter_overrides" not in rendered
    assert "PreviewStack" not in rendered


@pytest.mark.asyncio
async def test_deployment_confirmation_hides_adjust_and_keeps_free_text_as_the_last_row():
    output = StringIO()
    repl = InlineREPL.__new__(InlineREPL)
    repl.renderer = MagicMock()
    repl.renderer.console = Console(file=output, width=120, force_terminal=False, color_system=None)
    data = {
        "options": [
            {"action": "confirm", "name": "确认部署", "summary": "按当前方案创建资源"},
            {"action": "adjust", "name": "调整参数", "summary": "修改规格后重新询价"},
            {"action": "reselect", "name": "重新选择方案"},
            {"action": "cancel", "name": "取消"},
        ]
    }

    with patch("iac_code.ui.repl.Select") as select_cls:
        select_cls.return_value.run.return_value = "reselect"
        result = await repl._prompt_deployment_confirmation(data)

    assert json.loads(result) == {"action": "reselect"}
    options = select_cls.call_args.kwargs["options"]
    assert [option.value for option in options[:-1]] == ["confirm", "reselect", "cancel"]
    assert options[-1].value == "__deployment_confirmation_free_text__"
    assert select_cls.call_args.kwargs["layout"].value == "compact_vertical"
    assert select_cls.call_args.kwargs["type_to_edit_input"] is True
    select_cls.return_value.run.assert_called_once_with(console=repl.renderer.console)


@pytest.mark.asyncio
async def test_deployment_confirmation_last_row_returns_the_typed_text():
    repl = InlineREPL.__new__(InlineREPL)
    repl.renderer = MagicMock()
    repl.renderer.console = Console(file=StringIO(), width=120, force_terminal=False, color_system=None)

    with patch("iac_code.ui.repl.Select") as select_cls:
        selector = select_cls.return_value
        selector.run.return_value = "__deployment_confirmation_free_text__"
        selector.state.input_values = {
            "__deployment_confirmation_free_text__": "换成 ecs.g7.large 后重新询价",
        }
        result = await repl._prompt_deployment_confirmation({"options": []})

    assert result == "换成 ecs.g7.large 后重新询价"


@pytest.mark.asyncio
async def test_pipeline_stream_resumes_immediately_after_interactive_deployment_confirmation():
    repl = InlineREPL.__new__(InlineREPL)
    repl.renderer = MagicMock()
    repl.renderer.console = Console(file=StringIO(), width=120, force_terminal=False, color_system=None)
    repl.renderer.run_streaming_output = AsyncMock(return_value=None)
    repl._pipeline_step_names = []
    repl._pipeline_completed_indices = set()
    repl._pipeline_waiting_input = False
    repl._pipeline_display_recorder = None
    repl._pipeline_display_current_step_id = "materialize_selected_candidate"
    repl._render_pipeline_event = MagicMock()
    repl._record_pipeline_display_event = MagicMock()
    repl._prompt_deployment_confirmation = AsyncMock(return_value='{"action":"confirm"}')
    pipeline = MagicMock()
    terminal = PipelineEvent(
        type=PipelineEventType.PIPELINE_COMPLETED,
        step_id=None,
        timestamp=time.time(),
        data={},
    )
    resumed_stream = ClosableAsyncStream([terminal])
    pipeline.resume.return_value = resumed_stream
    repl._pipeline = pipeline
    waiting_stream = ClosableAsyncStream(
        [
            PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="materialize_selected_candidate",
                timestamp=time.time(),
                data={"kind": "deployment_confirmation", "options": []},
            )
        ]
    )

    result = await repl._render_pipeline_stream(waiting_stream)

    assert result is terminal
    assert waiting_stream.closed is True
    pipeline.resume.assert_called_once_with('{"action":"confirm"}')
    assert repl._pipeline_waiting_input is False


@pytest.mark.asyncio
async def test_pipeline_stream_reenters_candidate_ui_when_restored_stream_has_no_step_started():
    repl = InlineREPL.__new__(InlineREPL)
    repl.renderer = MagicMock()
    repl.renderer.console = Console(file=StringIO(), width=120, force_terminal=False, color_system=None)
    repl._pipeline_step_names = ["solution_planning_and_selection"]
    repl._pipeline_completed_indices = set()
    repl._pipeline_waiting_input = False
    repl._pipeline_display_recorder = None
    repl._pipeline_display_current_step_id = "solution_planning_and_selection"
    repl._render_pipeline_event = MagicMock()
    repl._record_pipeline_display_event = MagicMock()
    repl._resume_waiting_candidate_selection_from_sidecar = AsyncMock(return_value=None)
    pipeline = MagicMock()
    pipeline.feature_enabled.side_effect = lambda name: name == "repl_auto_resume_running_on_startup"
    repl._pipeline = pipeline
    waiting_stream = ClosableAsyncStream(
        [
            PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="solution_planning_and_selection",
                timestamp=time.time(),
                data={"kind": "candidate_selection", "options": [{"name": "Plan A"}]},
            )
        ]
    )

    result = await repl._render_pipeline_stream(waiting_stream)

    assert result is None
    assert waiting_stream.closed is True
    repl._resume_waiting_candidate_selection_from_sidecar.assert_awaited_once()
    assert repl._pipeline_waiting_input is True


@pytest.mark.asyncio
async def test_pipeline_stream_keeps_legacy_candidate_boundary_behavior_without_opt_in():
    repl = InlineREPL.__new__(InlineREPL)
    repl.renderer = MagicMock()
    repl.renderer.console = Console(file=StringIO(), width=120, force_terminal=False, color_system=None)
    repl._pipeline_step_names = ["confirm_and_select"]
    repl._pipeline_completed_indices = set()
    repl._pipeline_waiting_input = False
    repl._pipeline_display_recorder = None
    repl._pipeline_display_current_step_id = "confirm_and_select"
    repl._render_pipeline_event = MagicMock()
    repl._record_pipeline_display_event = MagicMock()
    repl._resume_waiting_candidate_selection_from_sidecar = AsyncMock(return_value=None)
    pipeline = MagicMock()
    pipeline.feature_enabled.return_value = False
    repl._pipeline = pipeline
    waiting_stream = ClosableAsyncStream(
        [
            PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="confirm_and_select",
                timestamp=time.time(),
                data={"kind": "candidate_selection", "options": [{"name": "Plan A"}]},
            )
        ]
    )

    result = await repl._render_pipeline_stream(waiting_stream)

    assert result is None
    repl._resume_waiting_candidate_selection_from_sidecar.assert_not_awaited()
    assert repl._pipeline_waiting_input is True


@pytest.mark.asyncio
async def test_pipeline_stream_restores_renderer_for_permission_request_after_confirmation_resume():
    repl = InlineREPL.__new__(InlineREPL)
    repl.renderer = MagicMock()
    repl.renderer.console = Console(file=StringIO(), width=120, force_terminal=False, color_system=None)
    repl.renderer.prompt_permission = AsyncMock(return_value=True)
    repl._pipeline_step_names = ["materialize_selected_candidate"]
    repl._pipeline_completed_indices = set()
    repl._pipeline_waiting_input = False
    repl._pipeline_display_recorder = None
    repl._pipeline_display_current_step_id = "materialize_selected_candidate"
    repl._render_pipeline_event = MagicMock()
    repl._record_pipeline_display_event = MagicMock()
    repl._record_pipeline_display_tool_use = MagicMock()
    repl._record_pipeline_display_stack_progress = MagicMock()
    repl._prompt_deployment_confirmation = AsyncMock(return_value='{"action":"confirm"}')

    terminal = PipelineEvent(
        type=PipelineEventType.PIPELINE_COMPLETED,
        step_id=None,
        timestamp=time.time(),
        data={},
    )
    permission_future = asyncio.get_running_loop().create_future()
    permission_event = PermissionRequestEvent(
        tool_name="write_file",
        tool_input={"path": "templates/1.yml", "content": "Resources: {}"},
        tool_use_id="tool-1",
        response_future=permission_future,
    )

    class PermissionStream:
        closed = False

        def __aiter__(self):
            return self._events()

        async def _events(self):
            yield PipelineEvent(
                type=PipelineEventType.USER_INPUT_RECEIVED,
                step_id="materialize_selected_candidate",
                timestamp=time.time(),
                data={"kind": "deployment_confirmation", "structured": True, "action": "confirm"},
            )
            yield permission_event
            await permission_future
            yield terminal

        async def aclose(self):
            self.closed = True

    resumed_stream = PermissionStream()
    pipeline = MagicMock()
    pipeline.resume.return_value = resumed_stream
    repl._pipeline = pipeline

    async def consume_agent_events(events, *, permission_handler, **_kwargs):
        async for event in events:
            if isinstance(event, PermissionRequestEvent):
                allowed = await permission_handler(event)
                if event.response_future is not None and not event.response_future.done():
                    event.response_future.set_result(allowed)

    repl.renderer.run_streaming_output = consume_agent_events
    waiting_stream = ClosableAsyncStream(
        [
            PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="materialize_selected_candidate",
                timestamp=time.time(),
                data={"kind": "deployment_confirmation", "options": []},
            )
        ]
    )

    result = await asyncio.wait_for(repl._render_pipeline_stream(waiting_stream), timeout=2)

    assert result is terminal
    assert permission_future.result() is True
    repl.renderer.prompt_permission.assert_awaited_once_with(permission_event)


@pytest.mark.asyncio
async def test_candidate_selection_shows_thinking_and_text_before_the_first_diagram(monkeypatch):
    repl, resumed_payloads = _make_repl_for_selection(monkeypatch)
    console = repl.renderer.console
    repl.renderer = Renderer(console, MagicMock())
    frames: list[str] = []

    class RecordingLive:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def update(self, content):
            output = StringIO()
            frame_console = Console(file=output, width=120, height=30, force_terminal=False, color_system=None)
            frame_console.print(content)
            frames.append(output.getvalue())

    monkeypatch.setattr("iac_code.ui.repl.Live", RecordingLive)
    stream = ClosableAsyncStream(
        [
            ThinkingDeltaEvent(text="正在分析用户需求"),
            TextDeltaEvent(text="需求清晰，开始规划候选方案。"),
            DiagramEvent("Plan A", "Resources: {}", "graph TD", candidate_index=0),
            CandidateDetailEvent("tu_a", "Plan A", "summary", [], "¥0/月", candidate_index=0),
            PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="solution_planning_and_selection",
                timestamp=time.time(),
                data={"prompt": "请选择", "options": [{"name": "Plan A", "summary": "summary"}]},
            ),
        ]
    )

    selected = await asyncio.wait_for(
        repl._render_candidate_selection_tabs(stream, show_agent_prelude=True),
        timeout=5,
    )

    assert selected == "Plan A"
    assert len(resumed_payloads) == 1
    thinking_frame = next(i for i, frame in enumerate(frames) if "正在分析用户需求" in frame)
    text_frame = next(i for i, frame in enumerate(frames) if "需求清晰，开始规划候选方案。" in frame)
    candidate_frame = next(i for i, frame in enumerate(frames) if "Plan A" in frame)
    assert thinking_frame < candidate_frame
    assert text_frame < candidate_frame
    assert "需求清晰，开始规划候选方案。" in console.file.getvalue()


@pytest.mark.asyncio
async def test_candidate_selection_resumes_with_structured_payload(monkeypatch):
    repl, resumed_payloads = _make_repl_for_selection(monkeypatch)

    stream = ClosableAsyncStream(
        [
            DiagramEvent("Plan A", "Resources: {}", "graph TD", candidate_index=0),
            CandidateDetailEvent("tu_a", "Plan A", "summary", [], "¥0/月", candidate_index=0),
            PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="confirm_and_select",
                timestamp=time.time(),
                data={"prompt": "请选择", "options": [{"name": "Plan A", "summary": "summary"}]},
            ),
        ]
    )

    selected = await asyncio.wait_for(repl._render_candidate_selection_tabs(stream), timeout=5)

    assert selected == "Plan A"
    assert stream.closed is True
    assert len(resumed_payloads) == 1
    payload = json.loads(resumed_payloads[0])
    assert payload == {
        "selected_candidate_name": "Plan A",
        "selected_candidate_index": 0,
        "selected_evaluated_candidate_index": 0,
    }


@pytest.mark.asyncio
async def test_candidate_selection_ready_is_recorded_only_after_key_input_is_ready(monkeypatch):
    repl, _resumed_payloads = _make_repl_for_selection(monkeypatch)
    observed_waiting_flags: list[bool] = []

    class Recorder:
        def record(self, event_type, **_kwargs):
            assert event_type == "candidate_selection_ready"
            observed_waiting_flags.append(repl._pipeline_waiting_input)

    repl._pipeline_display_recorder = Recorder()
    repl._pipeline_display_current_step_id = "solution_planning_and_selection"
    stream = ClosableAsyncStream(
        [
            CandidateDetailEvent("tu_a", "Plan A", "summary", [], "¥0/月", candidate_index=0),
            PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="solution_planning_and_selection",
                timestamp=time.time(),
                data={"prompt": "请选择", "options": [{"name": "Plan A", "summary": "summary"}]},
            ),
        ]
    )

    selected = await asyncio.wait_for(repl._render_candidate_selection_tabs(stream), timeout=5)

    assert selected == "Plan A"
    assert observed_waiting_flags == [True]


@pytest.mark.asyncio
async def test_candidate_selection_handles_step_question_before_candidates(monkeypatch):
    repl, resumed_payloads = _make_repl_for_selection(monkeypatch)
    answer = {"selected_id": "existing", "selected_label": "使用已有 VPC", "free_text": ""}
    repl.renderer.prompt_user_question = AsyncMock(return_value=answer)
    repl._persist_pending_ask_user_question = AsyncMock()
    repl._persist_pending_ask_user_question_answer = AsyncMock()
    repl._acknowledge_pending_ask_user_question = MagicMock()
    response_future = asyncio.get_running_loop().create_future()
    question = AskUserQuestionEvent(
        tool_use_id="ask-vpc",
        question="使用已有 VPC 还是新建 VPC？",
        options=[
            {"id": "existing", "label": "使用已有 VPC"},
            {"id": "create", "label": "新建 VPC"},
        ],
        response_future=response_future,
    )
    stream = ClosableAsyncStream(
        [
            question,
            DiagramEvent("Plan A", "Resources: {}", "graph TD", candidate_index=0),
            CandidateDetailEvent("tu_a", "Plan A", "summary", [], "¥0/月", candidate_index=0),
            PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="solution_planning_and_selection",
                timestamp=time.time(),
                data={"prompt": "请选择", "options": [{"name": "Plan A", "summary": "summary"}]},
            ),
        ]
    )

    selected = await asyncio.wait_for(repl._render_candidate_selection_tabs(stream), timeout=5)

    assert selected == "Plan A"
    assert response_future.result() == answer
    repl.renderer.prompt_user_question.assert_awaited_once_with(question)
    repl._persist_pending_ask_user_question.assert_awaited_once_with(question)
    repl._persist_pending_ask_user_question_answer.assert_awaited_once_with("ask-vpc", answer)
    repl._acknowledge_pending_ask_user_question.assert_called_once_with("ask-vpc")
    assert len(resumed_payloads) == 1


@pytest.mark.asyncio
async def test_candidate_selection_seeds_options_when_display_tools_are_missing(monkeypatch):
    repl, resumed_payloads = _make_repl_for_selection(monkeypatch)

    stream = ClosableAsyncStream(
        [
            PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="confirm_and_select",
                timestamp=time.time(),
                data={
                    "prompt": "请选择",
                    "options": [
                        {"name": "Plan A", "summary": "missing display", "candidate_index": 0},
                        {"name": "Plan B", "summary": "missing display", "candidate_index": 1},
                    ],
                },
            ),
        ]
    )

    selected = await asyncio.wait_for(repl._render_candidate_selection_tabs(stream), timeout=5)

    assert selected == "Plan A"
    assert repl._test_live_refreshes >= 1
    assert len(resumed_payloads) == 1
    payload = json.loads(resumed_payloads[0])
    assert payload == {
        "selected_candidate_name": "Plan A",
        "selected_candidate_index": 0,
        "selected_evaluated_candidate_index": 0,
    }


@pytest.mark.asyncio
async def test_streaming_candidate_detail_preserves_indexed_identity(monkeypatch):
    repl, resumed_payloads = _make_repl_for_selection(monkeypatch)

    stream = ClosableAsyncStream(
        [
            ToolUseStartEvent("tu_same", "show_candidate_detail"),
            ToolInputDeltaEvent(
                "tu_same",
                '{"candidate_name":"Same","candidate_index":0,"summary":"partial summary',
            ),
            CandidateDetailEvent("tu_same", "Same", "full summary", [], "¥0/月", candidate_index=0),
            PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="confirm_and_select",
                timestamp=time.time(),
                data={"prompt": "请选择", "options": [{"name": "Same", "summary": "full", "candidate_index": 0}]},
            ),
        ]
    )

    selected = await asyncio.wait_for(repl._render_candidate_selection_tabs(stream), timeout=5)

    assert selected == "Same"
    assert len(resumed_payloads) == 1
    payload = json.loads(resumed_payloads[0])
    assert payload == {
        "selected_candidate_name": "Same",
        "selected_candidate_index": 0,
        "selected_evaluated_candidate_index": 0,
    }


@pytest.mark.asyncio
async def test_candidate_selection_can_choose_second_duplicate_by_index(monkeypatch):
    repl, resumed_payloads = _make_repl_for_selection(monkeypatch, key_sequence=["right", "enter"])

    stream = ClosableAsyncStream(
        [
            DiagramEvent("Same", "Resources: {}", "graph TD\nA-->B", candidate_index=0),
            CandidateDetailEvent("tu_a", "Same", "first", [], "¥0/月", candidate_index=0),
            DiagramEvent("Same", "Resources: {}", "graph TD\nC-->D", candidate_index=1),
            CandidateDetailEvent("tu_b", "Same", "second", [], "¥0/月", candidate_index=1),
            PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="confirm_and_select",
                timestamp=time.time(),
                data={
                    "prompt": "请选择",
                    "options": [
                        {"name": "Same", "summary": "first", "candidate_index": 0},
                        {"name": "Same", "summary": "second", "candidate_index": 1},
                    ],
                },
            ),
        ]
    )

    selected = await asyncio.wait_for(repl._render_candidate_selection_tabs(stream), timeout=5)

    assert selected == "Same"
    assert len(resumed_payloads) == 1
    payload = json.loads(resumed_payloads[0])
    assert payload == {
        "selected_candidate_name": "Same",
        "selected_candidate_index": 1,
        "selected_evaluated_candidate_index": 1,
    }
    assert "Same #2" in repl.renderer.console.file.getvalue()
