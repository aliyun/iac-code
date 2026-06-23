import asyncio
import json
import logging
from pathlib import Path
from textwrap import dedent
from unittest.mock import ANY, MagicMock, call, patch

import pytest
import yaml

from iac_code.agent.message import Message, ToolResultBlock
from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
from iac_code.pipeline.engine.session import PipelineSession
from iac_code.pipeline.engine.state_machine import StateMachine
from iac_code.pipeline.engine.transcript_storage import PipelineTranscriptStorage
from iac_code.pipeline.engine.types import StepResult, StepStatus
from iac_code.services.session_storage import SessionStorage


def _selling_dir() -> Path:
    return Path(__file__).parent.parent.parent.parent / "src" / "iac_code" / "pipeline" / "selling"


class FakeSessionStorage:
    def __init__(self):
        self.meta_entries = []
        self._path = MagicMock()

    def append_meta(self, cwd, session_id, meta):
        self.meta_entries.append(meta)

    def session_path(self, cwd, session_id):
        return self._path


class DirectorySessionStorage(FakeSessionStorage):
    def __init__(self, root: Path):
        super().__init__()
        self._storage = SessionStorage(root)

    def session_path(self, cwd, session_id):
        return self._storage.session_path(cwd, session_id)

    def session_dir(self, cwd, session_id):
        return self._storage.session_dir(cwd, session_id)


class RecordingPipelineSession:
    def __init__(self):
        self.calls = []
        self.restore_result = None

    async def save_running(
        self, current_step, state_machine_snapshot, context_snapshot, pipeline_identity, reason=None, **kwargs
    ):
        self.calls.append(("running", current_step, state_machine_snapshot["current_index"], reason))

    async def save_waiting_input(
        self, current_step, state_machine_snapshot, context_snapshot, pipeline_identity, reason=None, **kwargs
    ):
        self.calls.append(("waiting_input", current_step, state_machine_snapshot["current_index"], reason))

    async def save_completed(
        self, current_step, state_machine_snapshot, context_snapshot, pipeline_identity, reason=None, **kwargs
    ):
        self.calls.append(("completed", current_step, state_machine_snapshot["current_index"], reason))

    async def save_failed(
        self, current_step, state_machine_snapshot, context_snapshot, pipeline_identity, reason=None, **kwargs
    ):
        self.calls.append(("failed", current_step, state_machine_snapshot["current_index"], reason))

    async def save_rollback(
        self, from_step, to_step, reason, state_machine_snapshot, context_snapshot, pipeline_identity, **kwargs
    ):
        self.calls.append(("rollback", from_step, to_step, state_machine_snapshot["current_index"], reason))

    def save_rollback_sync(
        self, from_step, to_step, reason, state_machine_snapshot, context_snapshot, pipeline_identity, **kwargs
    ):
        self.calls.append(("rollback_sync", from_step, to_step, state_machine_snapshot["current_index"], reason))

    def restore_sync(self, pipeline_identity):
        return self.restore_result

    async def restore(self, pipeline_identity):
        return self.restore_result

    def exists(self):
        return self.restore_result is not None


class FailingSavePipelineSession(RecordingPipelineSession):
    async def save_running(
        self, current_step, state_machine_snapshot, context_snapshot, pipeline_identity, reason=None, **kwargs
    ):
        self.calls.append(("running_attempted", current_step, state_machine_snapshot["current_index"], reason))
        raise OSError("sidecar unavailable")

    async def save_failed(
        self, current_step, state_machine_snapshot, context_snapshot, pipeline_identity, reason=None, **kwargs
    ):
        self.calls.append(("failed_attempted", current_step, state_machine_snapshot["current_index"], reason))
        raise OSError("sidecar unavailable")

    async def save_waiting_input(
        self, current_step, state_machine_snapshot, context_snapshot, pipeline_identity, reason=None, **kwargs
    ):
        self.calls.append(("waiting_input_attempted", current_step, state_machine_snapshot["current_index"], reason))
        raise OSError("sidecar unavailable")

    def save_rollback_sync(
        self, from_step, to_step, reason, state_machine_snapshot, context_snapshot, pipeline_identity, **kwargs
    ):
        self.calls.append(
            ("rollback_sync_attempted", from_step, to_step, state_machine_snapshot["current_index"], reason)
        )
        raise OSError("sidecar unavailable")


class FailingAfterAdvancePipelineSession(RecordingPipelineSession):
    async def save_running(
        self, current_step, state_machine_snapshot, context_snapshot, pipeline_identity, reason=None, **kwargs
    ):
        self.calls.append(("running_attempted", current_step, state_machine_snapshot["current_index"], reason))
        if current_step == "b" and reason == "advanced from a":
            raise OSError("sidecar unavailable")


class FailingFinalCompletedPipelineSession(RecordingPipelineSession):
    async def save_completed(
        self, current_step, state_machine_snapshot, context_snapshot, pipeline_identity, reason=None, **kwargs
    ):
        self.calls.append(("completed_attempted", current_step, state_machine_snapshot["current_index"], reason))
        raise OSError("sidecar unavailable")


class FailingCandidateFailedPipelineSession(RecordingPipelineSession):
    async def save_running(
        self, current_step, state_machine_snapshot, context_snapshot, pipeline_identity, reason=None, **kwargs
    ):
        self.calls.append(("running_attempted", current_step, state_machine_snapshot["current_index"], reason))
        if reason == "parallel candidate failed":
            raise OSError("sidecar unavailable")


class CapturingPipelineSession(RecordingPipelineSession):
    def __init__(self):
        super().__init__()
        self.saved = []

    async def save_running(
        self, current_step, state_machine_snapshot, context_snapshot, pipeline_identity, reason=None, **kwargs
    ):
        self.saved.append(("running", current_step, reason, kwargs))
        await super().save_running(
            current_step, state_machine_snapshot, context_snapshot, pipeline_identity, reason=reason, **kwargs
        )

    async def save_failed(
        self, current_step, state_machine_snapshot, context_snapshot, pipeline_identity, reason=None, **kwargs
    ):
        self.saved.append(("failed", current_step, reason, kwargs))
        await super().save_failed(
            current_step, state_machine_snapshot, context_snapshot, pipeline_identity, reason=reason, **kwargs
        )


def _build_two_step_runner(tmp_path, *, auto_advance_first=True):
    (tmp_path / "prompts").mkdir(exist_ok=True)
    (tmp_path / "prompts" / "a.md").write_text("A", encoding="utf-8")
    (tmp_path / "prompts" / "b.md").write_text("B", encoding="utf-8")
    auto_advance = "true" if auto_advance_first else "false"
    (tmp_path / "pipeline.yaml").write_text(
        dedent(
            f"""\
            name: test
            context_dependencies:
              a_out: []
              b_out: [a_out]
            max_rollbacks: 3
            steps:
              - id: a
                conclusion_field: a_out
                forward: b
                prompt: prompts/a.md
                auto_advance: {auto_advance}
              - id: b
                conclusion_field: b_out
                forward: null
                prompt: prompts/b.md
            """
        ),
        encoding="utf-8",
    )
    return PipelineRunner(
        pipeline_dir=tmp_path,
        provider_manager=MagicMock(),
        base_tool_registry=MagicMock(),
        session_storage=FakeSessionStorage(),
        session_id="test",
        cwd=str(tmp_path),
    )


def _build_parallel_runner(tmp_path, *, storage=None):
    (tmp_path / "prompts").mkdir(exist_ok=True)
    (tmp_path / "prompts" / "arch.md").write_text("Arch", encoding="utf-8")
    (tmp_path / "prompts" / "eval.md").write_text("Eval", encoding="utf-8")
    (tmp_path / "prompts" / "template.md").write_text("T", encoding="utf-8")
    (tmp_path / "prompts" / "cost.md").write_text("C", encoding="utf-8")
    (tmp_path / "pipeline.yaml").write_text(
        dedent("""\
        name: test
        context_dependencies:
          architecture: []
          evaluated: [architecture]
        max_rollbacks: 3
        sub_pipelines:
          evaluate_candidate:
            max_rollbacks: 2
            iterate_over: architecture.candidates
            context_fields_from_parent: []
            steps:
              - id: template_gen
                conclusion_field: template
                forward: cost
                prompt: prompts/template.md
                context_fields: [candidate]
              - id: cost
                conclusion_field: cost
                forward: null
                prompt: prompts/cost.md
                context_fields: [template]
        steps:
          - id: arch
            conclusion_field: architecture
            forward: eval
            prompt: prompts/arch.md
          - id: eval
            type: parallel_sub_pipeline
            sub_pipeline: evaluate_candidate
            conclusion_field: evaluated
            forward: null
            prompt: prompts/eval.md
        """),
        encoding="utf-8",
    )
    runner = PipelineRunner(
        pipeline_dir=tmp_path,
        provider_manager=MagicMock(),
        base_tool_registry=MagicMock(),
        session_storage=storage or FakeSessionStorage(),
        session_id="test123",
        cwd=str(tmp_path),
    )
    runner.context.set_conclusion(
        "architecture",
        {
            "candidates": [
                {"name": "Plan A"},
                {"name": "Plan B"},
            ]
        },
    )
    runner.state_machine.advance()
    return runner


def test_parent_attempt_created_on_step_start(tmp_path):
    runner = _build_two_step_runner(tmp_path)

    attempt = runner._ensure_parent_attempt("a")

    assert attempt["scope"] == "parent"
    assert attempt["step_id"] == "a"
    assert attempt["status"] == "running"
    assert attempt["transcript_id"] == "transcript_att_0001"
    assert runner._execution["active_attempt_id"] == "att_0001"


def test_rollback_to_same_step_creates_new_attempt(tmp_path):
    runner = _build_two_step_runner(tmp_path)

    first = runner._ensure_parent_attempt("a")
    runner._mark_attempt_status(first["attempt_id"], "completed")
    second = runner._create_parent_attempt("a")

    assert first["attempt_id"] == "att_0001"
    assert second["attempt_id"] == "att_0002"
    assert second["transcript_id"] == "transcript_att_0002"


@pytest.mark.asyncio
async def test_runner_passes_attempt_transcript_to_step_executor(tmp_path, monkeypatch):
    runner = _build_two_step_runner(tmp_path)
    captured = {}

    async def fake_execute(
        step,
        context,
        session_id,
        user_message=None,
        *,
        attempt_id=None,
        transcript_id=None,
        resume_messages=None,
    ):
        if not captured:
            captured["session_id"] = session_id
            captured["attempt_id"] = attempt_id
            captured["transcript_id"] = transcript_id
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion={"ok": True})

    monkeypatch.setattr(runner._step_executor, "execute", fake_execute)

    async for _event in runner.run("start"):
        pass

    assert captured["session_id"] == runner._session_id
    assert captured["attempt_id"] == "att_0001"
    assert captured["transcript_id"] == "transcript_att_0001"


@pytest.mark.asyncio
async def test_auto_advance_saves_next_step_after_advance(tmp_path):
    runner = _build_two_step_runner(tmp_path)
    runner.session = RecordingPipelineSession()

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        conclusion = {"value": step.step_id}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

    runner._step_executor.execute = fake_execute

    async for _event in runner._continue_from_current():
        if any(call[0] == "running" and call[1] == "b" for call in runner.session.calls):
            break

    assert ("running", "b", 1, "advanced from a") in runner.session.calls


@pytest.mark.asyncio
async def test_step_started_saves_running_before_yielding_event(tmp_path):
    runner = _build_two_step_runner(tmp_path)
    runner.session = RecordingPipelineSession()

    gen = runner._continue_from_current()
    event = await gen.__anext__()
    await gen.aclose()

    assert isinstance(event, PipelineEvent)
    assert event.type == PipelineEventType.STEP_STARTED
    assert ("running", "a", 0, "step started") in runner.session.calls


@pytest.mark.asyncio
async def test_user_input_required_saves_waiting_input(tmp_path):
    runner = _build_two_step_runner(tmp_path, auto_advance_first=False)
    runner.session = RecordingPipelineSession()

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        conclusion = {"user_prompt": "choose", "options": ["one"]}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

    runner._step_executor.execute = fake_execute

    async for _event in runner._continue_from_current():
        pass

    assert ("waiting_input", "a", 0, "waiting for user input") in runner.session.calls


@pytest.mark.asyncio
async def test_sidecar_save_failure_stops_before_next_step(tmp_path):
    runner = _build_two_step_runner(tmp_path)
    runner.session = FailingAfterAdvancePipelineSession()
    executed_steps = []

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        executed_steps.append(step.step_id)
        conclusion = {"value": step.step_id}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

    runner._step_executor.execute = fake_execute

    events = []
    async for event in runner._continue_from_current():
        events.append(event)

    assert executed_steps == ["a"]
    assert any(
        isinstance(event, PipelineEvent)
        and event.type == PipelineEventType.STEP_FAILED
        and "pipeline state persistence failed" in str(event.data).lower()
        for event in events
    )
    assert not any(
        isinstance(event, PipelineEvent) and event.type == PipelineEventType.STEP_COMPLETED and event.step_id == "a"
        for event in events
    )
    assert not any(
        isinstance(event, PipelineEvent) and event.type == PipelineEventType.STEP_STARTED and event.step_id == "b"
        for event in events
    )
    assert runner.state_machine.current_step.step_id == "b"
    assert ("running_attempted", "b", 1, "advanced from a") in runner.session.calls
    assert runner.sidecar_status is None


@pytest.mark.asyncio
async def test_final_completed_save_failure_yields_persistence_failure_event(tmp_path):
    runner = _build_two_step_runner(tmp_path)
    runner.session = FailingFinalCompletedPipelineSession()
    executed_steps = []

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        executed_steps.append(step.step_id)
        conclusion = {"value": step.step_id}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

    runner._step_executor.execute = fake_execute

    events = []
    async for event in runner._continue_from_current():
        events.append(event)

    assert executed_steps == ["a", "b"]
    assert any(
        isinstance(event, PipelineEvent)
        and event.type == PipelineEventType.STEP_FAILED
        and event.step_id == "b"
        and event.data["error_details"]["type"] == "PipelineStatePersistenceError"
        for event in events
    )
    assert not any(
        isinstance(event, PipelineEvent) and event.type == PipelineEventType.PIPELINE_COMPLETED for event in events
    )
    assert ("completed_attempted", "b", 2, "pipeline completed") in runner.session.calls
    assert runner.sidecar_status is None


@pytest.mark.asyncio
async def test_sidecar_save_failure_emits_sidecar_failed_telemetry(tmp_path):
    runner = _build_two_step_runner(tmp_path)
    runner.session = FailingSavePipelineSession()
    runner._observability.sidecar_failed = MagicMock()

    with pytest.raises(RuntimeError, match="pipeline state persistence failed during save_running"):
        await runner._save_running("a", reason="step started")

    runner._observability.sidecar_failed.assert_called_once_with(
        operation="save_running",
        status="running",
        error_type="OSError",
        error_summary="sidecar unavailable",
        error_id=ANY,
    )


@pytest.mark.asyncio
async def test_real_sidecar_save_failure_logs_once_at_runner_boundary(tmp_path, caplog, monkeypatch):
    from iac_code.pipeline.engine.session import PipelineSession

    runner = _build_two_step_runner(tmp_path)
    runner.session = PipelineSession(tmp_path / "pipeline")

    def fail_write(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(runner.session, "_atomic_write_yaml", fail_write)
    caplog.set_level(logging.WARNING)

    with pytest.raises(RuntimeError, match="pipeline state persistence failed during save_running"):
        await runner._save_running("a", reason="step started")

    sidecar_records = [record for record in caplog.records if "pipeline sidecar" in record.getMessage()]
    assert len(sidecar_records) == 1
    assert sidecar_records[0].name == "iac_code.pipeline.engine.pipeline_runner"
    assert "Failed to persist pipeline sidecar during save_running" in sidecar_records[0].getMessage()
    assert "status=running" in sidecar_records[0].getMessage()
    assert "Failed to save pipeline sidecar" not in caplog.text
    assert runner.sidecar_status is None


def test_restore_from_sidecar_rejects_mismatch_without_state_machine_restore(tmp_path):
    from iac_code.pipeline.engine.session import RestoreResult

    runner = _build_two_step_runner(tmp_path)
    runner.session = RecordingPipelineSession()
    runner.session.restore_result = RestoreResult(ok=False, status="running", reason="pipeline_identity_mismatch")
    runner._observability.sidecar_failed = MagicMock()

    result = runner.restore_from_sidecar_sync()

    assert result.ok is False
    assert result.reason == "pipeline_identity_mismatch"
    assert runner.state_machine.current_step.step_id == "a"
    runner._observability.sidecar_failed.assert_not_called()


def test_failed_sidecar_restore_result_is_observable(tmp_path):
    from iac_code.pipeline.engine.session import RestoreResult

    runner = _build_two_step_runner(tmp_path)
    runner.session = RecordingPipelineSession()
    runner.session.restore_result = RestoreResult(ok=False, status="running", reason="pipeline_identity_mismatch")

    result = runner.restore_from_sidecar_sync()

    assert runner.sidecar_restore_result is result
    assert runner.sidecar_restore_result.ok is False
    assert runner.sidecar_restore_result.reason == "pipeline_identity_mismatch"


@pytest.mark.asyncio
async def test_continue_from_sidecar_continues_without_user_prompt(tmp_path):
    runner = _build_two_step_runner(tmp_path)
    seen_user_messages = []

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        seen_user_messages.append(user_message)
        conclusion = {"value": step.step_id}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

    runner._step_executor.execute = fake_execute

    async for _event in runner.continue_from_sidecar():
        if seen_user_messages:
            break

    assert seen_user_messages == [None]


def test_sync_sidecar_save_failure_raises_after_hard_interrupt_boundary(tmp_path):
    from iac_code.pipeline.engine.interrupt import InterruptVerdict

    runner = _build_two_step_runner(tmp_path)
    runner.session = FailingSavePipelineSession()
    runner._observability.sidecar_failed = MagicMock()
    runner.state_machine.advance()

    with pytest.raises(RuntimeError, match="pipeline state persistence failed during save_rollback_sync"):
        runner.apply_hard_interrupt(
            InterruptVerdict(action="hard_interrupt", reason="changed mind", rollback_target="a")
        )

    assert runner.state_machine.current_step.step_id == "a"
    assert any(call[0] == "rollback_sync_attempted" for call in runner.session.calls)
    assert runner.sidecar_status is None
    runner._observability.sidecar_failed.assert_called_once_with(
        operation="save_rollback_sync",
        status="running",
        error_type="OSError",
        error_summary="sidecar unavailable",
        error_id=ANY,
    )


def test_parent_step_executor_uses_pipeline_transcript_storage_when_sidecar_exists(tmp_path):
    _build_two_step_runner(tmp_path)
    storage = DirectorySessionStorage(tmp_path / "projects")

    runner = PipelineRunner(
        pipeline_dir=tmp_path,
        provider_manager=MagicMock(),
        base_tool_registry=MagicMock(),
        session_storage=storage,
        session_id="test",
        cwd=str(tmp_path),
    )

    expected_path = (
        storage.session_dir(str(tmp_path), "test")
        / "pipeline"
        / "transcripts"
        / "transcript_att_0001"
        / "session.jsonl"
    )
    assert isinstance(runner._step_executor._session_storage, PipelineTranscriptStorage)
    assert runner._step_executor._session_storage.session_path(str(tmp_path), "transcript_att_0001") == expected_path


def test_prompt_context_for_restored_parent_step_comes_from_agent_loop_context(tmp_path):
    _build_two_step_runner(tmp_path)
    storage = DirectorySessionStorage(tmp_path / "projects")
    runner = PipelineRunner(
        pipeline_dir=tmp_path,
        provider_manager=MagicMock(),
        base_tool_registry=MagicMock(),
        session_storage=storage,
        session_id="test",
        cwd=str(tmp_path),
    )
    attempt = runner._ensure_parent_attempt("a")
    assert runner._transcript_storage is not None
    runner._transcript_storage.save(
        str(tmp_path),
        attempt["transcript_id"],
        [
            Message(role="user", content="original user prompt"),
            Message(role="assistant", content="partial answer"),
        ],
    )

    contexts = runner.get_prompt_contexts()

    assert len(contexts) == 1
    context = contexts[0]
    assert context.scope == "parent"
    assert context.step_id == "a"
    assert context.agent_loop_session_id == attempt["transcript_id"]
    assert "A" in context.system_prompt
    assert [message.get_text() for message in context.messages] == ["original user prompt", "partial answer"]


def test_prompt_context_for_restored_parallel_candidates_comes_from_each_agent_loop_context(tmp_path):
    storage = DirectorySessionStorage(tmp_path / "projects")
    runner = _build_parallel_runner(tmp_path, storage=storage)
    current_step = runner.state_machine.current_step
    sub_spec = runner._loaded.sub_pipelines[current_step.sub_pipeline_name]
    sub_state_machine = StateMachine(sub_spec.steps, sub_spec.max_rollbacks)
    sub_state_machine.advance()
    sub_context = PipelineContext(
        {
            "candidate": [],
            "template": ["candidate"],
            "cost": ["template"],
        }
    )
    sub_context.set_conclusion("candidate", {"name": "Plan A"})
    sub_context.set_conclusion("template", {"template_body": "Resources: {}"})
    attempt = runner._create_sub_step_attempt(
        parent_step_id="eval",
        candidate_index=0,
        sub_pipeline_id="evaluate_candidate_0",
        sub_step_id="cost",
    )
    runner._execution = {
        "kind": "parallel_sub_pipeline",
        "step_id": "eval",
        "sub_pipeline_name": "evaluate_candidate",
        "candidates": {
            "0": {
                "status": "running",
                "candidate": {"name": "Plan A"},
                "name": "Plan A",
                "sub_pipeline_id": "evaluate_candidate_0",
                "current_sub_step": "cost",
                "state_machine": sub_state_machine.to_snapshot(),
                "context": sub_context.to_snapshot(),
                "active_attempt_id": attempt["attempt_id"],
                "transcript_id": attempt["transcript_id"],
            }
        },
    }
    assert runner._transcript_storage is not None
    runner._transcript_storage.save(
        str(tmp_path),
        attempt["transcript_id"],
        [Message(role="user", content="resume cost context")],
    )

    contexts = runner.get_prompt_contexts()

    assert len(contexts) == 1
    context = contexts[0]
    assert context.scope == "candidate"
    assert context.candidate_index == 0
    assert context.candidate_name == "Plan A"
    assert context.step_id == "cost"
    assert context.agent_loop_session_id == attempt["transcript_id"]
    assert "C" in context.system_prompt
    assert [message.get_text() for message in context.messages] == ["resume cost context"]


@pytest.mark.asyncio
async def test_resume_candidate_ask_user_question_targets_matching_candidate(tmp_path, monkeypatch):
    runner = _build_parallel_runner(tmp_path)
    runner._step_attempts = {"eval": 1}
    captured: dict[int, dict[str, object]] = {}

    async def fake_execute_streaming(
        self,
        sub_spec,
        candidate,
        candidate_index,
        parent_context,
        session_id,
        **kwargs,
    ):
        captured[candidate_index] = {
            "user_message": kwargs.get("user_message"),
            "precompleted_tools": kwargs.get("precompleted_tools"),
        }
        yield PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_COMPLETED,
            step_id=None,
            timestamp=0.0,
            data={
                "sub_pipeline_id": f"evaluate_candidate_{candidate_index}",
                "candidate_index": candidate_index,
                "candidate_name": candidate["name"],
                "sub_pipeline_name": sub_spec.name,
                "failed": False,
                "conclusions": {"template": {"ok": candidate_index}, "cost": {"ok": candidate_index}},
            },
        )

    monkeypatch.setattr(
        "iac_code.pipeline.engine.sub_pipeline_executor.SubPipelineExecutor.execute_streaming",
        fake_execute_streaming,
    )

    async for _event in runner.resume_ask_user_question(
        {"selected_id": "nginx", "selected_label": "Nginx 网站", "free_text": ""},
        tool_use_id="ask-1",
        pending_input={
            "candidate": {"index": 0},
            "candidateStep": {"id": "template_gen"},
        },
    ):
        pass

    user_message = captured[0]["user_message"]
    assert isinstance(user_message, list)
    assert len(user_message) == 1
    assert isinstance(user_message[0], ToolResultBlock)
    assert user_message[0].tool_use_id == "ask-1"
    assert json.loads(user_message[0].content) == {
        "selected_id": "nginx",
        "selected_label": "Nginx 网站",
        "free_text": "",
    }
    assert captured[0]["precompleted_tools"] == {
        "ask_user_question": {"selected_id": "nginx", "selected_label": "Nginx 网站", "free_text": ""}
    }
    assert captured[1]["user_message"] is None
    assert captured[1]["precompleted_tools"] is None


@pytest.mark.asyncio
async def test_parent_step_transcript_lands_under_pipeline_transcripts(tmp_path, monkeypatch):
    _build_two_step_runner(tmp_path)
    storage = DirectorySessionStorage(tmp_path / "projects")
    runner = PipelineRunner(
        pipeline_dir=tmp_path,
        provider_manager=MagicMock(),
        base_tool_registry=MagicMock(),
        session_storage=storage,
        session_id="test",
        cwd=str(tmp_path),
    )

    class FakeAgentLoop:
        def __init__(self, **kwargs):
            self._session_storage = kwargs["session_storage"]
            self._session_id = kwargs["session_id"]
            self._cwd = kwargs["cwd"]
            self.current_turn_text = ""

        async def run_streaming(self, message):
            self._session_storage.append(self._cwd, self._session_id, Message(role="user", content=message))
            if False:
                yield

        async def continue_streaming(self):
            if False:
                yield

    monkeypatch.setattr("iac_code.agent.agent_loop.AgentLoop", FakeAgentLoop)

    async for _event in runner._continue_from_current():
        pass

    transcript_path = (
        storage.session_dir(str(tmp_path), "test")
        / "pipeline"
        / "transcripts"
        / "transcript_att_0001"
        / "session.jsonl"
    )
    root_session_path = storage.session_path(str(tmp_path), "transcript_att_0001")
    assert transcript_path.exists()
    assert not root_session_path.exists()


@pytest.mark.asyncio
async def test_restored_running_parent_attempt_loads_sidecar_transcript_for_resume(tmp_path):
    from iac_code.pipeline.engine.state_machine import StateMachine

    initial_runner = _build_two_step_runner(tmp_path)
    storage = DirectorySessionStorage(tmp_path / "projects")
    session_id = "test"
    cwd = str(tmp_path)
    sidecar = PipelineSession(storage.session_dir(cwd, session_id) / "pipeline")
    transcript_storage = PipelineTranscriptStorage(sidecar.session_dir)
    transcript_storage.save(cwd, "transcript_att_0001", [Message(role="user", content="resume me")])
    attempts = {
        "next_attempt_number": 2,
        "items": {
            "att_0001": {
                "attempt_id": "att_0001",
                "scope": "parent",
                "step_id": "a",
                "status": "running",
                "transcript_id": "transcript_att_0001",
            }
        },
    }
    execution = {
        "kind": "step",
        "step_id": "a",
        "active_attempt_id": "att_0001",
        "transcript_id": "transcript_att_0001",
    }
    state_machine_snapshot = StateMachine(
        initial_runner._loaded.steps, initial_runner._loaded.max_rollbacks
    ).to_snapshot()
    state_machine_snapshot["step_attempts"] = {"a": 1}
    state_machine_snapshot["current_step_user_input"] = "resume me"
    sidecar.save_running_sync(
        "a",
        state_machine_snapshot,
        initial_runner.context.to_snapshot(),
        initial_runner._pipeline_identity,
        execution=execution,
        attempts=attempts,
    )
    runner = PipelineRunner(
        pipeline_dir=tmp_path,
        provider_manager=MagicMock(),
        base_tool_registry=MagicMock(),
        session_storage=storage,
        session_id=session_id,
        cwd=cwd,
        resume_from_sidecar=True,
    )
    captured = {}
    runner._observability.step_started = MagicMock()

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        captured["user_message"] = user_message
        captured["resume_messages"] = kwargs["resume_messages"]
        captured["attempt_id"] = kwargs["attempt_id"]
        captured["transcript_id"] = kwargs["transcript_id"]
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion={"ok": True})

    runner._step_executor.execute = fake_execute

    events = []
    async for _event in runner.continue_from_sidecar():
        events.append(_event)
        if "resume_messages" in captured:
            break

    assert [message.content for message in captured["resume_messages"]] == ["resume me"]
    assert captured["user_message"] is None
    assert captured["attempt_id"] == "att_0001"
    assert captured["transcript_id"] == "transcript_att_0001"
    assert runner._step_attempts == {"a": 1}
    runner._observability.step_started.assert_not_called()
    assert not any(
        isinstance(event, PipelineEvent) and event.type == PipelineEventType.STEP_STARTED for event in events
    )


def test_fresh_runner_after_terminal_sidecar_allocates_new_attempt(tmp_path):
    initial_runner = _build_two_step_runner(tmp_path)
    storage = DirectorySessionStorage(tmp_path / "projects")
    session_id = "test"
    cwd = str(tmp_path)
    sidecar = PipelineSession(storage.session_dir(cwd, session_id) / "pipeline")
    transcript_storage = PipelineTranscriptStorage(sidecar.session_dir)
    transcript_storage.save(cwd, "transcript_att_0001", [Message(role="user", content="old run")])
    sidecar.save_completed_sync(
        "a",
        initial_runner.state_machine.to_snapshot(),
        initial_runner.context.to_snapshot(),
        initial_runner._pipeline_identity,
        execution={},
        attempts={
            "next_attempt_number": 2,
            "items": {
                "att_0001": {
                    "attempt_id": "att_0001",
                    "scope": "parent",
                    "step_id": "a",
                    "status": "completed",
                    "transcript_id": "transcript_att_0001",
                }
            },
        },
    )

    runner = PipelineRunner(
        pipeline_dir=tmp_path,
        provider_manager=MagicMock(),
        base_tool_registry=MagicMock(),
        session_storage=storage,
        session_id=session_id,
        cwd=cwd,
    )
    attempt = runner._ensure_parent_attempt("a")

    assert attempt["attempt_id"] == "att_0002"
    assert attempt["transcript_id"] == "transcript_att_0002"
    assert transcript_storage.exists(cwd, "transcript_att_0001")


def test_fresh_runner_after_corrupt_sidecar_avoids_existing_transcript_ids(tmp_path):
    _build_two_step_runner(tmp_path)
    storage = DirectorySessionStorage(tmp_path / "projects")
    session_id = "test"
    cwd = str(tmp_path)
    sidecar = PipelineSession(storage.session_dir(cwd, session_id) / "pipeline")
    transcript_storage = PipelineTranscriptStorage(sidecar.session_dir)
    transcript_storage.save(cwd, "transcript_att_0003", [Message(role="user", content="old run")])
    sidecar.session_dir.mkdir(parents=True, exist_ok=True)
    sidecar.meta_path.write_text("status: [", encoding="utf-8")

    runner = PipelineRunner(
        pipeline_dir=tmp_path,
        provider_manager=MagicMock(),
        base_tool_registry=MagicMock(),
        session_storage=storage,
        session_id=session_id,
        cwd=cwd,
    )
    attempt = runner._ensure_parent_attempt("a")

    assert attempt["attempt_id"] == "att_0004"
    assert attempt["transcript_id"] == "transcript_att_0004"
    assert transcript_storage.exists(cwd, "transcript_att_0003")


@pytest.mark.parametrize("terminal_status", ["completed", "rolled_back", "discarded", "failed"])
def test_ensure_parent_attempt_reuses_only_running_parent_attempts(tmp_path, terminal_status):
    runner = _build_two_step_runner(tmp_path)
    first = runner._ensure_parent_attempt("a")
    runner._mark_attempt_status(first["attempt_id"], terminal_status)

    second = runner._ensure_parent_attempt("a")

    assert second["attempt_id"] != first["attempt_id"]
    assert second["status"] == "running"


@pytest.mark.asyncio
async def test_auto_advanced_completed_step_does_not_persist_stale_active_attempt(tmp_path):
    runner = _build_two_step_runner(tmp_path)
    runner.session = CapturingPipelineSession()

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        conclusion = {"value": step.step_id}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

    runner._step_executor.execute = fake_execute

    async for _event in runner._continue_from_current():
        if any(call[0] == "running" and call[1] == "b" for call in runner.session.calls):
            break

    advanced_save = next(save for save in runner.session.saved if save[0] == "running" and save[1] == "b")
    assert advanced_save[3]["execution"] is None
    assert advanced_save[3]["attempts"]["items"]["att_0001"]["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("yield_result", [False, True])
async def test_failed_or_no_result_parent_attempt_is_marked_failed_before_save(tmp_path, yield_result):
    runner = _build_two_step_runner(tmp_path)
    runner.session = CapturingPipelineSession()

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        if yield_result:
            yield StepResult(step_id=step.step_id, status=StepStatus.FAILED, error="bad step")
        elif False:
            yield

    runner._step_executor.execute = fake_execute

    async for _event in runner._continue_from_current():
        pass

    failed_save = next(save for save in runner.session.saved if save[0] == "failed")
    assert failed_save[3]["execution"] is None
    assert failed_save[3]["attempts"]["items"]["att_0001"]["status"] == "failed"


def test_real_sidecar_auto_advanced_metadata_has_no_stale_active_attempt(tmp_path):
    _build_two_step_runner(tmp_path)
    storage = DirectorySessionStorage(tmp_path / "projects")
    runner = PipelineRunner(
        pipeline_dir=tmp_path,
        provider_manager=MagicMock(),
        base_tool_registry=MagicMock(),
        session_storage=storage,
        session_id="test",
        cwd=str(tmp_path),
    )
    runner._ensure_parent_attempt("a")
    asyncio.run(runner._save_running("a", reason="step started"))
    runner._mark_attempt_status("att_0001", "completed")
    runner.state_machine.advance()

    asyncio.run(runner._save_after_advance("a"))

    meta_path = storage.session_dir(str(tmp_path), "test") / "pipeline" / "meta.yaml"
    meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
    assert "execution" not in meta
    assert meta["attempts"]["items"]["att_0001"]["status"] == "completed"


class TestPipelineRunnerBuild:
    def test_builds_5_steps(self):
        storage = FakeSessionStorage()
        runner = PipelineRunner(
            pipeline_dir=_selling_dir(),
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=storage,
            session_id="test123",
        )
        assert runner.state_machine.total_steps == 5
        assert runner.pipeline_name == "selling"

    def test_has_sub_pipeline(self):
        storage = FakeSessionStorage()
        runner = PipelineRunner(
            pipeline_dir=_selling_dir(),
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=storage,
            session_id="test123",
        )
        assert "evaluate_candidate" in runner._loaded.sub_pipelines
        sub = runner._loaded.sub_pipelines["evaluate_candidate"]
        assert sub.iterate_over == "architecture.candidates"


class TestPipelineRunnerContextDeps:
    def test_context_deps_keys_match_step_conclusion_fields(self):
        storage = FakeSessionStorage()
        runner = PipelineRunner(
            pipeline_dir=_selling_dir(),
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=storage,
            session_id="test123",
        )
        dep_fields = set(runner.context._deps.keys())
        step_fields = {s.conclusion_field for s in runner._loaded.steps}
        assert step_fields.issubset(dep_fields)


class TestParallelSubPipelineStep:
    @pytest.mark.asyncio
    async def test_parallel_step_invokes_sub_pipeline_executor(self, tmp_path):
        """parallel_sub_pipeline step should fan-out to SubPipelineExecutor."""
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "arch.md").write_text("Arch", encoding="utf-8")
        (tmp_path / "prompts" / "eval.md").write_text("Eval", encoding="utf-8")
        (tmp_path / "prompts" / "template.md").write_text("T", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              architecture: []
              evaluated: [architecture]
            max_rollbacks: 3
            sub_pipelines:
              evaluate_candidate:
                max_rollbacks: 2
                iterate_over: architecture.candidates
                context_fields_from_parent: []
                steps:
                  - id: template_gen
                    conclusion_field: template
                    forward: null
                    prompt: prompts/template.md
                    context_fields: [candidate]
            steps:
              - id: arch
                conclusion_field: architecture
                forward: eval
                prompt: prompts/arch.md
              - id: eval
                type: parallel_sub_pipeline
                sub_pipeline: evaluate_candidate
                conclusion_field: evaluated
                forward: null
                prompt: prompts/eval.md
        """),
            encoding="utf-8",
        )

        storage = FakeSessionStorage()
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=storage,
            session_id="test123",
        )

        # Pre-set architecture conclusion with candidates
        runner.context.set_conclusion(
            "architecture",
            {
                "candidates": [
                    {"name": "Plan A"},
                    {"name": "Plan B"},
                ]
            },
        )
        # Advance past arch step
        runner.state_machine.advance()

        # Mock SubPipelineExecutor.execute_streaming
        parent_step_ids = []

        async def mock_execute_streaming(
            sub_spec,
            candidate,
            candidate_index,
            parent_context,
            session_id,
            *,
            start_from_step=None,
            preserved_conclusions=None,
            user_message=None,
            parent_step_id=None,
            **kwargs,
        ):
            parent_step_ids.append(parent_step_id)
            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_STARTED,
                step_id=None,
                timestamp=0,
                data={
                    "sub_pipeline_id": f"eval_{candidate_index}",
                    "candidate_index": candidate_index,
                    "candidate_name": candidate["name"],
                    "total_steps": 1,
                    "sub_pipeline_name": "evaluate_candidate",
                },
            )
            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_COMPLETED,
                step_id=None,
                timestamp=0,
                data={
                    "sub_pipeline_id": f"eval_{candidate_index}",
                    "candidate_index": candidate_index,
                    "failed": False,
                    "conclusions": {"template": {"body": f"template_{candidate_index}"}},
                },
            )

        with patch("iac_code.pipeline.engine.pipeline_runner.SubPipelineExecutor") as mock_sub_exec:
            instance = MagicMock()
            instance.execute_streaming = mock_execute_streaming
            mock_sub_exec.return_value = instance

            events = []
            async for event in runner._continue_from_current():
                events.append(event)

        # Should have set evaluated conclusion
        evaluated = runner.context.get_conclusion("evaluated")
        assert evaluated is not None
        assert len(evaluated) == 2
        assert evaluated[0]["candidate"] == {"name": "Plan A"}
        assert evaluated[1]["candidate"] == {"name": "Plan B"}
        assert parent_step_ids == ["eval", "eval"]

    @pytest.mark.asyncio
    async def test_parallel_candidate_executor_exception_emits_failed_event_and_telemetry(self, tmp_path, caplog):
        caplog.set_level(logging.WARNING, logger="iac_code.pipeline.engine.pipeline_runner")
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "arch.md").write_text("Arch", encoding="utf-8")
        (tmp_path / "prompts" / "eval.md").write_text("Eval", encoding="utf-8")
        (tmp_path / "prompts" / "template.md").write_text("T", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              architecture: []
              evaluated: [architecture]
            max_rollbacks: 3
            sub_pipelines:
              evaluate_candidate:
                max_rollbacks: 2
                iterate_over: architecture.candidates
                context_fields_from_parent: []
                steps:
                  - id: template_gen
                    conclusion_field: template
                    forward: null
                    prompt: prompts/template.md
                    context_fields: [candidate]
            steps:
              - id: arch
                conclusion_field: architecture
                forward: eval
                prompt: prompts/arch.md
              - id: eval
                type: parallel_sub_pipeline
                sub_pipeline: evaluate_candidate
                conclusion_field: evaluated
                forward: null
                prompt: prompts/eval.md
        """),
            encoding="utf-8",
        )

        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=FakeSessionStorage(),
            session_id="test123",
        )
        runner.session = CapturingPipelineSession()
        runner.context.set_conclusion("architecture", {"candidates": [{"name": "Plan A"}]})
        runner.state_machine.advance()
        runner._observability.now = MagicMock(return_value=10.0)
        runner._observability.duration_ms = MagicMock(return_value=33.0)
        runner._observability.sub_pipeline_completed = MagicMock()

        async def failing_execute_streaming(*args, **kwargs):
            raise RuntimeError("lost worker token=abc123 /Users/alice/project")
            if False:
                yield

        with patch("iac_code.pipeline.engine.pipeline_runner.SubPipelineExecutor") as mock_sub_exec:
            instance = MagicMock()
            instance.execute_streaming = failing_execute_streaming
            instance.current_step_executor_agent_loop = None
            mock_sub_exec.return_value = instance

            step = runner.state_machine.current_step
            events = [event async for event in runner._execute_parallel_sub_pipeline(step)]

        failed_events = [
            event
            for event in events
            if isinstance(event, PipelineEvent)
            and event.type == PipelineEventType.SUB_PIPELINE_COMPLETED
            and event.data.get("failed") is True
        ]
        assert len(failed_events) == 1
        failed_event = failed_events[0]
        assert failed_event.data["sub_pipeline_id"] == "evaluate_candidate_candidate_0"
        assert failed_event.data["candidate_index"] == 0
        assert failed_event.data["candidate_name"] == "Plan A"
        assert failed_event.data["sub_pipeline_name"] == "evaluate_candidate"
        assert failed_event.data["error_summary"] == "lost worker token=[REDACTED] [PATH]"
        assert failed_event.data["error_details"]["type"] == "RuntimeError"
        assert "error_id" in failed_event.data["error_details"]
        assert "abc123" not in str(failed_event.data)
        assert "/Users/alice" not in str(failed_event.data)
        evaluated = runner.context.get_conclusion("evaluated")
        assert evaluated == [
            {
                "candidate": {"name": "Plan A"},
                "failed": True,
                "error": "lost worker token=[REDACTED] [PATH]",
                "error_details": failed_event.data["error_details"],
            }
        ]
        failed_save = next(save for save in reversed(runner.session.saved) if save[2] == "parallel candidate failed")
        failed_state = failed_save[3]["execution"]["candidates"]["0"]
        assert failed_state["error"] == "lost worker token=[REDACTED] [PATH]"
        assert failed_state["error_details"] == failed_event.data["error_details"]
        runner._observability.sub_pipeline_completed.assert_called_once_with(
            duration_ms=33.0,
            failed=True,
            parent_step_id="eval",
            sub_pipeline_name="evaluate_candidate",
            sub_pipeline_id="evaluate_candidate_candidate_0",
            candidate_index=0,
            candidate_name="Plan A",
            total_steps=1,
            error_summary="lost worker token=[REDACTED] [PATH]",
            error_type="RuntimeError",
            error_id=failed_event.data["error_details"]["error_id"],
        )
        record = next(record for record in caplog.records if record.message.startswith("Pipeline candidate failed:"))
        assert "pipeline=test" in record.message
        assert "session_id=test123" in record.message
        assert "parent_step_id=eval" in record.message
        assert "sub_pipeline_name=evaluate_candidate" in record.message
        assert "sub_pipeline_id=evaluate_candidate_candidate_0" in record.message
        assert "candidate_index=0" in record.message
        assert "candidate_name=Plan A" in record.message
        assert "error_type=RuntimeError" in record.message
        assert "error_summary=lost worker token=[REDACTED] [PATH]" in record.message
        assert record.pipeline == "test"
        assert record.session_id == "test123"
        assert record.parent_step_id == "eval"
        assert record.sub_pipeline_name == "evaluate_candidate"
        assert record.sub_pipeline_id == "evaluate_candidate_candidate_0"
        assert record.candidate_index == 0
        assert record.candidate_name == "Plan A"
        assert record.error_type == "RuntimeError"
        assert record.error_summary == "lost worker token=[REDACTED] [PATH]"
        assert record.error_id == failed_event.data["error_details"]["error_id"]

    @pytest.mark.asyncio
    async def test_parallel_candidate_failure_save_failure_stops_before_failed_event(self, tmp_path):
        runner = _build_parallel_runner(tmp_path)
        runner.session = FailingCandidateFailedPipelineSession()
        step = runner.state_machine.current_step

        async def failing_execute_streaming(*args, **kwargs):
            raise RuntimeError("candidate worker failed")
            if False:
                yield

        with patch("iac_code.pipeline.engine.pipeline_runner.SubPipelineExecutor") as mock_sub_exec:
            instance = MagicMock()
            instance.execute_streaming = failing_execute_streaming
            instance.current_step_executor_agent_loop = None
            mock_sub_exec.return_value = instance

            events = [event async for event in runner._execute_parallel_sub_pipeline(step)]

        assert any(
            isinstance(event, PipelineEvent)
            and event.type == PipelineEventType.STEP_FAILED
            and event.data["error_details"]["type"] == "PipelineStatePersistenceError"
            for event in events
        )
        assert not any(
            isinstance(event, PipelineEvent)
            and event.type == PipelineEventType.SUB_PIPELINE_COMPLETED
            and event.data.get("failed") is True
            for event in events
        )
        assert not any(
            isinstance(event, PipelineEvent) and event.type == PipelineEventType.STEP_COMPLETED for event in events
        )
        assert ("running_attempted", "eval", 1, "parallel candidate failed") in runner.session.calls

    @pytest.mark.asyncio
    async def test_resolve_iterate_field_raises_for_missing_and_returns_for_present(self, tmp_path):
        """P-I16: _resolve_iterate_field raises ValueError when context field is missing,
        and returns the list when present."""
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "arch.md").write_text("Arch", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              architecture: []
            max_rollbacks: 3
            steps:
              - id: arch
                conclusion_field: architecture
                forward: null
                prompt: prompts/arch.md
        """),
            encoding="utf-8",
        )

        storage = FakeSessionStorage()
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=storage,
            session_id="test123",
        )

        with pytest.raises(ValueError, match="not present in context"):
            runner._resolve_iterate_field("architecture.candidates")
        runner.context.set_conclusion("architecture", {"candidates": [{"x": 1}]})
        assert runner._resolve_iterate_field("architecture.candidates") == [{"x": 1}]

    @pytest.mark.asyncio
    async def test_parallel_restore_preserves_completed_and_resumes_running_candidate(self, tmp_path):
        runner = _build_parallel_runner(tmp_path)
        step = runner.state_machine.current_step
        completed_conclusions = {"template": {"body": "done"}}
        running_state = {
            "status": "running",
            "candidate": {"name": "Plan B"},
            "sub_pipeline_id": "evaluate_candidate_b",
            "state_machine": {
                "current_index": 1,
                "rollback_count": 0,
                "interrupt_rollback_count": 0,
                "step_statuses": {"template_gen": "completed", "cost": "running"},
            },
            "context": {
                "candidate": {
                    "value": {"name": "Plan B"},
                    "version": 1,
                    "stale": False,
                    "updated_at": None,
                    "history": [],
                },
                "template": {
                    "value": {"body": "resume me"},
                    "version": 1,
                    "stale": False,
                    "updated_at": None,
                    "history": [],
                },
            },
            "current_sub_step": "cost",
            "current_index": 1,
            "active_attempt_id": "att_0003",
            "transcript_id": "transcript_att_0003",
        }
        runner._execution = {
            "kind": "parallel_sub_pipeline",
            "step_id": "eval",
            "sub_pipeline_name": "evaluate_candidate",
            "candidates": {
                "0": {
                    "status": "completed",
                    "candidate": {"name": "Plan A"},
                    "sub_pipeline_id": "evaluate_candidate_a",
                    "conclusions": completed_conclusions,
                },
                "1": running_state,
            },
        }
        runner._attempts = {
            "next_attempt_number": 4,
            "items": {
                "att_0003": {
                    "attempt_id": "att_0003",
                    "scope": "sub_step",
                    "parent_step_id": "eval",
                    "candidate_index": 1,
                    "sub_pipeline_id": "evaluate_candidate_b",
                    "sub_step_id": "cost",
                    "status": "running",
                    "transcript_id": "transcript_att_0003",
                }
            },
        }

        calls = []

        async def execute_streaming(
            sub_spec,
            candidate,
            candidate_index,
            parent_context,
            session_id,
            **kwargs,
        ):
            calls.append((candidate_index, candidate, kwargs.get("resume_state")))
            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_COMPLETED,
                step_id=None,
                timestamp=0,
                data={
                    "sub_pipeline_id": kwargs["resume_state"]["sub_pipeline_id"],
                    "candidate_index": candidate_index,
                    "failed": False,
                    "conclusions": {"template": {"body": "resume me"}, "cost": {"total": 10}},
                },
            )

        with patch("iac_code.pipeline.engine.pipeline_runner.SubPipelineExecutor") as mock_sub_exec:
            instance = MagicMock()
            instance.execute_streaming = execute_streaming
            instance.current_step_executor_agent_loop = None
            mock_sub_exec.return_value = instance

            async for _event in runner._execute_parallel_sub_pipeline(step):
                pass

        assert calls == [(1, {"name": "Plan B"}, running_state)]
        assert runner.context.get_conclusion("evaluated") == [
            {"candidate": {"name": "Plan A"}, "failed": False, "template": {"body": "done"}},
            {
                "candidate": {"name": "Plan B"},
                "failed": False,
                "template": {"body": "resume me"},
                "cost": {"total": 10},
            },
        ]

    @pytest.mark.asyncio
    async def test_parallel_restore_preserves_failed_and_resumes_only_running_candidate(self, tmp_path):
        runner = _build_parallel_runner(tmp_path)
        step = runner.state_machine.current_step
        failed_state = {
            "status": "failed",
            "candidate": {"name": "Plan A"},
            "sub_pipeline_id": "evaluate_candidate_a",
            "state_machine": {
                "current_index": 1,
                "rollback_count": 0,
                "interrupt_rollback_count": 0,
                "step_statuses": {"template_gen": "completed", "cost": "running"},
            },
            "context": {
                "candidate": {
                    "value": {"name": "Plan A"},
                    "version": 1,
                    "stale": False,
                    "updated_at": None,
                    "history": [],
                },
                "template": {
                    "value": {"body": "partial"},
                    "version": 1,
                    "stale": False,
                    "updated_at": None,
                    "history": [],
                },
            },
            "current_sub_step": "cost",
            "current_index": 1,
            "active_attempt_id": "att_0002",
            "transcript_id": "transcript_att_0002",
            "conclusions": {"template": {"body": "partial"}},
        }
        running_state = {
            "status": "running",
            "candidate": {"name": "Plan B"},
            "sub_pipeline_id": "evaluate_candidate_b",
            "state_machine": {
                "current_index": 1,
                "rollback_count": 0,
                "interrupt_rollback_count": 0,
                "step_statuses": {"template_gen": "completed", "cost": "running"},
            },
            "context": {
                "candidate": {
                    "value": {"name": "Plan B"},
                    "version": 1,
                    "stale": False,
                    "updated_at": None,
                    "history": [],
                },
                "template": {
                    "value": {"body": "resume me"},
                    "version": 1,
                    "stale": False,
                    "updated_at": None,
                    "history": [],
                },
            },
            "current_sub_step": "cost",
            "current_index": 1,
            "active_attempt_id": "att_0003",
            "transcript_id": "transcript_att_0003",
        }
        runner._execution = {
            "kind": "parallel_sub_pipeline",
            "step_id": "eval",
            "sub_pipeline_name": "evaluate_candidate",
            "candidates": {
                "0": failed_state,
                "1": running_state,
            },
        }
        runner._attempts = {
            "next_attempt_number": 4,
            "items": {
                "att_0002": {
                    "attempt_id": "att_0002",
                    "scope": "sub_step",
                    "parent_step_id": "eval",
                    "candidate_index": 0,
                    "sub_pipeline_id": "evaluate_candidate_a",
                    "sub_step_id": "cost",
                    "status": "failed",
                    "transcript_id": "transcript_att_0002",
                },
                "att_0003": {
                    "attempt_id": "att_0003",
                    "scope": "sub_step",
                    "parent_step_id": "eval",
                    "candidate_index": 1,
                    "sub_pipeline_id": "evaluate_candidate_b",
                    "sub_step_id": "cost",
                    "status": "running",
                    "transcript_id": "transcript_att_0003",
                },
            },
        }

        calls = []

        async def execute_streaming(
            sub_spec,
            candidate,
            candidate_index,
            parent_context,
            session_id,
            **kwargs,
        ):
            calls.append((candidate_index, candidate, kwargs.get("resume_state")))
            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_COMPLETED,
                step_id=None,
                timestamp=0,
                data={
                    "sub_pipeline_id": kwargs["resume_state"]["sub_pipeline_id"],
                    "candidate_index": candidate_index,
                    "failed": False,
                    "conclusions": {"template": {"body": "resume me"}, "cost": {"total": 10}},
                },
            )

        with patch("iac_code.pipeline.engine.pipeline_runner.SubPipelineExecutor") as mock_sub_exec:
            instance = MagicMock()
            instance.execute_streaming = execute_streaming
            instance.current_step_executor_agent_loop = None
            mock_sub_exec.return_value = instance

            async for _event in runner._execute_parallel_sub_pipeline(step):
                pass

        assert calls == [(1, {"name": "Plan B"}, running_state)]
        assert runner.context.get_conclusion("evaluated") == [
            {"candidate": {"name": "Plan A"}, "failed": True, "template": {"body": "partial"}},
            {
                "candidate": {"name": "Plan B"},
                "failed": False,
                "template": {"body": "resume me"},
                "cost": {"total": 10},
            },
        ]

    @pytest.mark.asyncio
    async def test_parallel_live_failed_candidate_aggregate_keeps_partial_conclusions_and_error(self, tmp_path):
        runner = _build_parallel_runner(tmp_path)
        step = runner.state_machine.current_step

        async def execute_streaming(
            sub_spec,
            candidate,
            candidate_index,
            parent_context,
            session_id,
            **kwargs,
        ):
            if candidate_index == 0:
                await kwargs["sub_step_state_callback"](
                    {
                        "status": "running",
                        "attempt_status": "completed",
                        "candidate_index": candidate_index,
                        "candidate": candidate,
                        "sub_pipeline_id": "evaluate_candidate_a",
                        "sub_pipeline_name": "evaluate_candidate",
                        "current_sub_step": "cost",
                        "current_index": 1,
                        "state_machine": {
                            "current_index": 1,
                            "rollback_count": 0,
                            "interrupt_rollback_count": 0,
                            "step_statuses": {"template_gen": "completed", "cost": "running"},
                        },
                        "context": {
                            "candidate": {
                                "value": candidate,
                                "version": 1,
                                "stale": False,
                                "updated_at": None,
                                "history": [],
                            },
                            "template": {
                                "value": {"body": "partial"},
                                "version": 1,
                                "stale": False,
                                "updated_at": None,
                                "history": [],
                            },
                        },
                        "active_attempt_id": "att_live_failed",
                        "transcript_id": "transcript_live_failed",
                        "conclusions": {"template": {"body": "partial"}},
                    }
                )
                yield PipelineEvent(
                    type=PipelineEventType.SUB_PIPELINE_COMPLETED,
                    step_id=None,
                    timestamp=0,
                    data={
                        "sub_pipeline_id": "evaluate_candidate_a",
                        "candidate_index": candidate_index,
                        "failed": True,
                        "error": "cost failed",
                        "error_summary": "cost failed",
                        "error_details": {"type": "StepFailed", "step_id": "cost"},
                    },
                )
                return

            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_COMPLETED,
                step_id=None,
                timestamp=0,
                data={
                    "sub_pipeline_id": "evaluate_candidate_b",
                    "candidate_index": candidate_index,
                    "failed": False,
                    "conclusions": {"template": {"body": "done"}, "cost": {"total": 10}},
                },
            )

        with patch("iac_code.pipeline.engine.pipeline_runner.SubPipelineExecutor") as mock_sub_exec:
            instance = MagicMock()
            instance.execute_streaming = execute_streaming
            instance.current_step_executor_agent_loop = None
            mock_sub_exec.return_value = instance

            async for _event in runner._execute_parallel_sub_pipeline(step):
                pass

        assert runner.context.get_conclusion("evaluated") == [
            {
                "candidate": {"name": "Plan A"},
                "failed": True,
                "template": {"body": "partial"},
                "error": "cost failed",
                "error_details": {"type": "StepFailed", "step_id": "cost"},
            },
            {
                "candidate": {"name": "Plan B"},
                "failed": False,
                "template": {"body": "done"},
                "cost": {"total": 10},
            },
        ]

    @pytest.mark.asyncio
    async def test_parallel_legacy_failed_completed_event_gets_public_error_details(self, tmp_path):
        runner = _build_parallel_runner(tmp_path)
        step = runner.state_machine.current_step

        async def execute_streaming(
            sub_spec,
            candidate,
            candidate_index,
            parent_context,
            session_id,
            **kwargs,
        ):
            if candidate_index == 0:
                yield PipelineEvent(
                    type=PipelineEventType.SUB_PIPELINE_COMPLETED,
                    step_id=None,
                    timestamp=0,
                    data={
                        "sub_pipeline_id": "evaluate_candidate_a",
                        "candidate_index": candidate_index,
                        "failed": True,
                        "error": "legacy failure token=secret-value at /Users/alice/work",
                    },
                )
                return

            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_COMPLETED,
                step_id=None,
                timestamp=0,
                data={
                    "sub_pipeline_id": "evaluate_candidate_b",
                    "candidate_index": candidate_index,
                    "failed": False,
                    "conclusions": {"template": {"body": "done"}, "cost": {"total": 10}},
                },
            )

        with patch("iac_code.pipeline.engine.pipeline_runner.SubPipelineExecutor") as mock_sub_exec:
            instance = MagicMock()
            instance.execute_streaming = execute_streaming
            instance.current_step_executor_agent_loop = None
            mock_sub_exec.return_value = instance

            events = [event async for event in runner._execute_parallel_sub_pipeline(step)]

        failed_event = next(
            event
            for event in events
            if isinstance(event, PipelineEvent)
            and event.type == PipelineEventType.SUB_PIPELINE_COMPLETED
            and event.data.get("failed") is True
        )
        rendered_event = str(failed_event.data)
        assert "secret-value" not in rendered_event
        assert "/Users/alice" not in rendered_event
        assert failed_event.data["error"] == failed_event.data["error_summary"]
        assert failed_event.data["error_details"]["type"] == "SubPipelineFailed"
        assert failed_event.data["error_details"]["error_id"]

        evaluated = runner.context.get_conclusion("evaluated")
        assert "secret-value" not in str(evaluated)
        assert "/Users/alice" not in str(evaluated)
        assert evaluated[0]["error"] == failed_event.data["error_summary"]
        assert evaluated[0]["error_details"]["error_id"] == failed_event.data["error_details"]["error_id"]

    @pytest.mark.asyncio
    async def test_parallel_allocator_failure_does_not_mark_previous_completed_attempt_failed(self, tmp_path):
        runner = _build_parallel_runner(tmp_path)
        runner.session = CapturingPipelineSession()
        step = runner.state_machine.current_step

        async def execute_streaming(
            sub_spec,
            candidate,
            candidate_index,
            parent_context,
            session_id,
            **kwargs,
        ):
            if candidate_index == 0:
                attempt = kwargs["sub_step_attempt_allocator"](
                    {
                        "candidate_index": candidate_index,
                        "candidate": candidate,
                        "sub_pipeline_id": "evaluate_candidate_a",
                        "sub_pipeline_name": "evaluate_candidate",
                        "parent_step_id": "eval",
                        "sub_step_id": "template_gen",
                        "current_index": 0,
                        "state_machine": {
                            "current_index": 0,
                            "rollback_count": 0,
                            "interrupt_rollback_count": 0,
                            "step_statuses": {"template_gen": "running", "cost": "pending"},
                        },
                        "context": {},
                    }
                )
                await kwargs["sub_step_state_callback"](
                    {
                        "status": "running",
                        "attempt_status": "completed",
                        "candidate_index": candidate_index,
                        "candidate": candidate,
                        "sub_pipeline_id": "evaluate_candidate_a",
                        "sub_pipeline_name": "evaluate_candidate",
                        "current_sub_step": "cost",
                        "current_index": 1,
                        "state_machine": {
                            "current_index": 1,
                            "rollback_count": 0,
                            "interrupt_rollback_count": 0,
                            "step_statuses": {"template_gen": "completed", "cost": "pending"},
                        },
                        "context": {},
                        "active_attempt_id": attempt["attempt_id"],
                        "transcript_id": attempt["transcript_id"],
                        "conclusions": {"template": {"body": "done"}},
                    }
                )
                yield PipelineEvent(
                    type=PipelineEventType.SUB_PIPELINE_COMPLETED,
                    step_id=None,
                    timestamp=0,
                    data={
                        "sub_pipeline_id": "evaluate_candidate_a",
                        "candidate_index": candidate_index,
                        "failed": True,
                        "error": "allocator failed",
                        "error_summary": "allocator failed",
                        "error_details": {"type": "RuntimeError", "error_id": "err1"},
                    },
                )
                return

            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_COMPLETED,
                step_id=None,
                timestamp=0,
                data={
                    "sub_pipeline_id": "evaluate_candidate_b",
                    "candidate_index": candidate_index,
                    "failed": False,
                    "conclusions": {"template": {"body": "done"}, "cost": {"total": 10}},
                },
            )

        with patch("iac_code.pipeline.engine.pipeline_runner.SubPipelineExecutor") as mock_sub_exec:
            instance = MagicMock()
            instance.execute_streaming = execute_streaming
            instance.current_step_executor_agent_loop = None
            mock_sub_exec.return_value = instance

            async for _event in runner._execute_parallel_sub_pipeline(step):
                pass

        attempt = runner._attempts["items"]["att_0001"]
        assert attempt["status"] == "completed"
        failed_save = next(save for save in reversed(runner.session.saved) if save[2] == "parallel candidate failed")
        failed_state = failed_save[3]["execution"]["candidates"]["0"]
        assert failed_state["status"] == "failed"
        assert failed_state["active_attempt_id"] is None

    @pytest.mark.asyncio
    async def test_parallel_running_sub_step_state_persisted_with_attempt_metadata(self, tmp_path):
        runner = _build_parallel_runner(tmp_path)
        runner.session = CapturingPipelineSession()
        step = runner.state_machine.current_step

        async def execute_streaming(
            sub_spec,
            candidate,
            candidate_index,
            parent_context,
            session_id,
            **kwargs,
        ):
            attempt = kwargs["sub_step_attempt_allocator"](
                {
                    "candidate_index": candidate_index,
                    "candidate": candidate,
                    "sub_pipeline_id": "evaluate_candidate_a",
                    "sub_pipeline_name": "evaluate_candidate",
                    "parent_step_id": "eval",
                    "sub_step_id": "template_gen",
                    "current_index": 0,
                    "state_machine": {
                        "current_index": 0,
                        "rollback_count": 0,
                        "interrupt_rollback_count": 0,
                        "step_statuses": {"template_gen": "running", "cost": "pending"},
                    },
                    "context": {
                        "candidate": {
                            "value": candidate,
                            "version": 1,
                            "stale": False,
                            "updated_at": None,
                            "history": [],
                        }
                    },
                }
            )
            await kwargs["sub_step_state_callback"](
                {
                    "status": "running",
                    "attempt_status": "running",
                    "candidate_index": candidate_index,
                    "candidate": candidate,
                    "sub_pipeline_id": "evaluate_candidate_a",
                    "sub_pipeline_name": "evaluate_candidate",
                    "current_sub_step": "template_gen",
                    "current_index": 0,
                    "state_machine": {
                        "current_index": 0,
                        "rollback_count": 0,
                        "interrupt_rollback_count": 0,
                        "step_statuses": {"template_gen": "running", "cost": "pending"},
                    },
                    "context": {
                        "candidate": {
                            "value": candidate,
                            "version": 1,
                            "stale": False,
                            "updated_at": None,
                            "history": [],
                        }
                    },
                    "active_attempt_id": attempt["attempt_id"],
                    "transcript_id": attempt["transcript_id"],
                }
            )
            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_STARTED,
                step_id=None,
                timestamp=0,
                data={
                    "sub_pipeline_id": "evaluate_candidate_a",
                    "candidate_index": candidate_index,
                    "candidate_name": candidate["name"],
                    "total_steps": 2,
                    "sub_pipeline_name": "evaluate_candidate",
                },
            )

        with patch("iac_code.pipeline.engine.pipeline_runner.SubPipelineExecutor") as mock_sub_exec:
            instance = MagicMock()
            instance.execute_streaming = execute_streaming
            instance.current_step_executor_agent_loop = None
            mock_sub_exec.return_value = instance

            gen = runner._execute_parallel_sub_pipeline(step)
            await gen.__anext__()
            await gen.aclose()

        running_save = next(save for save in runner.session.saved if save[0] == "running")
        execution = running_save[3]["execution"]
        candidate_state = execution["candidates"]["0"]
        assert execution["kind"] == "parallel_sub_pipeline"
        assert candidate_state["status"] == "running"
        assert candidate_state["current_sub_step"] == "template_gen"
        assert candidate_state["active_attempt_id"] == "att_0001"
        assert candidate_state["transcript_id"] == "transcript_att_0001"
        assert candidate_state["state_machine"]["current_index"] == 0
        assert candidate_state["context"]["candidate"]["value"] == {"name": "Plan A"}
        attempt = running_save[3]["attempts"]["items"]["att_0001"]
        assert attempt["scope"] == "sub_step"
        assert attempt["parent_step_id"] == "eval"
        assert attempt["candidate_index"] == 0
        assert attempt["sub_pipeline_id"] == "evaluate_candidate_a"
        assert attempt["sub_step_id"] == "template_gen"
        assert attempt["status"] == "running"

    @pytest.mark.asyncio
    async def test_sub_step_rollback_allocates_new_attempt_for_target(self, tmp_path):
        runner = _build_parallel_runner(tmp_path)

        first = runner._ensure_sub_step_attempt(
            parent_step_id="eval",
            candidate_index=0,
            sub_pipeline_id="evaluate_candidate_a",
            sub_step_id="cost",
        )
        runner._mark_attempt_status(first["attempt_id"], "rolled_back")
        second = runner._ensure_sub_step_attempt(
            parent_step_id="eval",
            candidate_index=0,
            sub_pipeline_id="evaluate_candidate_a",
            sub_step_id="template_gen",
        )

        assert first["attempt_id"] == "att_0001"
        assert first["status"] == "rolled_back"
        assert second["attempt_id"] == "att_0002"
        assert second["sub_step_id"] == "template_gen"
        assert second["status"] == "running"


class TestStepStartedUiMode:
    @pytest.mark.asyncio
    async def test_step_started_includes_ui_mode(self, tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "select.md").write_text("Select.", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              result: []
            max_rollbacks: 1
            steps:
              - id: select
                conclusion_field: result
                forward: null
                prompt: prompts/select.md
                ui_mode: candidate_selection
                auto_advance: false
        """),
            encoding="utf-8",
        )

        storage = FakeSessionStorage()
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=storage,
            session_id="test123",
        )

        events = []
        async for event in runner.run("hello"):
            events.append(event)
            if isinstance(event, PipelineEvent) and event.type == PipelineEventType.STEP_STARTED:
                break

        step_started = [e for e in events if isinstance(e, PipelineEvent) and e.type == PipelineEventType.STEP_STARTED]
        assert len(step_started) == 1
        assert step_started[0].data["ui_mode"] == "candidate_selection"

    @pytest.mark.asyncio
    async def test_step_started_ui_mode_none_by_default(self, tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "step.md").write_text("Step.", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              result: []
            max_rollbacks: 1
            steps:
              - id: step
                conclusion_field: result
                forward: null
                prompt: prompts/step.md
        """),
            encoding="utf-8",
        )

        storage = FakeSessionStorage()
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=storage,
            session_id="test123",
        )

        events = []
        async for event in runner.run("hello"):
            events.append(event)
            if isinstance(event, PipelineEvent) and event.type == PipelineEventType.STEP_STARTED:
                break

        step_started = [e for e in events if isinstance(e, PipelineEvent) and e.type == PipelineEventType.STEP_STARTED]
        assert len(step_started) == 1
        assert step_started[0].data["ui_mode"] is None


class TestApplyHardInterruptClearsPendingRestarts:
    def test_parent_rollback_clears_pending_restarts(self, tmp_path):
        """I2: _pending_candidate_restarts must be cleared on parent-level rollback."""
        from iac_code.pipeline.engine.interrupt import InterruptVerdict

        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "arch.md").write_text("Arch", encoding="utf-8")
        (tmp_path / "prompts" / "eval.md").write_text("Eval", encoding="utf-8")
        (tmp_path / "prompts" / "template.md").write_text("T", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              architecture: []
              evaluated: [architecture]
            max_rollbacks: 3
            sub_pipelines:
              evaluate_candidate:
                max_rollbacks: 2
                iterate_over: architecture.candidates
                context_fields_from_parent: []
                steps:
                  - id: template_gen
                    conclusion_field: template
                    forward: null
                    prompt: prompts/template.md
                    context_fields: [candidate]
            steps:
              - id: arch
                conclusion_field: architecture
                forward: eval
                prompt: prompts/arch.md
              - id: eval
                type: parallel_sub_pipeline
                sub_pipeline: evaluate_candidate
                conclusion_field: evaluated
                forward: null
                prompt: prompts/eval.md
        """),
            encoding="utf-8",
        )

        storage = FakeSessionStorage()
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=storage,
            session_id="test123",
        )
        runner.state_machine.advance()

        runner._pending_candidate_restarts[0] = {
            "start_from_step": "template_gen",
            "preserved_conclusions": {},
            "rollback_context": None,
        }

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="user wants cheaper",
            rollback_target="arch",
        )
        result = runner.apply_hard_interrupt(verdict)

        assert result is True
        assert len(runner._pending_candidate_restarts) == 0


class TestApplyHardInterruptAllScopeEscalation:
    """I4: scope='all' with partial completion must escalate to parent rollback,
    not silently leave completed candidates with stale conclusions."""

    @staticmethod
    def _build_runner(tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "arch.md").write_text("Arch", encoding="utf-8")
        (tmp_path / "prompts" / "eval.md").write_text("Eval", encoding="utf-8")
        (tmp_path / "prompts" / "template.md").write_text("T", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              architecture: []
              evaluated: [architecture]
            max_rollbacks: 3
            sub_pipelines:
              evaluate_candidate:
                max_rollbacks: 2
                iterate_over: architecture.candidates
                context_fields_from_parent: []
                steps:
                  - id: template_gen
                    conclusion_field: template
                    forward: null
                    prompt: prompts/template.md
                    context_fields: [candidate]
            steps:
              - id: arch
                conclusion_field: architecture
                forward: eval
                prompt: prompts/arch.md
              - id: eval
                type: parallel_sub_pipeline
                sub_pipeline: evaluate_candidate
                conclusion_field: evaluated
                forward: null
                prompt: prompts/eval.md
        """),
            encoding="utf-8",
        )
        storage = FakeSessionStorage()
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=storage,
            session_id="test123",
        )
        runner.state_machine.advance()  # position at "eval"
        return runner

    def test_all_scope_with_partial_completion_escalates(self, tmp_path):
        from iac_code.pipeline.engine.interrupt import InterruptVerdict

        runner = self._build_runner(tmp_path)
        runner._parallel_candidates_total = 3
        runner._active_candidates[1] = {"task": MagicMock(done=lambda: True)}
        # Candidates 0 and 2 already finished (not in _active_candidates).

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="redo all with cheaper option",
            rollback_target="template_gen",
            candidate_scope="all",
        )
        result = runner.apply_hard_interrupt(verdict)

        assert result is True, "Should escalate to parent rollback"
        assert runner.state_machine.current_step.step_id == "eval"

    def test_all_scope_with_full_active_uses_candidate_restart(self, tmp_path):
        from iac_code.pipeline.engine.interrupt import InterruptVerdict

        runner = self._build_runner(tmp_path)
        runner._parallel_candidates_total = 2
        runner._active_candidates[0] = {
            "task": MagicMock(done=lambda: True),
            "current_sub_step": "template_gen",
            "conclusions": {},
        }
        runner._active_candidates[1] = {
            "task": MagicMock(done=lambda: True),
            "current_sub_step": "template_gen",
            "conclusions": {},
        }

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="all candidates need rework",
            rollback_target="template_gen",
            candidate_scope="all",
        )
        result = runner.apply_hard_interrupt(verdict)

        assert result is False, "All still-running candidates → candidate-level restart"
        assert 0 in runner._pending_candidate_restarts
        assert 1 in runner._pending_candidate_restarts

    def test_candidate_n_completed_escalates(self, tmp_path):
        """scope='candidate:1' where candidate 1 already finished → parent rollback."""
        from iac_code.pipeline.engine.interrupt import InterruptVerdict

        runner = self._build_runner(tmp_path)
        runner._parallel_candidates_total = 2
        runner._active_candidates[0] = {"task": MagicMock(done=lambda: True), "conclusions": {}}
        # Candidate 1 already finished (not in _active_candidates).

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="fix candidate 1",
            rollback_target="template_gen",
            candidate_scope="candidate:1",
        )
        result = runner.apply_hard_interrupt(verdict)

        assert result is True, "Requested candidate already completed → escalate to parent"
        assert runner.state_machine.current_step.step_id == "eval"

    def test_candidate_n_active_uses_candidate_restart(self, tmp_path):
        """scope='candidate:0' where candidate 0 still running → candidate restart."""
        from iac_code.pipeline.engine.interrupt import InterruptVerdict

        runner = self._build_runner(tmp_path)
        runner._parallel_candidates_total = 2
        runner._active_candidates[0] = {
            "task": MagicMock(done=lambda: True),
            "current_sub_step": "template_gen",
            "conclusions": {},
        }
        runner._active_candidates[1] = {
            "task": MagicMock(done=lambda: True),
            "current_sub_step": "template_gen",
            "conclusions": {},
        }

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="rework candidate 0",
            rollback_target="template_gen",
            candidate_scope="candidate:0",
        )
        result = runner.apply_hard_interrupt(verdict)

        assert result is False, "Requested candidate still active → candidate-level restart"
        assert 0 in runner._pending_candidate_restarts
        assert 1 not in runner._pending_candidate_restarts


class TestParallelCleanupOnClose:
    """Ctrl+C tears down the REPL by aclose()ing the pipeline stream. The parallel
    step's generator must, on GeneratorExit, cancel every candidate task and reset
    _active_candidates / _parallel_candidates_total — no orphaned tasks or stale state."""

    @staticmethod
    def _build_runner(tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "arch.md").write_text("Arch", encoding="utf-8")
        (tmp_path / "prompts" / "eval.md").write_text("Eval", encoding="utf-8")
        (tmp_path / "prompts" / "template.md").write_text("T", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              architecture: []
              evaluated: [architecture]
            max_rollbacks: 3
            sub_pipelines:
              evaluate_candidate:
                max_rollbacks: 2
                iterate_over: architecture.candidates
                context_fields_from_parent: []
                steps:
                  - id: template_gen
                    conclusion_field: template
                    forward: null
                    prompt: prompts/template.md
                    context_fields: [candidate]
            steps:
              - id: arch
                conclusion_field: architecture
                forward: eval
                prompt: prompts/arch.md
              - id: eval
                type: parallel_sub_pipeline
                sub_pipeline: evaluate_candidate
                conclusion_field: evaluated
                forward: null
                prompt: prompts/eval.md
        """),
            encoding="utf-8",
        )
        storage = FakeSessionStorage()
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=storage,
            session_id="test123",
        )
        runner.context.set_conclusion("architecture", {"candidates": [{"name": "Plan A"}, {"name": "Plan B"}]})
        runner.state_machine.advance()  # position at "eval"
        return runner

    @pytest.mark.asyncio
    async def test_aclose_cancels_candidates_and_resets_state(self, tmp_path, caplog):
        caplog.set_level(logging.INFO, logger="iac_code.pipeline.engine.pipeline_runner")
        runner = self._build_runner(tmp_path)
        runner._observability.candidate_cancelled = MagicMock()
        released = asyncio.Event()  # never set → candidates hang after first event

        async def hanging_execute_streaming(
            sub_spec,
            candidate,
            candidate_index,
            parent_context,
            session_id,
            *,
            start_from_step=None,
            preserved_conclusions=None,
            user_message=None,
            parent_step_id=None,
            **kwargs,
        ):
            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_STARTED,
                step_id=None,
                timestamp=0,
                data={
                    "sub_pipeline_id": f"eval_{candidate_index}",
                    "candidate_index": candidate_index,
                    "candidate_name": candidate["name"],
                    "total_steps": 1,
                    "sub_pipeline_name": "evaluate_candidate",
                },
            )
            await released.wait()  # block forever until cancelled
            yield  # pragma: no cover

        with patch("iac_code.pipeline.engine.pipeline_runner.SubPipelineExecutor") as mock_sub_exec:
            instance = MagicMock()
            instance.execute_streaming = hanging_execute_streaming
            instance.current_step_executor_agent_loop = None
            mock_sub_exec.return_value = instance

            step = next(s for s in runner._loaded.steps if s.step_id == "eval")
            gen = runner._execute_parallel_sub_pipeline(step)

            # Pull both SUB_PIPELINE_STARTED events so both candidates are active.
            await gen.__anext__()
            await gen.__anext__()
            assert runner._parallel_candidates_total == 2
            assert len(runner._active_candidates) == 2

            # Simulate Ctrl+C teardown: close the generator mid-flight.
            await gen.aclose()

        assert runner._active_candidates == {}
        assert runner._parallel_candidates_total == 0
        assert runner._observability.candidate_cancelled.call_args_list == [
            call(
                parent_step_id="eval",
                candidate_index=0,
                candidate_name="Plan A",
                reason="parallel_cleanup",
            ),
            call(
                parent_step_id="eval",
                candidate_index=1,
                candidate_name="Plan B",
                reason="parallel_cleanup",
            ),
        ]
        log_records = [
            record for record in caplog.records if record.message.startswith("Pipeline candidate cancelled:")
        ]
        assert len(log_records) == 2
        assert {record.candidate_index for record in log_records} == {0, 1}
        assert {record.parent_step_id for record in log_records} == {"eval"}
        assert {record.reason for record in log_records} == {"parallel_cleanup"}


class TestClearSidecar:
    """clear_sidecar() is a compatibility API that must preserve sidecar data."""

    @staticmethod
    def _build_runner_with_real_session_path(tmp_path):
        """FakeSessionStorage that returns a real Path so PipelineSession lands on disk."""
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "a.md").write_text("A", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              a_out: []
            max_rollbacks: 3
            steps:
              - id: a
                conclusion_field: a_out
                forward: null
                prompt: prompts/a.md
        """),
            encoding="utf-8",
        )

        sessions_root = tmp_path / "sessions"
        sessions_root.mkdir()

        class RealPathStorage(FakeSessionStorage):
            def session_path(self, cwd, session_id):
                return sessions_root / session_id / "session.jsonl"

            def session_dir(self, cwd, session_id):
                return sessions_root / session_id

        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=RealPathStorage(),
            session_id="abc123",
        )
        # Sidecar now nests under the session directory (问题 4).
        sidecar = sessions_root / "abc123" / "pipeline"
        return runner, sidecar

    @pytest.mark.asyncio
    async def test_clear_sidecar_marks_discarded_without_removing_snapshot(self, tmp_path):
        runner, sidecar = self._build_runner_with_real_session_path(tmp_path)
        assert runner.session is not None
        await runner.session.save_step_completion(
            step_id="a",
            state_machine_snapshot=runner.state_machine.to_snapshot(),
            context_snapshot=runner.context.to_snapshot(),
        )
        assert sidecar.is_dir()
        assert runner.session.exists()

        runner.clear_sidecar()

        assert sidecar.is_dir()
        assert runner.session.exists()
        meta = yaml.safe_load((sidecar / "meta.yaml").read_text(encoding="utf-8"))
        assert meta["status"] == "discarded"
        assert meta["resume_policy"] == "none"
        assert meta["terminal"] is True

    def test_clear_sidecar_when_no_session_is_noop(self, tmp_path):
        """If session is None (storage has no session_dir method), clear_sidecar must not raise."""
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "a.md").write_text("A", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              a_out: []
            max_rollbacks: 3
            steps:
              - id: a
                conclusion_field: a_out
                forward: null
                prompt: prompts/a.md
        """),
            encoding="utf-8",
        )

        class NoParentStorage:
            def append_meta(self, *a, **k):
                pass

            def session_path(self, cwd, session_id):
                return "not-a-path-object"  # no .parent attribute → session = None

        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=NoParentStorage(),
            session_id="abc123",
        )
        assert runner.session is None
        runner.clear_sidecar()  # must not raise

    @pytest.mark.asyncio
    async def test_clear_sidecar_when_never_saved_is_noop(self, tmp_path):
        runner, sidecar = self._build_runner_with_real_session_path(tmp_path)
        assert not sidecar.exists()
        runner.clear_sidecar()
        assert not sidecar.exists()

    def test_clear_sidecar_emits_sidecar_failed_when_discard_fails(self, tmp_path):
        runner, _sidecar = self._build_runner_with_real_session_path(tmp_path)
        assert runner.session is not None
        runner.session.session_dir.mkdir(parents=True)
        runner.session.mark_discarded = MagicMock(side_effect=OSError("locked"))
        runner._sidecar_status = "running"
        runner._observability.sidecar_failed = MagicMock()

        runner.clear_sidecar()

        runner._observability.sidecar_failed.assert_called_once_with(
            operation="mark_discarded",
            status="running",
            error_type="OSError",
            error_summary="locked",
            error_id=ANY,
        )


class TestExitCondition:
    @staticmethod
    def _write_pipeline_with_exit_condition(tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "step1.md").write_text("Step1.", encoding="utf-8")
        (tmp_path / "prompts" / "step2.md").write_text("Step2.", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              intent: []
              arch: [intent]
            max_rollbacks: 1
            steps:
              - id: intent_parsing
                conclusion_field: intent
                forward: arch_planning
                prompt: prompts/step1.md
                exit_condition:
                  field: is_infra_intent
                  value: false
              - id: arch_planning
                conclusion_field: arch
                forward: null
                prompt: prompts/step2.md
        """),
            encoding="utf-8",
        )

    @pytest.mark.asyncio
    async def test_exit_condition_triggers_early_exit(self, tmp_path):
        from iac_code.pipeline.engine.types import StepResult, StepStatus

        self._write_pipeline_with_exit_condition(tmp_path)
        storage = FakeSessionStorage()
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=storage,
            session_id="test123",
        )

        async def mock_execute(step, context, session_id, *, user_message=None, **kwargs):
            conclusion = {"is_infra_intent": False, "rejection_reason": "not infra"}
            context.set_conclusion(step.conclusion_field, conclusion)
            yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

        runner._step_executor.execute = mock_execute

        events = []
        async for event in runner.run("帮我写个Python脚本"):
            events.append(event)

        completed = [
            e for e in events if isinstance(e, PipelineEvent) and e.type == PipelineEventType.PIPELINE_COMPLETED
        ]
        assert len(completed) == 1
        assert completed[0].data.get("early_exit") is True

        started = [e for e in events if isinstance(e, PipelineEvent) and e.type == PipelineEventType.STEP_STARTED]
        assert len(started) == 1
        assert started[0].data["name"] == "intent_parsing"

    @pytest.mark.asyncio
    async def test_no_exit_when_condition_not_met(self, tmp_path):
        from iac_code.pipeline.engine.types import StepResult, StepStatus

        self._write_pipeline_with_exit_condition(tmp_path)
        storage = FakeSessionStorage()
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=storage,
            session_id="test123",
        )

        call_count = 0

        async def mock_execute(step, context, session_id, *, user_message=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if step.step_id == "intent_parsing":
                conclusion = {"is_infra_intent": True, "business_type": "web"}
            else:
                conclusion = {"design": "basic"}
            context.set_conclusion(step.conclusion_field, conclusion)
            yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

        runner._step_executor.execute = mock_execute

        events = []
        async for event in runner.run("我要部署一个网站"):
            events.append(event)

        assert call_count == 2

        completed = [
            e for e in events if isinstance(e, PipelineEvent) and e.type == PipelineEventType.PIPELINE_COMPLETED
        ]
        assert len(completed) == 1
        assert completed[0].data.get("early_exit") is None


class TestInvalidRollbackTarget:
    """Regression: hallucinated rollback target must not crash the stream (P-C3)."""

    @pytest.mark.asyncio
    async def test_parent_step_can_rollback_to_any_completed_non_future_step(self, tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        for step_id in ("a", "b", "c"):
            (tmp_path / "prompts" / f"{step_id}.md").write_text(f"do {step_id}", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              a_out: []
              b_out: [a_out]
              c_out: [b_out]
            max_rollbacks: 3
            steps:
              - id: a
                conclusion_field: a_out
                forward: b
                prompt: prompts/a.md
              - id: b
                conclusion_field: b_out
                forward: c
                prompt: prompts/b.md
              - id: c
                conclusion_field: c_out
                forward: null
                prompt: prompts/c.md
            """),
            encoding="utf-8",
        )
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=FakeSessionStorage(),
            session_id="test",
            cwd=str(tmp_path),
        )
        seen_steps: list[str] = []
        seen_targets_by_step: dict[str, list[str]] = {}

        async def fake_execute(step, context, session_id, user_message=None, **kwargs):
            seen_steps.append(step.step_id)
            seen_targets_by_step[step.step_id] = kwargs.get("rollback_targets") or []
            conclusion = {step.conclusion_field: step.step_id}
            context.set_conclusion(step.conclusion_field, conclusion)
            rollback_request = ("b", "revise b") if step.step_id == "c" and seen_steps.count("c") == 1 else None
            yield StepResult(
                step_id=step.step_id,
                status=StepStatus.COMPLETED,
                conclusion=conclusion,
                rollback_request=rollback_request,
            )

        runner._step_executor.execute = fake_execute

        events = []
        async for event in runner._continue_from_current():
            events.append(event)
            if len(events) > 30:
                break

        failed = [
            event
            for event in events
            if isinstance(event, PipelineEvent) and event.type == PipelineEventType.STEP_FAILED
        ]
        completed = [
            event
            for event in events
            if isinstance(event, PipelineEvent) and event.type == PipelineEventType.PIPELINE_COMPLETED
        ]
        assert failed == []
        assert len(completed) == 1
        assert completed[0].data.get("failed") is not True
        assert seen_steps == ["a", "b", "c", "b", "c"]
        assert seen_targets_by_step["c"] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_failed_step_event_uses_public_error_payload(self, tmp_path):
        runner = _build_two_step_runner(tmp_path)
        runner._observability.step_failed = MagicMock()

        async def fake_execute(step, context, session_id, user_message=None, **kwargs):
            yield StepResult(
                step_id=step.step_id,
                status=StepStatus.FAILED,
                error="provider failed Cookie=session-secret /tmp/iac-code/config.yml",
            )

        runner._step_executor.execute = fake_execute

        events = [event async for event in runner._continue_from_current()]

        failed = [
            event
            for event in events
            if isinstance(event, PipelineEvent) and event.type == PipelineEventType.STEP_FAILED
        ]
        assert len(failed) == 1
        payload = failed[0].data
        rendered = str(payload)
        assert "session-secret" not in rendered
        assert "/tmp/iac-code" not in rendered
        assert payload["error"] == "provider failed [REDACTED]"
        assert payload["error_summary"] == payload["error"]
        assert runner._observability.step_failed.call_args.kwargs["error_id"]
        assert payload["error_details"]["type"] == "StepFailed"
        assert payload["error_details"]["step_id"] == "a"

    @pytest.mark.asyncio
    async def test_empty_parallel_candidates_emit_failed_events_without_escaping(self, tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "architecture.md").write_text("architecture", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              architecture: []
              evaluations: [architecture]
            steps:
              - id: architecture_planning
                conclusion_field: architecture
                forward: evaluate_candidates
                prompt: prompts/architecture.md
              - id: evaluate_candidates
                type: parallel_sub_pipeline
                conclusion_field: evaluations
                forward: null
                sub_pipeline: evaluate_candidate
            sub_pipelines:
              evaluate_candidate:
                iterate_over: architecture.candidates
                steps:
                  - id: template_generating
                    conclusion_field: template
                    prompt: prompts/architecture.md
            """),
            encoding="utf-8",
        )
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=FakeSessionStorage(),
            session_id="test",
            cwd=str(tmp_path),
        )
        runner._observability.step_failed = MagicMock()

        async def fake_execute(step, context, session_id, user_message=None, **kwargs):
            conclusion = {"candidates": []}
            context.set_conclusion(step.conclusion_field, conclusion)
            yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

        runner._step_executor.execute = fake_execute

        events = [event async for event in runner._continue_from_current()]

        failed = [e for e in events if isinstance(e, PipelineEvent) and e.type == PipelineEventType.STEP_FAILED]
        completed = [
            e for e in events if isinstance(e, PipelineEvent) and e.type == PipelineEventType.PIPELINE_COMPLETED
        ]
        assert len(failed) == 1
        assert failed[0].step_id == "evaluate_candidates"
        assert "architecture.candidates" in failed[0].data["error"]
        assert "empty" in failed[0].data["error"]
        assert len(completed) == 1
        assert completed[0].data.get("failed") is True
        runner._observability.step_failed.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_rollback_target_emits_step_failed(self, tmp_path):
        from textwrap import dedent
        from unittest.mock import MagicMock

        from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
        from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
        from iac_code.pipeline.engine.types import StepResult, StepStatus

        # Build a minimal 2-step pipeline
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "a.md").write_text("do A", encoding="utf-8")
        (tmp_path / "prompts" / "b.md").write_text("do B", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              a_out: []
              b_out: [a_out]
            max_rollbacks: 3
            steps:
              - id: a
                conclusion_field: a_out
                forward: b
                prompt: prompts/a.md
                description: Step A
              - id: b
                conclusion_field: b_out
                forward: null
                prompt: prompts/b.md
                description: Step B
        """),
            encoding="utf-8",
        )
        storage = FakeSessionStorage()
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=storage,
            session_id="test",
            cwd=str(tmp_path),
        )

        # Mock _step_executor.execute to yield a StepResult with a hallucinated
        # rollback target that doesn't exist in the state machine.
        async def fake_execute(step, ctx, sid, user_message=None, **kwargs):
            yield StepResult(
                step_id=step.step_id,
                status=StepStatus.COMPLETED,
                conclusion={"a_out": "x"},
                rollback_request=("nonexistent_step", "user said so"),
            )

        runner._step_executor.execute = fake_execute
        runner._observability.step_completed = MagicMock()
        runner._observability.step_failed = MagicMock()

        events = []
        async for event in runner._continue_from_current():
            events.append(event)
            if len(events) > 20:  # safety cap to detect infinite loops
                break

        # Critical: no ValueError escaped — we got a clean STEP_FAILED event
        # AND a terminal PIPELINE_COMPLETED(failed=True) so consumers can
        # tear down cleanly.
        failed = [e for e in events if isinstance(e, PipelineEvent) and e.type == PipelineEventType.STEP_FAILED]
        assert len(failed) == 1, f"Expected exactly one STEP_FAILED, got: {events}"
        error_msg = failed[0].data.get("error", "")
        assert "nonexistent_step" in error_msg
        assert "Valid targets:" in error_msg

        completed = [
            e for e in events if isinstance(e, PipelineEvent) and e.type == PipelineEventType.PIPELINE_COMPLETED
        ]
        assert len(completed) == 1, f"Expected one PIPELINE_COMPLETED, got: {events}"
        assert completed[0].data.get("failed") is True

        # Order: STEP_FAILED comes before PIPELINE_COMPLETED
        step_failed_idx = events.index(failed[0])
        pipe_completed_idx = events.index(completed[0])
        assert step_failed_idx < pipe_completed_idx
        runner._observability.step_completed.assert_not_called()
        runner._observability.step_failed.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_rollback_target_step_failed_redacts_public_payload(self, tmp_path):
        runner = _build_two_step_runner(tmp_path)
        runner._observability.step_completed = MagicMock()
        runner._observability.step_failed = MagicMock()
        raw_target = "Authorization: ACS3-HMAC-SHA256 Credential=LTAIabc123456789,Signature=secret-one"

        async def fake_execute(step, ctx, sid, user_message=None, **kwargs):
            yield StepResult(
                step_id=step.step_id,
                status=StepStatus.COMPLETED,
                conclusion={"a_out": "x"},
                rollback_request=(raw_target, "user said so at /Users/alice/.iac-code/settings.yml"),
            )

        runner._step_executor.execute = fake_execute

        events = [event async for event in runner._continue_from_current()]

        failed = [e for e in events if isinstance(e, PipelineEvent) and e.type == PipelineEventType.STEP_FAILED]
        assert len(failed) == 1
        payload = failed[0].data
        rendered = str(payload)
        assert "secret-one" not in rendered
        assert "LTAIabc123456789" not in rendered
        assert "/Users/alice" not in rendered
        assert payload["error"] == payload["error_summary"]
        assert payload["error_details"]["type"] == "StepFailed"
        assert payload["error_details"]["step_id"] == "a"
        assert "secret-one" not in runner._observability.step_failed.call_args.kwargs["error_summary"]
        assert runner._observability.step_failed.call_args.kwargs["error_id"]

    @pytest.mark.asyncio
    async def test_invalid_rollback_target_saves_failed_attempt_before_yielding_step_failed(self, tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "a.md").write_text("do A", encoding="utf-8")
        (tmp_path / "prompts" / "b.md").write_text("do B", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              a_out: []
              b_out: [a_out]
            max_rollbacks: 3
            steps:
              - id: a
                conclusion_field: a_out
                forward: b
                prompt: prompts/a.md
              - id: b
                conclusion_field: b_out
                forward: null
                prompt: prompts/b.md
        """),
            encoding="utf-8",
        )
        storage = DirectorySessionStorage(tmp_path / "projects")
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=storage,
            session_id="test",
            cwd=str(tmp_path),
        )

        async def fake_execute(step, ctx, sid, user_message=None, **kwargs):
            yield StepResult(
                step_id=step.step_id,
                status=StepStatus.COMPLETED,
                conclusion={"a_out": "x"},
                rollback_request=("missing", "user said so"),
            )

        runner._step_executor.execute = fake_execute

        async for event in runner._continue_from_current():
            if isinstance(event, PipelineEvent) and event.type == PipelineEventType.STEP_FAILED:
                break

        meta_path = storage.session_dir(str(tmp_path), "test") / "pipeline" / "meta.yaml"
        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        assert meta["status"] == "failed"
        assert meta["attempts"]["items"]["att_0001"]["status"] == "failed"
        assert "execution" not in meta


class TestParallelPartialOutputAggregation:
    """P-I12: during parallel_sub_pipeline, judge state must aggregate
    partial_output from each running candidate, not return empty."""

    def test_parallel_step_aggregates_candidate_partial_outputs(self):
        """When current_step is parallel_sub_pipeline, _get_state_for_judge
        must include each candidate's current_turn_text in partial_output."""
        from unittest.mock import MagicMock

        from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

        # Build a runner bypassing full __init__.
        runner = PipelineRunner.__new__(PipelineRunner)

        # Parent has no agent_loop (parallel mode).
        runner._step_executor = MagicMock()
        runner._step_executor.current_agent_loop = None

        # current_step is a parallel_sub_pipeline step.
        parallel_step = MagicMock(step_id="evaluate_candidates", step_type="parallel_sub_pipeline")
        runner.state_machine = MagicMock()
        runner.state_machine.current_step = parallel_step
        runner.state_machine.history = []

        # Two active candidates with text being streamed.
        cand0_loop = MagicMock(current_turn_text="方案A 草稿: 用 ECS 单机")
        cand1_loop = MagicMock(current_turn_text="方案B 草稿: 用 ECS + SLB 双机")
        runner._active_candidates = {
            0: {"agent_loop": cand0_loop, "task": MagicMock()},
            1: {"agent_loop": cand1_loop, "task": MagicMock()},
        }
        runner.context = MagicMock()
        runner.context.snapshot = MagicMock(return_value={})

        # Other attrs _get_state_for_judge may touch — stub minimally.
        runner._loaded = MagicMock()
        runner._loaded.steps = []

        state = runner._get_state_for_judge()

        partial = state.get("partial_output", "")
        assert "方案A 草稿: 用 ECS 单机" in partial, f"candidate 0 partial missing from aggregated output: {partial!r}"
        assert "方案B 草稿: 用 ECS + SLB 双机" in partial, (
            f"candidate 1 partial missing from aggregated output: {partial!r}"
        )
        # Should have some indication of which candidate's text is which.
        assert "Candidate" in partial or "候选" in partial or "[" in partial, (
            f"no candidate labels in aggregated output: {partial!r}"
        )

    def test_non_parallel_step_uses_step_executor_agent_loop(self):
        """When current_step is NOT parallel, partial_output comes from
        self._step_executor.current_agent_loop (existing behavior preserved)."""
        from unittest.mock import MagicMock

        from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

        runner = PipelineRunner.__new__(PipelineRunner)

        runner._step_executor = MagicMock()
        runner._step_executor.current_agent_loop = MagicMock(current_turn_text="single agent loop text")

        normal_step = MagicMock(step_id="single_step", step_type="agent_loop")
        runner.state_machine = MagicMock()
        runner.state_machine.current_step = normal_step
        runner.state_machine.history = []
        runner._active_candidates = {}
        runner.context = MagicMock()
        runner.context.snapshot = MagicMock(return_value={})
        runner._loaded = MagicMock()
        runner._loaded.steps = []

        state = runner._get_state_for_judge()
        assert state.get("partial_output") == "single agent loop text", (
            f"non-parallel step should use step_executor.current_agent_loop, got: {state.get('partial_output')!r}"
        )

    def test_parallel_step_with_empty_loops_returns_empty(self):
        """Parallel step with candidate loops that have no text — partial is empty (or just labels)."""
        from unittest.mock import MagicMock

        from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

        runner = PipelineRunner.__new__(PipelineRunner)
        runner._step_executor = MagicMock()
        runner._step_executor.current_agent_loop = None

        parallel_step = MagicMock(step_id="evaluate_candidates", step_type="parallel_sub_pipeline")
        runner.state_machine = MagicMock()
        runner.state_machine.current_step = parallel_step
        runner.state_machine.history = []

        # Loops exist but no text yet.
        cand0_loop = MagicMock(current_turn_text="")
        runner._active_candidates = {0: {"agent_loop": cand0_loop, "task": MagicMock()}}
        runner.context = MagicMock()
        runner.context.snapshot = MagicMock(return_value={})
        runner._loaded = MagicMock()
        runner._loaded.steps = []

        state = runner._get_state_for_judge()
        partial = state.get("partial_output", "")
        # Acceptable: empty string, OR a label-only output. Don't be too strict.
        assert "方案A" not in partial  # no fake content


class TestResolveIterateField:
    """P-I16: missing field or empty list must raise ValueError (no silent zero-iterate)."""

    def test_missing_field_raises(self):
        from unittest.mock import MagicMock

        from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

        runner = PipelineRunner.__new__(PipelineRunner)
        runner.context = MagicMock()
        runner.context.snapshot = MagicMock(return_value={"intent": {}, "other": []})
        with pytest.raises(ValueError, match="not present in context"):
            runner._resolve_iterate_field("candidates")

    def test_empty_list_raises(self):
        from unittest.mock import MagicMock

        from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

        runner = PipelineRunner.__new__(PipelineRunner)
        runner.context = MagicMock()
        runner.context.snapshot = MagicMock(return_value={"candidates": []})
        with pytest.raises(ValueError, match="is empty"):
            runner._resolve_iterate_field("candidates")

    def test_non_list_raises(self):
        from unittest.mock import MagicMock

        from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

        runner = PipelineRunner.__new__(PipelineRunner)
        runner.context = MagicMock()
        runner.context.snapshot = MagicMock(return_value={"candidates": "not a list"})
        with pytest.raises(ValueError, match="is not a list"):
            runner._resolve_iterate_field("candidates")

    def test_valid_list_returns(self):
        from unittest.mock import MagicMock

        from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

        runner = PipelineRunner.__new__(PipelineRunner)
        runner.context = MagicMock()
        runner.context.snapshot = MagicMock(return_value={"candidates": [{"name": "A"}, {"name": "B"}]})
        result = runner._resolve_iterate_field("candidates")
        assert len(result) == 2
        assert result[0]["name"] == "A"

    def test_nested_path_supported(self):
        from unittest.mock import MagicMock

        from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

        runner = PipelineRunner.__new__(PipelineRunner)
        runner.context = MagicMock()
        runner.context.snapshot = MagicMock(return_value={"plan": {"options": [{"x": 1}]}})
        result = runner._resolve_iterate_field("plan.options")
        assert result == [{"x": 1}]
