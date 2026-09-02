from types import SimpleNamespace
from typing import Any

import pytest

from iac_code.web.pipeline_actions import A2APipelineActionRunner, _ForwardingEventQueue


@pytest.mark.asyncio
async def test_forwarding_queue_invokes_observer():
    sink_calls = []
    observed = []

    async def sink(evs):
        sink_calls.append(evs)

    q = _ForwardingEventQueue(sink, envelope_observer=lambda env: observed.append(env))
    env = {"eventType": "input_required", "data": {"options": [{"candidate_index": 0}]}}
    await q.enqueue_local_pipeline_envelope(env)
    assert observed == [env]


@pytest.mark.asyncio
async def test_forwarding_queue_observer_error_does_not_break_sink():
    sink_calls = []

    async def sink(evs):
        sink_calls.append(evs)

    def boom(_env):
        raise RuntimeError("observer failed")

    q = _ForwardingEventQueue(sink, envelope_observer=boom)
    # push() must still run and sink must not be prevented from future events.
    await q.enqueue_local_pipeline_envelope({"eventType": "status", "data": {}})
    # No exception propagated == pass.


@pytest.mark.asyncio
async def test_forwarding_queue_hydrates_paused_step_before_forwarding_continuation():
    batches: list[list[dict[str, Any]]] = []

    async def sink(events: list[dict[str, Any]]) -> None:
        batches.append(events)

    step = {"id": "materialize_selected_candidate", "runId": "step-materialize-1", "index": 2, "total": 3}
    history = [
        {"eventType": "step_started", "scope": "step", "sequence": 1, "step": step, "data": {}},
        {
            "eventType": "step_completed",
            "scope": "step",
            "sequence": 2,
            "step": step,
            "data": {"durationS": 120.0},
        },
        {
            "eventType": "input_required",
            "scope": "step",
            "sequence": 3,
            "step": step,
            "data": {"kind": "deployment_confirmation", "prompt": "请选择下一步"},
        },
    ]
    queue = _ForwardingEventQueue(sink, history_envelopes=history)

    await queue.enqueue_local_pipeline_envelope(
        {
            "eventType": "input_received",
            "scope": "step",
            "sequence": 4,
            "step": step,
            "data": {"kind": "deployment_confirmation", "userInputLength": 12},
        }
    )
    await queue.enqueue_local_pipeline_envelope(
        {
            "eventType": "step_completed",
            "scope": "step",
            "sequence": 5,
            "step": step,
            "data": {"durationS": 92.0},
        }
    )

    markers = [event for batch in batches for event in batch if event["type"] == "pipeline.step.marker"]
    assert markers[0]["payload"]["pipelineStep"]["status"] == "working"
    assert markers[-1]["payload"]["pipelineStep"]["status"] == "completed"
    assert markers[-1]["payload"]["pipelineStep"]["durationS"] == 212.0


class _RecordingExecutor:
    """Stand in for IacCodeA2APipelineExecutor and record its construction kwargs."""

    constructions: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        type(self).constructions.append(kwargs)

    async def execute(self, **_kwargs: Any) -> None:
        return None


class _StubTaskStore:
    async def get_or_create_task(self, *, task_id: str, context_id: str) -> Any:
        return SimpleNamespace(id=task_id, context_id=context_id)

    async def get_task_record(self, _task_id: str) -> Any:
        return SimpleNamespace(state="working")


async def _executor_kwargs_for_session(monkeypatch: pytest.MonkeyPatch, session: Any) -> dict[str, Any]:
    import iac_code.a2a.pipeline_executor as pipeline_executor_module
    from iac_code.web import pipeline_actions

    async def no_snapshot(**_kwargs: Any) -> None:
        return None

    _RecordingExecutor.constructions = []
    monkeypatch.setattr(pipeline_executor_module, "IacCodeA2APipelineExecutor", _RecordingExecutor)
    monkeypatch.setattr(pipeline_actions, "load_pipeline_snapshot", no_snapshot)

    runner = A2APipelineActionRunner.__new__(A2APipelineActionRunner)
    runner._task_store = _StubTaskStore()
    runner._uses_web_global_defaults = False
    runner._owner = SimpleNamespace(
        model="qwen3.6-plus",
        metrics=None,
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        thinking_exposure_types=None,
        auto_approve_permissions=True,
    )

    result = await runner._execute(session, "起一套三层高可用架构", action="started", events=[])

    assert result.accepted is True
    assert len(_RecordingExecutor.constructions) == 1
    return _RecordingExecutor.constructions[0]


def _pipeline_session(**overrides: Any) -> Any:
    base = {
        "cwd": "/tmp/project",
        "task_id": "task-1",
        "context_id": "ctx-1",
        "model": None,
        "provider": None,
        "effort": None,
        "permission_mode": "bypass_permissions",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_execute_runs_the_pipeline_the_session_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    """模式选择器选中的 pipeline 必须传给执行器,否则会静默跑回旧 selling。"""
    session = _pipeline_session(pipeline_name="selling_solution_first")

    kwargs = await _executor_kwargs_for_session(monkeypatch, session)

    assert kwargs["pipeline_name"] == "selling_solution_first"


@pytest.mark.asyncio
async def test_execute_leaves_the_process_default_when_the_session_has_no_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """老会话(无 pipelineName)不带覆盖值,执行器继续用 IAC_CODE_PIPELINE_NAME/selling。"""
    session = _pipeline_session()

    kwargs = await _executor_kwargs_for_session(monkeypatch, session)

    assert kwargs["pipeline_name"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_name", ["no_such_pipeline", "   "])
async def test_execute_falls_back_when_the_session_pipeline_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    stored_name: str,
) -> None:
    """非法/空 pipelineName(settings.yml 手写错或旧构建遗留)必须回落进程默认,
    否则 create_pipeline 会 ValueError,让每一轮 pipeline 请求都 500。"""
    session = _pipeline_session(pipeline_name=stored_name)

    kwargs = await _executor_kwargs_for_session(monkeypatch, session)

    assert kwargs["pipeline_name"] is None


@pytest.mark.asyncio
async def test_execute_accepts_a_known_pipeline_name_with_surrounding_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _pipeline_session(pipeline_name="  selling  ")

    kwargs = await _executor_kwargs_for_session(monkeypatch, session)

    assert kwargs["pipeline_name"] == "selling"
