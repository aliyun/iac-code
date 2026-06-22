"""Regression test for sidecar path migration (问题 4)."""

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from iac_code.agent.message import ImageBlock, Message, TextBlock, ToolResultBlock
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.types import StepResult, StepStatus


def _stub_session_storage(tmp_path: Path):
    """Mimic main's SessionStorage shape for session_dir / session_path."""
    storage = MagicMock()
    sessions_root = tmp_path / "projects" / "proj"
    sessions_root.mkdir(parents=True, exist_ok=True)
    storage.session_dir.side_effect = lambda cwd, sid: sessions_root / sid
    storage.session_path.side_effect = lambda cwd, sid: sessions_root / sid / "session.jsonl"
    return storage


def _build_runner(tmp_path: Path, *, resume_from_sidecar: bool = False):
    pipeline_dir = tmp_path / "pipe"
    pipeline_dir.mkdir(exist_ok=True)
    (pipeline_dir / "pipeline.yaml").write_text(
        yaml.dump(
            {
                "name": "t",
                "context_dependencies": {"x": []},
                "steps": [{"id": "s1", "conclusion_field": "x", "forward": None, "prompt": "prompts/s1.md"}],
            }
        ),
        encoding="utf-8",
    )
    prompts_dir = pipeline_dir / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    (prompts_dir / "s1.md").write_text("step 1", encoding="utf-8")
    from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

    return PipelineRunner(
        pipeline_dir=pipeline_dir,
        provider_manager=MagicMock(),
        base_tool_registry=MagicMock(),
        session_storage=_stub_session_storage(tmp_path),
        session_id="sess123",
        cwd="/proj",
        resume_from_sidecar=resume_from_sidecar,
    )


def _build_two_step_runner(tmp_path: Path, *, resume_from_sidecar: bool = False, max_rollbacks: int = 3):
    pipeline_dir = tmp_path / "pipe"
    pipeline_dir.mkdir(exist_ok=True)
    (pipeline_dir / "pipeline.yaml").write_text(
        yaml.dump(
            {
                "name": "t",
                "context_dependencies": {"x": [], "y": ["x"]},
                "max_rollbacks": max_rollbacks,
                "steps": [
                    {"id": "s1", "conclusion_field": "x", "forward": "s2", "prompt": "prompts/s1.md"},
                    {
                        "id": "s2",
                        "conclusion_field": "y",
                        "forward": None,
                        "prompt": "prompts/s2.md",
                        "rollback": [{"target": "s1", "condition": "revise"}],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    prompts_dir = pipeline_dir / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    (prompts_dir / "s1.md").write_text("step 1", encoding="utf-8")
    (prompts_dir / "s2.md").write_text("step 2", encoding="utf-8")
    from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

    return PipelineRunner(
        pipeline_dir=pipeline_dir,
        provider_manager=MagicMock(),
        base_tool_registry=MagicMock(),
        session_storage=_stub_session_storage(tmp_path),
        session_id="sess123",
        cwd="/proj",
        resume_from_sidecar=resume_from_sidecar,
    )


def _build_attempt_sanitizer_runner(tmp_path: Path, *, resume_from_sidecar: bool = False):
    pipeline_dir = tmp_path / "pipe"
    pipeline_dir.mkdir(exist_ok=True)
    step_ids = [f"s{i}" for i in range(1, 6)]
    (pipeline_dir / "pipeline.yaml").write_text(
        yaml.dump(
            {
                "name": "t",
                "context_dependencies": {f"x{i}": [] for i in range(1, 6)},
                "steps": [
                    {
                        "id": step_id,
                        "conclusion_field": f"x{i}",
                        "forward": step_ids[i] if i < len(step_ids) else None,
                        "prompt": f"prompts/{step_id}.md",
                    }
                    for i, step_id in enumerate(step_ids, start=1)
                ],
            }
        ),
        encoding="utf-8",
    )
    prompts_dir = pipeline_dir / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    for step_id in step_ids:
        (prompts_dir / f"{step_id}.md").write_text(step_id, encoding="utf-8")

    from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

    return PipelineRunner(
        pipeline_dir=pipeline_dir,
        provider_manager=MagicMock(),
        base_tool_registry=MagicMock(),
        session_storage=_stub_session_storage(tmp_path),
        session_id="sess123",
        cwd="/proj",
        resume_from_sidecar=resume_from_sidecar,
    )


def test_sidecar_under_session_directory(tmp_path):
    """问题 4：sidecar 应该在 <session_id>/pipeline/ 里，不再 <session_id>.pipeline/ 平级。"""
    runner = _build_runner(tmp_path)
    assert runner.session is not None
    expected = tmp_path / "projects" / "proj" / "sess123" / "pipeline"
    assert runner.session.session_dir == expected


def test_legacy_sidecar_path_not_used(tmp_path):
    """问题 4：旧的 <session_id>.pipeline/ 路径绝对不应被新代码使用。"""
    runner = _build_runner(tmp_path)
    assert runner.session is not None
    sidecar_str = str(runner.session.session_dir)
    assert ".pipeline" not in sidecar_str  # 旧后缀不能出现


@pytest.mark.asyncio
async def test_resume_from_sidecar_kwarg_restores_state(tmp_path):
    """问题 4：PipelineRunner(resume_from_sidecar=True) 应在 __init__ 后自动 restore。"""
    # First build a runner without resume to learn its sidecar path; pre-seed
    # that location with persisted state, then build a second runner with
    # resume_from_sidecar=True and assert it picked up the persisted step.
    runner = _build_runner(tmp_path)
    sidecar_dir = runner.session.session_dir
    pipeline_dir = tmp_path / "pipe"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    (sidecar_dir / "meta.yaml").write_text(
        yaml.dump(
            {
                "pipeline_name": "t",
                "status": "running",
                "current_step": "s1",
                "step_ids": ["s1"],
                "sub_pipeline_step_ids": {},
                "pipeline_fingerprint": hashlib.sha256((pipeline_dir / "pipeline.yaml").read_bytes()).hexdigest(),
                "state_machine": {
                    "current_index": 0,
                    "rollback_count": 0,
                    "interrupt_rollback_count": 0,
                    "step_statuses": {"s1": "running"},
                },
                "updated_at": 0.0,
                "reason": "seeded test sidecar",
            }
        ),
        encoding="utf-8",
    )
    (sidecar_dir / "context.yaml").write_text(
        yaml.dump({}),
        encoding="utf-8",
    )

    runner2 = _build_runner(tmp_path, resume_from_sidecar=True)
    assert runner2.state_machine.current_step.step_id == "s1"


def test_resume_from_sidecar_kwarg_exposes_failed_restore_result(tmp_path):
    runner = _build_two_step_runner(tmp_path)
    sidecar_dir = runner.session.session_dir
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    (sidecar_dir / "meta.yaml").write_text(
        yaml.dump(
            {
                "pipeline_name": "t",
                "status": "running",
                "current_step": "s2",
                "step_ids": ["different"],
                "sub_pipeline_step_ids": {},
                "pipeline_fingerprint": "old",
                "state_machine": {
                    "current_index": 1,
                    "rollback_count": 0,
                    "interrupt_rollback_count": 0,
                    "step_statuses": {"s1": "completed", "s2": "running"},
                },
                "updated_at": 0.0,
                "reason": "seeded mismatch",
            }
        ),
        encoding="utf-8",
    )
    (sidecar_dir / "context.yaml").write_text(yaml.dump({}), encoding="utf-8")

    runner2 = _build_two_step_runner(tmp_path, resume_from_sidecar=True)

    assert runner2.state_machine.current_step.step_id == "s1"
    assert runner2.sidecar_restore_result is not None
    assert runner2.sidecar_restore_result.ok is False
    assert runner2.sidecar_restore_result.reason == "pipeline_identity_mismatch"


@pytest.mark.parametrize(
    ("state_machine", "context", "reason"),
    [
        ({}, {}, "invalid_meta"),
        (
            {
                "current_index": 0,
                "rollback_count": 0,
                "interrupt_rollback_count": 0,
                "step_statuses": {"s1": "running"},
            },
            {"x": {}},
            "invalid_context",
        ),
    ],
)
def test_resume_from_sidecar_kwarg_gracefully_rejects_malformed_snapshot(tmp_path, state_machine, context, reason):
    runner = _build_runner(tmp_path)
    sidecar_dir = runner.session.session_dir
    pipeline_dir = tmp_path / "pipe"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    (sidecar_dir / "meta.yaml").write_text(
        yaml.dump(
            {
                "pipeline_name": "t",
                "status": "running",
                "current_step": "s1",
                "step_ids": ["s1"],
                "sub_pipeline_step_ids": {},
                "pipeline_fingerprint": hashlib.sha256((pipeline_dir / "pipeline.yaml").read_bytes()).hexdigest(),
                "state_machine": state_machine,
                "updated_at": 0.0,
                "reason": "seeded malformed snapshot",
            }
        ),
        encoding="utf-8",
    )
    (sidecar_dir / "context.yaml").write_text(yaml.dump(context), encoding="utf-8")

    runner2 = _build_runner(tmp_path, resume_from_sidecar=True)

    assert runner2.state_machine.current_step.step_id == "s1"
    assert runner2.sidecar_restore_result is not None
    assert runner2.sidecar_restore_result.ok is False
    assert runner2.sidecar_restore_result.reason == reason


def test_resume_from_sidecar_preserves_configured_max_rollbacks(tmp_path):
    runner = _build_two_step_runner(tmp_path, max_rollbacks=1)
    sidecar_dir = runner.session.session_dir
    pipeline_dir = tmp_path / "pipe"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    (sidecar_dir / "meta.yaml").write_text(
        yaml.dump(
            {
                "pipeline_name": "t",
                "status": "running",
                "current_step": "s2",
                "step_ids": ["s1", "s2"],
                "sub_pipeline_step_ids": {},
                "pipeline_fingerprint": hashlib.sha256((pipeline_dir / "pipeline.yaml").read_bytes()).hexdigest(),
                "state_machine": {
                    "current_index": 1,
                    "rollback_count": 1,
                    "interrupt_rollback_count": 0,
                    "step_statuses": {"s1": "completed", "s2": "running"},
                },
                "updated_at": 0.0,
                "reason": "seeded rollback limit",
            }
        ),
        encoding="utf-8",
    )
    (sidecar_dir / "context.yaml").write_text(
        yaml.dump(
            {
                "x": {"value": {"value": "x"}, "version": 1, "stale": False, "updated_at": 0.0, "history": []},
                "y": {"value": None, "version": 0, "stale": False, "updated_at": None, "history": []},
            }
        ),
        encoding="utf-8",
    )

    runner2 = _build_two_step_runner(tmp_path, resume_from_sidecar=True, max_rollbacks=1)

    assert runner2.sidecar_restore_result is not None
    assert runner2.sidecar_restore_result.ok is True
    with pytest.raises(ValueError, match="Max rollbacks"):
        runner2.state_machine.rollback("s1", "again")


def test_resume_from_sidecar_sanitizes_step_attempts(tmp_path):
    runner = _build_attempt_sanitizer_runner(tmp_path)
    sidecar_dir = runner.session.session_dir
    pipeline_dir = tmp_path / "pipe"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    (sidecar_dir / "meta.yaml").write_text(
        yaml.dump(
            {
                "pipeline_name": "t",
                "status": "running",
                "current_step": "s1",
                "step_ids": ["s1", "s2", "s3", "s4", "s5"],
                "sub_pipeline_step_ids": {},
                "pipeline_fingerprint": hashlib.sha256((pipeline_dir / "pipeline.yaml").read_bytes()).hexdigest(),
                "state_machine": {
                    "current_index": 0,
                    "rollback_count": 0,
                    "interrupt_rollback_count": 0,
                    "step_statuses": {
                        "s1": "running",
                        "s2": "pending",
                        "s3": "pending",
                        "s4": "pending",
                        "s5": "pending",
                    },
                    "step_attempts": {
                        "s1": True,
                        "s2": "2",
                        "s3": 0,
                        "s4": -1,
                        "s5": 3,
                        "unknown": 4,
                        99: 5,
                    },
                },
                "updated_at": 0.0,
                "reason": "seeded malformed step attempts",
            }
        ),
        encoding="utf-8",
    )
    (sidecar_dir / "context.yaml").write_text(yaml.dump({}), encoding="utf-8")

    runner2 = _build_attempt_sanitizer_runner(tmp_path, resume_from_sidecar=True)

    assert runner2.sidecar_restore_result is not None
    assert runner2.sidecar_restore_result.ok is True
    assert runner2._step_attempts == {"s5": 3}


def test_resume_from_sidecar_without_step_attempts_uses_empty_attempts(tmp_path):
    runner = _build_runner(tmp_path)
    sidecar_dir = runner.session.session_dir
    pipeline_dir = tmp_path / "pipe"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    (sidecar_dir / "meta.yaml").write_text(
        yaml.dump(
            {
                "pipeline_name": "t",
                "status": "running",
                "current_step": "s1",
                "step_ids": ["s1"],
                "sub_pipeline_step_ids": {},
                "pipeline_fingerprint": hashlib.sha256((pipeline_dir / "pipeline.yaml").read_bytes()).hexdigest(),
                "state_machine": {
                    "current_index": 0,
                    "rollback_count": 0,
                    "interrupt_rollback_count": 0,
                    "step_statuses": {"s1": "running"},
                },
                "updated_at": 0.0,
                "reason": "seeded old sidecar",
            }
        ),
        encoding="utf-8",
    )
    (sidecar_dir / "context.yaml").write_text(yaml.dump({}), encoding="utf-8")

    runner2 = _build_runner(tmp_path, resume_from_sidecar=True)

    assert runner2.sidecar_restore_result is not None
    assert runner2.sidecar_restore_result.ok is True
    assert runner2._step_attempts == {}


@pytest.mark.asyncio
async def test_resume_from_sidecar_preserves_step_attempts_after_parent_rollback(tmp_path):
    runner = _build_two_step_runner(tmp_path)
    seen: dict[str, int] = {"s1": 0, "s2": 0}

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        seen[step.step_id] += 1
        conclusion = {"value": f"{step.step_id}-{seen[step.step_id]}"}
        context.set_conclusion(step.conclusion_field, conclusion)
        rollback_request = ("s1", "revise") if step.step_id == "s2" and seen["s2"] == 1 else None
        yield StepResult(
            step_id=step.step_id,
            status=StepStatus.COMPLETED,
            conclusion=conclusion,
            rollback_request=rollback_request,
        )

    runner._step_executor.execute = fake_execute

    stream = runner.run("hello")
    try:
        async for event in stream:
            if isinstance(event, PipelineEvent) and event.type == PipelineEventType.ROLLBACK_TRIGGERED:
                break
    finally:
        await stream.aclose()

    runner2 = _build_two_step_runner(tmp_path, resume_from_sidecar=True)
    runner2._observability.step_started = MagicMock()

    async def fake_resume_execute(step, context, session_id, user_message=None, **kwargs):
        conclusion = {"value": step.step_id}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

    runner2._step_executor.execute = fake_resume_execute

    async for _event in runner2.continue_from_sidecar():
        pass

    assert [
        (call.kwargs["step_id"], call.kwargs["step_attempt"])
        for call in runner2._observability.step_started.call_args_list
    ] == [("s1", 2), ("s2", 2)]


@pytest.mark.asyncio
async def test_continue_from_sidecar_reuses_persisted_current_step_user_input(tmp_path):
    runner = _build_two_step_runner(tmp_path)
    runner._set_current_step_user_input("选择一个已有vpc，创建一个vswitch")
    await runner._save_running("s1", reason="step started")

    runner2 = _build_two_step_runner(tmp_path, resume_from_sidecar=True)
    seen_user_messages: list[str | None] = []

    async def fake_execute(step, context, session_id, user_message=None, **_kwargs):
        seen_user_messages.append(user_message)
        conclusion = {"value": step.step_id}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

    runner2._step_executor.execute = fake_execute

    async for _event in runner2.continue_from_sidecar():
        if seen_user_messages:
            break

    assert seen_user_messages == ["选择一个已有vpc，创建一个vswitch"]


@pytest.mark.asyncio
async def test_continue_from_sidecar_reuses_persisted_current_step_image_input(tmp_path):
    from iac_code.pipeline.engine.user_input import PipelineUserInput

    image_input = PipelineUserInput(
        content=[
            TextBlock(text="参考这张图"),
            ImageBlock(media_type="image/png", data="aW1hZ2U="),
        ],
        display_text="参考这张图",
        has_images=True,
    )
    runner = _build_two_step_runner(tmp_path)
    runner._set_current_step_user_input(image_input)
    await runner._save_running("s1", reason="step started")

    runner2 = _build_two_step_runner(tmp_path, resume_from_sidecar=True)
    seen_user_messages = []

    async def fake_execute(step, context, session_id, user_message=None, **_kwargs):
        seen_user_messages.append(user_message)
        conclusion = {"value": step.step_id}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

    runner2._step_executor.execute = fake_execute

    async for _event in runner2.continue_from_sidecar():
        if seen_user_messages:
            break

    assert seen_user_messages == [image_input.content]


@pytest.mark.asyncio
async def test_hard_interrupt_rollback_context_survives_sidecar_restore(tmp_path):
    from iac_code.pipeline.engine.interrupt import InterruptVerdict

    runner = _build_two_step_runner(tmp_path)
    runner.state_machine.advance()

    applied = runner.apply_hard_interrupt(
        InterruptVerdict(
            action="hard_interrupt",
            reason="用户切换需求",
            rollback_target="s1",
            rollback_context="用户反馈：改为选择一个已有vpc，创建一个安全组",
        )
    )

    assert applied is True

    runner2 = _build_two_step_runner(tmp_path, resume_from_sidecar=True)
    seen_user_messages: list[str | None] = []

    async def fake_execute(step, context, session_id, user_message=None, **_kwargs):
        seen_user_messages.append(user_message)
        conclusion = {"value": step.step_id}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

    runner2._step_executor.execute = fake_execute

    async for _event in runner2.continue_from_sidecar():
        if seen_user_messages:
            break

    assert seen_user_messages == ["用户反馈：改为选择一个已有vpc，创建一个安全组"]


@pytest.mark.asyncio
async def test_resume_from_waiting_input_sidecar_preserves_step_attempt_for_user_input_received(tmp_path):
    runner = _build_two_step_runner(tmp_path)
    runner._step_attempts = {"s1": 2, "s2": 1}

    await runner._save_waiting_input("s1")

    runner2 = _build_two_step_runner(tmp_path, resume_from_sidecar=True)
    runner2._observability.user_input_received = MagicMock()
    runner2._observability.selection_made = MagicMock()

    async def fake_continue(user_input=None, **kwargs):
        assert kwargs == {"resume_waiting_step": True}
        if False:
            yield

    runner2._continue_from_current = fake_continue

    async for _event in runner2.resume("choice"):
        pass

    runner2._observability.user_input_received.assert_called_once_with(
        step_id="s1",
        step_index=1,
        step_attempt=2,
        total_steps=2,
        ui_mode=None,
        user_input="choice",
        wait_duration_ms=None,
    )


@pytest.mark.asyncio
async def test_resume_candidate_selection_emits_selected_option_details(tmp_path):
    runner = _build_runner(tmp_path)
    runner.state_machine.current_step.ui_mode = "candidate_selection"
    runner._waiting_input_options_by_step["s1"] = [
        {"name": "方案A", "candidate_index": 0},
        {"name": "方案B", "candidate_index": 1},
    ]

    async def fake_continue(user_input=None, **kwargs):
        assert kwargs == {"resume_waiting_step": True}
        if False:
            yield

    runner._continue_from_current = fake_continue

    events = [event async for event in runner.resume("方案B")]

    received = next(event for event in events if isinstance(event, PipelineEvent))
    assert received.type == PipelineEventType.USER_INPUT_RECEIVED
    assert received.data == {
        "user_input_length": 3,
        "kind": "candidate_selection",
        "selected_index": 1,
        "selected_value": "方案B",
        "selected_option": {"name": "方案B", "candidate_index": 1},
    }


@pytest.mark.asyncio
async def test_resume_candidate_selection_extracts_index_from_structured_json(tmp_path):
    runner = _build_runner(tmp_path)
    runner.state_machine.current_step.ui_mode = "candidate_selection"
    runner._waiting_input_options_by_step["s1"] = [
        {"name": "方案A", "candidate_index": 0},
        {"name": "方案B", "candidate_index": 1},
    ]

    async def fake_continue(user_input=None, **kwargs):
        assert kwargs == {"resume_waiting_step": True}
        if False:
            yield

    runner._continue_from_current = fake_continue
    user_input = json.dumps(
        {
            "selected_candidate_index": 1,
            "parameter_overrides": {"InstanceType": "ecs.g7.large"},
        },
        ensure_ascii=False,
    )

    events = [event async for event in runner.resume(user_input)]

    received = next(event for event in events if isinstance(event, PipelineEvent))
    assert received.type == PipelineEventType.USER_INPUT_RECEIVED
    assert received.data["selected_index"] == 1
    assert received.data["selected_option"] == {"name": "方案B", "candidate_index": 1}


@pytest.mark.asyncio
async def test_resume_candidate_selection_uses_restored_context_options(tmp_path):
    runner = _build_runner(tmp_path)
    runner.state_machine.current_step.ui_mode = "candidate_selection"
    runner.context.set_conclusion(
        "x",
        {
            "options": [
                {"name": "方案A", "candidate_index": 0},
                {"name": "方案B", "candidate_index": 1},
            ]
        },
    )

    async def fake_continue(user_input=None, **kwargs):
        assert kwargs == {"resume_waiting_step": True}
        if False:
            yield

    runner._continue_from_current = fake_continue

    events = [event async for event in runner.resume("方案B")]

    received = next(event for event in events if isinstance(event, PipelineEvent))
    assert received.type == PipelineEventType.USER_INPUT_RECEIVED
    assert received.data == {
        "user_input_length": 3,
        "kind": "candidate_selection",
        "selected_index": 1,
        "selected_value": "方案B",
        "selected_option": {"name": "方案B", "candidate_index": 1},
    }


@pytest.mark.asyncio
async def test_resume_ask_user_question_injects_tool_result_and_guard_state(tmp_path):
    runner = _build_runner(tmp_path)
    runner._step_attempts = {"s1": 1}
    runner._observability.step_started = MagicMock()
    history = [
        Message(role="user", content="我有个项目想上线"),
        Message(
            role="assistant",
            content=[
                {
                    "type": "tool_use",
                    "id": "ask-1",
                    "name": "ask_user_question",
                    "input": {"question": "请选择部署目标"},
                }
            ],
        ),
    ]
    runner._session_storage.load.return_value = history
    captured = {}

    async def fake_execute(
        step,
        context,
        session_id,
        user_message=None,
        resume_messages=None,
        precompleted_tools=None,
        **kwargs,
    ):
        captured["user_message"] = user_message
        captured["resume_messages"] = resume_messages
        captured["precompleted_tools"] = precompleted_tools
        conclusion = {"ok": True}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

    runner._step_executor.execute = fake_execute

    events = [
        event
        async for event in runner.resume_ask_user_question(
            {"selected_id": "nginx", "selected_label": "Nginx 网站", "free_text": ""},
            tool_use_id="ask-1",
        )
    ]

    user_message = captured["user_message"]
    assert isinstance(user_message, list)
    assert len(user_message) == 1
    assert isinstance(user_message[0], ToolResultBlock)
    assert user_message[0].tool_use_id == "ask-1"
    assert json.loads(user_message[0].content) == {
        "selected_id": "nginx",
        "selected_label": "Nginx 网站",
        "free_text": "",
    }
    assert captured["resume_messages"] == history
    assert captured["precompleted_tools"] == {
        "ask_user_question": {"selected_id": "nginx", "selected_label": "Nginx 网站", "free_text": ""}
    }
    assert not any(
        isinstance(event, PipelineEvent) and event.type == PipelineEventType.STEP_STARTED for event in events
    )
    runner._observability.step_started.assert_not_called()
    assert runner._step_attempts == {"s1": 1}


@pytest.mark.asyncio
async def test_resume_ask_user_question_rejects_pending_input_tool_use_id_mismatch(tmp_path):
    runner = _build_runner(tmp_path)
    runner._session_storage.load.return_value = []
    runner._step_executor.execute = MagicMock()

    with pytest.raises(ValueError, match="tool_use_id"):
        events = runner.resume_ask_user_question(
            {"selected_id": "nginx", "selected_label": "Nginx 网站", "free_text": ""},
            tool_use_id="ask-stale",
            pending_input={"toolUseId": "ask-current", "kind": "ask_user_question"},
        )
        async for _event in events:
            pass

    runner._step_executor.execute.assert_not_called()


@pytest.mark.asyncio
async def test_resume_ask_user_question_rejects_transcript_tool_use_id_mismatch(tmp_path):
    runner = _build_runner(tmp_path)
    runner._session_storage.load.return_value = [
        Message(
            role="assistant",
            content=[
                {
                    "type": "tool_use",
                    "id": "ask-current",
                    "name": "ask_user_question",
                    "input": {"question": "请选择部署目标"},
                }
            ],
        )
    ]
    runner._step_executor.execute = MagicMock()

    with pytest.raises(ValueError, match="tool_use_id"):
        events = runner.resume_ask_user_question(
            {"selected_id": "nginx", "selected_label": "Nginx 网站", "free_text": ""},
            tool_use_id="ask-stale",
        )
        async for _event in events:
            pass

    runner._step_executor.execute.assert_not_called()


def test_resume_from_sidecar_accepts_list_valued_context_fields(tmp_path):
    runner = _build_two_step_runner(tmp_path)
    sidecar_dir = runner.session.session_dir
    pipeline_dir = tmp_path / "pipe"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    evaluated_candidates = [
        {
            "candidate": {"name": "轻量应用服务器方案"},
            "template": {"path": "templates/1.yml"},
            "failed": False,
        }
    ]
    (sidecar_dir / "meta.yaml").write_text(
        yaml.dump(
            {
                "pipeline_name": "t",
                "status": "waiting_input",
                "current_step": "s2",
                "step_ids": ["s1", "s2"],
                "sub_pipeline_step_ids": {},
                "pipeline_fingerprint": hashlib.sha256((pipeline_dir / "pipeline.yaml").read_bytes()).hexdigest(),
                "state_machine": {
                    "current_index": 1,
                    "rollback_count": 0,
                    "interrupt_rollback_count": 0,
                    "step_statuses": {"s1": "completed", "s2": "running"},
                },
                "updated_at": 0.0,
                "reason": "waiting for user input",
            }
        ),
        encoding="utf-8",
    )
    (sidecar_dir / "context.yaml").write_text(
        yaml.dump(
            {
                "x": {
                    "value": evaluated_candidates,
                    "version": 1,
                    "stale": False,
                    "updated_at": 0.0,
                    "history": [],
                },
                "y": {"value": None, "version": 0, "stale": False, "updated_at": None, "history": []},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    runner2 = _build_two_step_runner(tmp_path, resume_from_sidecar=True)

    assert runner2.sidecar_restore_result is not None
    assert runner2.sidecar_restore_result.ok is True
    assert runner2.state_machine.current_step.step_id == "s2"
    assert runner2.context.get_conclusion("x") == evaluated_candidates
