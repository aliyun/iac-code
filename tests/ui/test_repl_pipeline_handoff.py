"""Pipeline terminal event handoff behavior in InlineREPL."""

from __future__ import annotations

import asyncio
import time
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import pytest
import yaml
from rich.console import Console

from iac_code.agent.message import Message
from iac_code.pipeline.config import RunMode
from iac_code.pipeline.engine.cleanup import (
    CLEANUP_PROMPT_METADATA_TYPE,
    CleanupLedger,
    CleanupResource,
    ObservedResource,
    create_cleanup_prompt_message,
)
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.user_input import PipelineUserInput
from iac_code.types.stream_events import StackProgressEvent, ToolResultEvent, ToolUseEndEvent


async def _empty_stream():
    return
    yield  # noqa: B901


def _pipeline_completed_event(**data) -> PipelineEvent:
    return PipelineEvent(
        type=PipelineEventType.PIPELINE_COMPLETED,
        step_id=None,
        timestamp=time.time(),
        data={"total_steps": 2, **data},
    )


def _pipeline_persistence_failure_event(step_id: str) -> PipelineEvent:
    return PipelineEvent(
        type=PipelineEventType.STEP_FAILED,
        step_id=step_id,
        timestamp=time.time(),
        data={
            "error": "Pipeline state persistence failed.",
            "error_details": {"type": "PipelineStatePersistenceError"},
        },
    )


def _make_repl_for_handoff(
    terminal_event: PipelineEvent,
    *,
    should_switch_to_normal: bool,
    summary: str = "handoff summary",
    is_complete: bool = True,
    sidecar_status: str | None = None,
):
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.PIPELINE
    repl._pipeline_waiting_input = False
    repl._original_cwd = "/workspace"
    repl._session_id = "session-1"
    repl.store = MagicMock()
    repl.renderer = MagicMock()
    repl.renderer.record_user_turn = MagicMock()
    repl.renderer.print_system_message = MagicMock()
    repl._render_pipeline_stream = AsyncMock(return_value=terminal_event)
    repl.current_git_branch = MagicMock(return_value="main")
    repl._session_storage = MagicMock()

    injected = Message(role="user", content=summary)
    repl._agent_loop = MagicMock()
    repl._agent_loop.context_manager = MagicMock()
    repl._agent_loop.context_manager.add_raw_message = MagicMock(return_value=injected)

    pipeline = MagicMock()
    pipeline.resume = MagicMock(return_value=_empty_stream())
    pipeline.run = MagicMock(return_value=_empty_stream())
    pipeline.clear_sidecar = MagicMock()
    pipeline.mark_normal_handoff = MagicMock()
    pipeline.mark_user_aborted = MagicMock()
    pipeline.sidecar_status = sidecar_status
    pipeline.state_machine = MagicMock()
    pipeline.state_machine.is_complete = is_complete
    pipeline.should_switch_to_normal = MagicMock(return_value=should_switch_to_normal)
    pipeline.build_normal_handoff_summary = MagicMock(return_value=summary)
    repl._pipeline = pipeline
    return repl, pipeline, injected


def test_pipeline_runner_mark_normal_handoff_saves_terminal_metadata(tmp_path: Path):
    from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
    from iac_code.pipeline.engine.session import PipelineIdentity, PipelineSession

    runner = PipelineRunner.__new__(PipelineRunner)
    runner.session = PipelineSession(tmp_path / "session" / "pipeline")
    runner._sidecar_status = None
    runner._session_id = "root-session"
    runner._pipeline_identity = PipelineIdentity(
        pipeline_name="support",
        step_ids=["collect"],
        sub_pipeline_step_ids={},
        pipeline_fingerprint="fingerprint",
    )
    runner._execution = {"kind": "step", "active_attempt_id": "att_0001"}
    runner._attempts = {"next_attempt_number": 2, "items": {"att_0001": {"status": "completed"}}}
    runner._observability = MagicMock()

    runner.state_machine = MagicMock()
    runner.state_machine.is_complete = True
    runner.state_machine._order = ["collect"]
    runner.state_machine.to_snapshot.return_value = {
        "current_index": 1,
        "rollback_count": 0,
        "interrupt_rollback_count": 0,
        "step_statuses": {"collect": "completed"},
    }
    runner.context = MagicMock()
    runner.context.to_snapshot.return_value = {"collected": {"value": "yes"}}

    runner.mark_normal_handoff(status="succeeded", failed_reason=None)

    meta = yaml.safe_load(runner.session.meta_path.read_text(encoding="utf-8"))
    context = yaml.safe_load(runner.session.context_path.read_text(encoding="utf-8"))
    assert runner.session.session_dir.exists()
    assert meta["status"] == "completed"
    assert meta["resume_policy"] == "none"
    assert meta["terminal"] is True
    assert meta["current_step"] == "collect"
    assert meta["execution"] == {"kind": "step", "active_attempt_id": "att_0001"}
    assert meta["attempts"] == {"next_attempt_number": 2, "items": {"att_0001": {"status": "completed"}}}
    assert meta["normal_handoff"] == {
        "status": "succeeded",
        "switched_to_normal": True,
        "root_session_id": "root-session",
        "summary_message_appended": True,
        "failed_reason": None,
    }
    assert context == {"collected": {"value": "yes"}}


def test_pipeline_runner_mark_normal_handoff_preserves_existing_execution_when_empty(tmp_path: Path):
    from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
    from iac_code.pipeline.engine.session import PipelineIdentity, PipelineSession

    identity = PipelineIdentity(
        pipeline_name="support",
        step_ids=["collect"],
        sub_pipeline_step_ids={},
        pipeline_fingerprint="fingerprint",
    )
    session = PipelineSession(tmp_path / "session" / "pipeline")
    existing_execution = {"kind": "step", "step_id": "collect", "active_attempt_id": "att_0001"}
    session.save_running_sync(
        "collect",
        {
            "current_index": 0,
            "rollback_count": 0,
            "interrupt_rollback_count": 0,
            "step_statuses": {"collect": "running"},
        },
        {"collected": {"value": "in-progress"}},
        identity,
        reason="step started",
        execution=existing_execution,
        attempts={"next_attempt_number": 2, "items": {"att_0001": {"status": "running"}}},
    )

    runner = PipelineRunner.__new__(PipelineRunner)
    runner.session = session
    runner._sidecar_status = None
    runner._session_id = "root-session"
    runner._pipeline_identity = identity
    runner._execution = {}
    runner._attempts = {"next_attempt_number": 2, "items": {"att_0001": {"status": "completed"}}}
    runner._observability = MagicMock()

    runner.state_machine = MagicMock()
    runner.state_machine.is_complete = True
    runner.state_machine._order = ["collect"]
    runner.state_machine.to_snapshot.return_value = {
        "current_index": 1,
        "rollback_count": 0,
        "interrupt_rollback_count": 0,
        "step_statuses": {"collect": "completed"},
    }
    runner.context = MagicMock()
    runner.context.to_snapshot.return_value = {"collected": {"value": "yes"}}

    runner.mark_normal_handoff(status="succeeded", failed_reason=None)

    meta = yaml.safe_load(runner.session.meta_path.read_text(encoding="utf-8"))
    assert meta["status"] == "completed"
    assert meta["execution"] == existing_execution
    assert meta["attempts"] == {"next_attempt_number": 2, "items": {"att_0001": {"status": "completed"}}}
    assert meta["normal_handoff"]["status"] == "succeeded"


def test_pipeline_runner_mark_user_aborted_marks_active_attempt_failed(tmp_path: Path):
    from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
    from iac_code.pipeline.engine.session import PipelineIdentity, PipelineSession

    runner = PipelineRunner.__new__(PipelineRunner)
    runner.session = PipelineSession(tmp_path / "session" / "pipeline")
    runner._sidecar_status = None
    runner._pipeline_identity = PipelineIdentity(
        pipeline_name="support",
        step_ids=["collect"],
        sub_pipeline_step_ids={},
        pipeline_fingerprint="fingerprint",
    )
    runner._execution = {"kind": "step", "step_id": "collect", "active_attempt_id": "att_0001"}
    runner._attempts = {
        "next_attempt_number": 2,
        "items": {"att_0001": {"attempt_id": "att_0001", "step_id": "collect", "status": "running"}},
    }

    runner.state_machine = MagicMock()
    runner.state_machine.is_complete = False
    runner.state_machine.current_step.step_id = "collect"
    runner.state_machine.to_snapshot.return_value = {
        "current_index": 0,
        "rollback_count": 0,
        "interrupt_rollback_count": 0,
        "step_statuses": {"collect": "pending"},
    }
    runner.context = MagicMock()
    runner.context.to_snapshot.return_value = {"collected": {"value": None}}

    runner.mark_user_aborted("ctrl-c")

    meta = yaml.safe_load(runner.session.meta_path.read_text(encoding="utf-8"))
    assert meta["status"] == "user_aborted"
    assert meta["terminal"] is True
    assert meta["execution"] == {"kind": "step", "step_id": "collect", "active_attempt_id": "att_0001"}
    assert meta["attempts"]["items"]["att_0001"]["status"] == "failed"


@pytest.mark.asyncio
async def test_user_aborted_pipeline_switches_to_normal_and_persists_notice():
    from iac_code.ui.repl import InlineREPL

    terminal_event = _pipeline_completed_event()
    repl, pipeline, _injected = _make_repl_for_handoff(
        terminal_event,
        should_switch_to_normal=False,
        is_complete=False,
    )
    root_message = Message(role="user", content="start")
    notice_message = Message(role="assistant", content=InlineREPL._pipeline_abort_notice_text())
    repl._render_pipeline_stream = AsyncMock(side_effect=asyncio.CancelledError)
    repl._session_storage.load.return_value = [root_message]
    repl._session_storage.repair_interrupted.side_effect = lambda messages: messages
    repl._agent_loop.replace_session = MagicMock()
    repl._agent_loop.context_manager.add_raw_message.return_value = notice_message

    with pytest.raises(asyncio.CancelledError):
        await repl._handle_pipeline_chat("start")

    assert repl._runtime_mode == RunMode.NORMAL
    pipeline.mark_user_aborted.assert_called_once_with("pipeline interrupted by user or renderer cancellation")
    repl._agent_loop.replace_session.assert_called_once_with("session-1", [root_message])
    repl._agent_loop.context_manager.add_raw_message.assert_called_once_with(
        {"role": "assistant", "content": InlineREPL._pipeline_abort_notice_text()}
    )
    repl.renderer.print_system_message.assert_called_once_with(
        InlineREPL._pipeline_abort_notice_text(),
        style="yellow",
    )
    repl._session_storage.append.assert_called_once_with(
        "/workspace",
        "session-1",
        notice_message,
        git_branch="main",
    )
    assert repl._pipeline is None
    assert repl._pipeline_waiting_input is False


@pytest.mark.asyncio
async def test_completed_pipeline_handoff_switches_to_normal_and_clears_state():
    terminal_event = _pipeline_completed_event()
    repl, pipeline, injected = _make_repl_for_handoff(terminal_event, should_switch_to_normal=True)

    await repl._handle_pipeline_chat("start")

    assert repl._runtime_mode == RunMode.NORMAL
    repl._agent_loop.context_manager.add_raw_message.assert_called_once_with(
        {"role": "user", "content": "handoff summary"}
    )
    repl._session_storage.append.assert_called_once_with(
        "/workspace",
        "session-1",
        injected,
        git_branch="main",
    )
    pipeline.mark_normal_handoff.assert_has_calls(
        [
            call(status="pending", failed_reason=None),
            call(status="succeeded", failed_reason=None),
        ]
    )
    pipeline.clear_sidecar.assert_not_called()
    pipeline.mark_user_aborted.assert_not_called()
    assert repl._pipeline is None
    assert repl._pipeline_waiting_input is False


@pytest.mark.asyncio
async def test_completed_pipeline_handoff_starts_hidden_cleanup_prompt(tmp_path: Path):
    terminal_event = _pipeline_completed_event()
    repl, pipeline, _injected = _make_repl_for_handoff(terminal_event, should_switch_to_normal=True)
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    observed = ObservedResource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-123",
        resource_name="demo",
        region_id="cn-hangzhou",
        source_step_id="deploying",
        source_attempt_id="att_0001",
        observed_action="CreateStack",
    )
    ledger.mark_cleanup_required(
        [CleanupResource.from_observed(observed, reason="rollback")],
        source_step_id="deploying",
        reason="rollback",
    )
    pipeline.cleanup_ledger = MagicMock(return_value=ledger)

    def add_raw_message(raw):
        return Message(role=raw["role"], content=raw["content"], metadata=raw.get("metadata", {}))

    repl._agent_loop.context_manager.add_raw_message.side_effect = add_raw_message

    async def cleanup_stream():
        yield ToolUseEndEvent(
            tool_use_id="toolu-delete",
            name="ros_stack",
            input={"action": "DeleteStack", "region_id": "cn-hangzhou", "params": {"StackId": "stack-123"}},
        )
        yield ToolResultEvent(
            tool_use_id="toolu-delete",
            tool_name="ros_stack",
            result='{"stack_id":"stack-123","status":"DELETE_COMPLETE","is_success":true}',
            is_error=False,
        )

    repl._agent_loop.continue_streaming = MagicMock(return_value=cleanup_stream())

    async def consume_cleanup_events(events, **_kwargs):
        async for _event in events:
            pass
        return (0.0, [], "")

    repl.renderer.run_streaming_output = AsyncMock(side_effect=consume_cleanup_events)
    repl._normalize_streaming_output_result = MagicMock(return_value=(0.0, [], ""))

    await repl._handle_pipeline_chat("start")

    repl.renderer.print_system_message.assert_any_call("\n检测到 1 个回滚残留资源，开始清理流程。", style="yellow")
    raw_cleanup_call = repl._agent_loop.context_manager.add_raw_message.call_args_list[-1].args[0]
    assert raw_cleanup_call["role"] == "user"
    assert raw_cleanup_call["metadata"]["type"] == CLEANUP_PROMPT_METADATA_TYPE
    assert "stack-123" in raw_cleanup_call["content"]
    repl.renderer.record_user_turn.assert_called_once_with("start")
    repl.renderer.run_streaming_output.assert_awaited_once()
    assert ledger.cleanup_resources()[0].cleanup_status == "completed"


def test_cleanup_resource_status_message_is_single_line_badge_style() -> None:
    from iac_code.ui.repl import InlineREPL

    resource = CleanupResource(
        provider="ros",
        resource_type="stack",
        resource_id="9b124deb-1ef2-46b1-8375-de8b76df2660",
        resource_name="basic-vpc-network",
        region_id="cn-hangzhou",
        cleanup_status="in_progress",
        progress_status="CREATE_COMPLETE",
    )

    message = InlineREPL._cleanup_resource_status_message(resource)

    assert message == (
        "↺ Rollback cleanup [Checking] basic-vpc-network · stack 9b124deb…2660 · "
        "cn-hangzhou · CREATE_COMPLETE; deletion required"
    )
    assert "\n" not in message
    assert "status=" not in message
    assert "progress=" not in message


@pytest.mark.asyncio
async def test_normal_resume_continues_existing_cleanup_prompt_without_duplicate(tmp_path: Path):
    from iac_code.pipeline.engine.cleanup import create_cleanup_prompt_message
    from iac_code.ui.repl import InlineREPL

    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-123",
                region_id="cn-hangzhou",
                cleanup_status="in_progress",
                progress_status="DELETE_REQUESTED",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    cleanup_prompt = ledger.build_pending_prompt()
    assert cleanup_prompt is not None

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.NORMAL
    repl._pipeline_cleanup_ledger_path = ledger.path
    repl.renderer = MagicMock()
    repl.renderer.print_system_message = MagicMock()
    repl.renderer._last_streaming_errors = []
    repl.store = MagicMock()
    repl._session_storage = MagicMock()
    repl._original_cwd = "/workspace"
    repl._session_id = "session-1"
    repl.current_git_branch = MagicMock(return_value="main")
    repl._streaming_error_log = []

    existing_prompt_message = create_cleanup_prompt_message(cleanup_prompt.prompt)
    repl._session_storage.load.return_value = [existing_prompt_message]
    repl._agent_loop = MagicMock()
    repl._agent_loop.context_manager = MagicMock()
    repl._agent_loop.context_manager.get_messages = MagicMock(return_value=[existing_prompt_message])
    repl._agent_loop.context_manager.add_raw_message = MagicMock()

    async def cleanup_stream():
        yield ToolUseEndEvent(
            tool_use_id="toolu-get",
            name="aliyun_api",
            input={
                "product": "ros",
                "action": "GetStack",
                "region_id": "cn-hangzhou",
                "params": {"StackId": "stack-123"},
            },
        )
        yield ToolResultEvent(
            tool_use_id="toolu-get",
            tool_name="aliyun_api",
            result='{"Stack":{"StackId":"stack-123","StackStatus":"DELETE_COMPLETE"}}',
            is_error=False,
        )

    repl._agent_loop.continue_streaming = MagicMock(return_value=cleanup_stream())

    async def consume_cleanup_events(events, **_kwargs):
        async for _event in events:
            pass
        return (0.0, [], "")

    repl.renderer.run_streaming_output = AsyncMock(side_effect=consume_cleanup_events)
    repl._normalize_streaming_output_result = MagicMock(return_value=(0.0, [], ""))

    assert await repl._maybe_start_normal_chat_cleanup_on_startup() is True

    repl.renderer.print_system_message.assert_any_call(
        "\n检测到 1 个回滚残留资源，开始清理流程。",
        style="yellow",
    )
    assert any(
        "DELETE_COMPLETE" in call.args[0] and call.kwargs.get("style") == "green"
        for call in repl.renderer.print_system_message.call_args_list
    )
    repl._agent_loop.context_manager.add_raw_message.assert_called_once()
    repl._session_storage.append.assert_not_called()
    assert ledger.cleanup_resources()[0].cleanup_status == "completed"


def test_normal_chat_finds_cleanup_ledger_from_prompt_metadata(tmp_path: Path):
    from iac_code.ui.repl import InlineREPL

    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-123",
                region_id="cn-hangzhou",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    cleanup_prompt = ledger.build_pending_prompt()
    assert cleanup_prompt is not None
    cleanup_message = create_cleanup_prompt_message(
        cleanup_prompt.prompt,
        cleanup_ledger_path=ledger.path,
        cleanup_status="pending",
    )

    repl = InlineREPL.__new__(InlineREPL)
    repl._session_storage = MagicMock()
    repl._session_storage.load.return_value = [cleanup_message]
    repl._original_cwd = "/workspace"
    repl._session_id = "session-1"
    repl._agent_loop = MagicMock()
    repl._agent_loop.context_manager = MagicMock()
    repl._agent_loop.context_manager.get_messages = MagicMock(return_value=[])

    restored = repl._cleanup_ledger_for_normal_chat()

    assert restored is not None
    assert restored.path == ledger.path


def test_normal_chat_ignores_observed_only_cleanup_ledger(tmp_path: Path):
    from iac_code.ui.repl import InlineREPL

    session_dir = tmp_path / "session"
    ledger = CleanupLedger(session_dir / "pipeline" / "cleanup.yaml")
    ledger.record_observed(
        ObservedResource(
            provider="ros",
            resource_type="stack",
            resource_id="stack-success",
            region_id="cn-hangzhou",
            observed_action="CreateStack",
            source_step_id="deploying",
        )
    )

    repl = InlineREPL.__new__(InlineREPL)
    repl._session_storage = MagicMock()
    repl._session_storage.load.return_value = []
    repl._session_storage.session_dir.return_value = session_dir
    repl._original_cwd = "/workspace"
    repl._session_id = "session-1"
    repl._agent_loop = MagicMock()
    repl._agent_loop.context_manager = MagicMock()
    repl._agent_loop.context_manager.get_messages = MagicMock(return_value=[])

    assert repl._cleanup_ledger_for_normal_chat() is None


def test_normal_chat_ignores_observed_only_explicit_cleanup_ledger(tmp_path: Path):
    from iac_code.ui.repl import InlineREPL

    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ledger.record_observed(
        ObservedResource(
            provider="ros",
            resource_type="stack",
            resource_id="stack-success",
            region_id="cn-hangzhou",
            observed_action="CreateStack",
            source_step_id="deploying",
        )
    )

    repl = InlineREPL.__new__(InlineREPL)
    repl._pipeline_cleanup_ledger_path = ledger.path
    repl._session_storage = MagicMock()
    repl._session_storage.load.return_value = []
    repl._original_cwd = "/workspace"
    repl._session_id = "session-1"
    repl._agent_loop = MagicMock()
    repl._agent_loop.context_manager = MagicMock()
    repl._agent_loop.context_manager.get_messages = MagicMock(return_value=[])

    assert repl._cleanup_ledger_for_normal_chat() is None
    assert not hasattr(repl, "_pipeline_cleanup_ledger_path")


def test_normal_chat_fallback_continues_past_observed_only_cleanup_ledger(monkeypatch, tmp_path: Path):
    from iac_code.ui.repl import InlineREPL

    pipeline_cwd = "/pipeline-workspace"
    original_cwd = "/workspace"
    session_id = "session-1"
    pipeline_dir = tmp_path / "pipeline-session"
    original_dir = tmp_path / "original-session"
    observed_ledger = CleanupLedger(pipeline_dir / "pipeline" / "cleanup.yaml")
    observed_ledger.record_observed(
        ObservedResource(
            provider="ros",
            resource_type="stack",
            resource_id="stack-success",
            region_id="cn-hangzhou",
            observed_action="CreateStack",
            source_step_id="deploying",
        )
    )
    pending_ledger = CleanupLedger(original_dir / "pipeline" / "cleanup.yaml")
    pending_ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-leftover",
                region_id="cn-hangzhou",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    monkeypatch.setattr("iac_code.pipeline.config.get_working_directory", lambda: pipeline_cwd)

    repl = InlineREPL.__new__(InlineREPL)
    repl._session_storage = MagicMock()
    repl._session_storage.load.return_value = []
    repl._session_storage.session_dir.side_effect = lambda cwd, _session_id: (
        pipeline_dir if cwd == pipeline_cwd else original_dir
    )
    repl._original_cwd = original_cwd
    repl._session_id = session_id
    repl._agent_loop = MagicMock()
    repl._agent_loop.context_manager = MagicMock()
    repl._agent_loop.context_manager.get_messages = MagicMock(return_value=[])

    restored = repl._cleanup_ledger_for_normal_chat()

    assert restored is not None
    assert restored.path == pending_ledger.path


def test_normal_chat_legacy_prompt_prefers_pending_ledger_across_cwds(monkeypatch, tmp_path: Path):
    from iac_code.ui.repl import InlineREPL

    pipeline_cwd = "/pipeline-workspace"
    original_cwd = "/workspace"
    session_id = "session-1"
    pipeline_dir = tmp_path / "pipeline-session"
    original_dir = tmp_path / "original-session"
    observed_ledger = CleanupLedger(pipeline_dir / "pipeline" / "cleanup.yaml")
    observed_ledger.record_observed(
        ObservedResource(
            provider="ros",
            resource_type="stack",
            resource_id="stack-success",
            region_id="cn-hangzhou",
            observed_action="CreateStack",
            source_step_id="deploying",
        )
    )
    pending_ledger = CleanupLedger(original_dir / "pipeline" / "cleanup.yaml")
    pending_ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-leftover",
                region_id="cn-hangzhou",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    legacy_prompt = create_cleanup_prompt_message("legacy cleanup prompt without ledger path")
    monkeypatch.setattr("iac_code.pipeline.config.get_working_directory", lambda: pipeline_cwd)

    repl = InlineREPL.__new__(InlineREPL)
    repl._session_storage = MagicMock()
    repl._session_storage.load.return_value = [legacy_prompt]
    repl._session_storage.session_dir.side_effect = lambda cwd, _session_id: (
        pipeline_dir if cwd == pipeline_cwd else original_dir
    )
    repl._original_cwd = original_cwd
    repl._session_id = session_id
    repl._agent_loop = MagicMock()
    repl._agent_loop.context_manager = MagicMock()
    repl._agent_loop.context_manager.get_messages = MagicMock(return_value=[])

    restored = repl._cleanup_ledger_for_normal_chat()

    assert restored is not None
    assert restored.path == pending_ledger.path


@pytest.mark.asyncio
async def test_cleanup_start_persists_prompt_when_runtime_has_prompt_but_session_does_not(tmp_path: Path):
    from iac_code.ui.repl import InlineREPL

    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-123",
                region_id="cn-hangzhou",
                cleanup_status="in_progress",
                progress_status="DELETE_REQUESTED",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    cleanup_prompt = ledger.build_pending_prompt()
    assert cleanup_prompt is not None
    existing_prompt_message = create_cleanup_prompt_message(cleanup_prompt.prompt)

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.NORMAL
    repl._pipeline_cleanup_ledger_path = ledger.path
    repl.renderer = MagicMock()
    repl.renderer.print_system_message = MagicMock()
    repl.renderer._last_streaming_errors = []
    repl.store = MagicMock()
    repl._session_storage = MagicMock()
    repl._session_storage.load.return_value = []
    repl._original_cwd = "/workspace"
    repl._session_id = "session-1"
    repl.current_git_branch = MagicMock(return_value="main")
    repl._streaming_error_log = []

    injected = create_cleanup_prompt_message(cleanup_prompt.prompt)
    repl._agent_loop = MagicMock()
    repl._agent_loop.context_manager = MagicMock()
    repl._agent_loop.context_manager.get_messages = MagicMock(return_value=[existing_prompt_message])
    repl._agent_loop.context_manager.add_raw_message = MagicMock(return_value=injected)
    repl._agent_loop.continue_streaming = MagicMock(return_value=_empty_stream())

    async def consume_cleanup_events(events, **_kwargs):
        async for _event in events:
            pass
        return (0.0, [], "")

    repl.renderer.run_streaming_output = AsyncMock(side_effect=consume_cleanup_events)
    repl._normalize_streaming_output_result = MagicMock(return_value=(0.0, [], ""))

    assert await repl._start_pipeline_cleanup_from_ledger(ledger) is True

    repl._session_storage.append.assert_called_once_with(
        "/workspace",
        "session-1",
        injected,
        git_branch="main",
    )


@pytest.mark.asyncio
async def test_normal_startup_prunes_completed_cleanup_prompt(tmp_path: Path):
    from iac_code.ui.repl import InlineREPL

    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-123",
                region_id="cn-hangzhou",
                cleanup_status="completed",
                progress_status="DELETE_COMPLETE",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.NORMAL
    repl._pipeline_cleanup_ledger_path = ledger.path
    repl._agent_loop = MagicMock()
    repl._agent_loop.context_manager = MagicMock()
    repl._agent_loop.context_manager.remove_cleanup_prompt_messages = MagicMock(return_value=1)

    assert await repl._maybe_start_normal_chat_cleanup_on_startup() is False

    repl._agent_loop.context_manager.remove_cleanup_prompt_messages.assert_called_once_with()


@pytest.mark.asyncio
async def test_normal_startup_replays_completed_cleanup_history_before_pruning(tmp_path: Path):
    from iac_code.ui.repl import InlineREPL

    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-123",
                resource_name="demo",
                region_id="cn-hangzhou",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-123",
        region_id="cn-hangzhou",
        cleanup_status="completed",
        progress_status="DELETE_COMPLETE",
    )

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.NORMAL
    repl._pipeline_cleanup_ledger_path = ledger.path
    repl.renderer = MagicMock()
    repl.renderer.print_system_message = MagicMock()
    repl._agent_loop = MagicMock()
    repl._agent_loop.context_manager = MagicMock()
    repl._agent_loop.context_manager.remove_cleanup_prompt_messages = MagicMock(return_value=1)

    assert await repl._maybe_start_normal_chat_cleanup_on_startup() is False

    rendered = "\n".join(call.args[0] for call in repl.renderer.print_system_message.call_args_list)
    assert "↺ Rollback cleanup resume: all 1 records are completed." in rendered
    assert "Rollback cleanup [Completed] demo" not in rendered
    assert "stack stack-123 · cn-hangzhou" not in rendered
    assert "status=" not in rendered
    assert "progress=" not in rendered


def test_cleanup_resume_summary_collapses_history_to_latest_resource_state(tmp_path: Path):
    from iac_code.ui.repl import InlineREPL

    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-1234567890",
                resource_name="demo-stack",
                region_id="cn-hangzhou",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-1234567890",
        region_id="cn-hangzhou",
        cleanup_status="started",
        progress_status="DELETE_STARTED",
    )
    ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-1234567890",
        region_id="cn-hangzhou",
        cleanup_status="in_progress",
        progress_status="DELETE_IN_PROGRESS",
    )
    ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-1234567890",
        region_id="cn-hangzhou",
        cleanup_status="failed",
        progress_status="DELETE_FAILED",
        last_error="DELETE_FAILED",
    )

    repl = InlineREPL.__new__(InlineREPL)
    repl._pipeline_cleanup_ledger_path = ledger.path
    repl.renderer = MagicMock()
    repl.renderer.print_system_message = MagicMock()

    repl._print_cleanup_resume_summary()

    rendered = "\n".join(call.args[0] for call in repl.renderer.print_system_message.call_args_list)
    assert "↺ Rollback cleanup resume: 1 records, 1 failed." in rendered
    assert rendered.count("  [Failed] demo-stack") == 1
    assert "↺ Rollback cleanup [Failed] demo-stack" not in rendered
    assert "  [Deleting] demo-stack" not in rendered
    assert "DELETE_FAILED" in rendered


def test_cleanup_resume_summary_collapses_completed_resources_and_indents_actionable_details(tmp_path: Path):
    from iac_code.ui.repl import InlineREPL

    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-completed",
                resource_name="completed-stack",
                region_id="cn-hangzhou",
            ),
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-failed",
                resource_name="failed-stack",
                region_id="cn-hangzhou",
            ),
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-running",
                resource_name="running-stack",
                region_id="cn-hangzhou",
            ),
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-completed",
        region_id="cn-hangzhou",
        cleanup_status="completed",
        progress_status="DELETE_COMPLETE",
    )
    ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-failed",
        region_id="cn-hangzhou",
        cleanup_status="failed",
        progress_status="DELETE_FAILED",
        last_error="DELETE_FAILED",
    )
    ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-running",
        region_id="cn-hangzhou",
        cleanup_status="in_progress",
        progress_status="DELETE_IN_PROGRESS",
    )

    repl = InlineREPL.__new__(InlineREPL)
    repl._pipeline_cleanup_ledger_path = ledger.path
    repl.renderer = MagicMock()
    repl.renderer.print_system_message = MagicMock()

    repl._print_cleanup_resume_summary()

    calls = repl.renderer.print_system_message.call_args_list
    assert calls[0].args[0] == "↺ Rollback cleanup resume: 3 records, 1 failed, 1 in progress, 1 completed."
    assert calls[0].kwargs["style"] == "yellow"
    assert not any("completed-stack" in call.args[0] for call in calls[1:])
    assert any("  [Failed] failed-stack" in call.args[0] and call.kwargs["style"] == "red" for call in calls)
    assert any("  [Deleting] running-stack" in call.args[0] and call.kwargs["style"] == "yellow" for call in calls)


def test_cleanup_resume_summary_shows_only_pending_detail_when_completed_resources_exist(tmp_path: Path):
    from iac_code.ui.repl import InlineREPL

    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-completed-1",
                resource_name="completed-one",
                region_id="cn-hangzhou",
            ),
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-completed-2",
                resource_name="completed-two",
                region_id="cn-hangzhou",
            ),
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-pending",
                resource_name="pending-stack",
                region_id="cn-hangzhou",
            ),
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-completed-1",
        region_id="cn-hangzhou",
        cleanup_status="completed",
        progress_status="DELETE_COMPLETE",
    )
    ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-completed-2",
        region_id="cn-hangzhou",
        cleanup_status="completed",
        progress_status="DELETE_COMPLETE",
    )

    repl = InlineREPL.__new__(InlineREPL)
    repl._pipeline_cleanup_ledger_path = ledger.path
    repl.renderer = MagicMock()
    repl.renderer.print_system_message = MagicMock()

    repl._print_cleanup_resume_summary()

    calls = repl.renderer.print_system_message.call_args_list
    rendered = "\n".join(call.args[0] for call in calls)
    assert calls[0].args[0] == "↺ Rollback cleanup resume: 3 records, 1 pending, 2 completed."
    assert rendered.count("  [Pending] pending-stack") == 1
    assert "completed-one" not in rendered
    assert "completed-two" not in rendered


@pytest.mark.asyncio
async def test_completed_cleanup_marks_session_prompt_completed(tmp_path: Path):
    from iac_code.services.session_storage import SessionStorage
    from iac_code.ui.repl import InlineREPL

    cwd = str(tmp_path / "workspace")
    session_id = "session-1"
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-123",
                region_id="cn-hangzhou",
                cleanup_status="completed",
                progress_status="DELETE_COMPLETE",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    cleanup_prompt = create_cleanup_prompt_message(
        "cleanup prompt for stack-123",
        cleanup_ledger_path=ledger.path,
        cleanup_status="pending",
    )
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    storage.append(cwd, session_id, cleanup_prompt, git_branch="main")
    runtime_prompt = create_cleanup_prompt_message(
        "cleanup prompt for stack-123",
        cleanup_ledger_path=ledger.path,
        cleanup_status="pending",
    )

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.NORMAL
    repl._pipeline_cleanup_ledger_path = ledger.path
    repl._session_storage = storage
    repl._original_cwd = cwd
    repl._session_id = session_id
    repl.current_git_branch = MagicMock(return_value="main")
    repl._agent_loop = MagicMock()
    repl._agent_loop.context_manager = MagicMock()
    repl._agent_loop.context_manager.get_messages = MagicMock(return_value=[runtime_prompt])
    repl._agent_loop.context_manager.remove_cleanup_prompt_messages = MagicMock(return_value=1)

    assert await repl._maybe_start_normal_chat_cleanup_on_startup() is False

    loaded = storage.load(cwd, session_id)
    assert loaded[0].metadata["cleanupStatus"] == "completed"
    assert runtime_prompt.metadata["cleanupStatus"] == "completed"
    repl._agent_loop.context_manager.remove_cleanup_prompt_messages.assert_called_once_with()


@pytest.mark.asyncio
async def test_normal_startup_keeps_cleanup_prompt_when_cleanup_ledger_is_corrupt(tmp_path: Path):
    from iac_code.ui.repl import InlineREPL

    path = tmp_path / "cleanup.yaml"
    path.write_text("[broken", encoding="utf-8")
    ledger = CleanupLedger(path)

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.NORMAL
    repl._pipeline_cleanup_ledger_path = ledger.path
    repl.renderer = MagicMock()
    repl.renderer.print_system_message = MagicMock()
    repl._agent_loop = MagicMock()
    repl._agent_loop.context_manager = MagicMock()
    repl._agent_loop.context_manager.remove_cleanup_prompt_messages = MagicMock(return_value=1)

    assert await repl._maybe_start_normal_chat_cleanup_on_startup() is False

    repl._agent_loop.context_manager.remove_cleanup_prompt_messages.assert_not_called()
    repl.renderer.print_system_message.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_observer_does_not_mutate_corrupt_ledger_or_prune_prompt(tmp_path: Path):
    from iac_code.ui.repl import InlineREPL

    path = tmp_path / "cleanup.yaml"
    path.write_text("[broken", encoding="utf-8")
    ledger = CleanupLedger(path)

    repl = InlineREPL.__new__(InlineREPL)
    repl.renderer = MagicMock()
    repl.renderer.print_system_message = MagicMock()
    repl._agent_loop = MagicMock()
    repl._agent_loop.context_manager = MagicMock()
    repl._agent_loop.context_manager.remove_cleanup_prompt_messages = MagicMock(return_value=1)

    async def events():
        yield ToolUseEndEvent(
            tool_use_id="toolu-delete",
            name="aliyun_api",
            input={
                "product": "ros",
                "action": "DeleteStack",
                "region_id": "cn-hangzhou",
                "params": {"StackId": "stack-123"},
            },
        )

    async for _event in repl._wrap_cleanup_observer(events(), ledger=ledger):
        pass
    repl._prune_cleanup_prompts_if_no_pending_cleanup(ledger)

    assert path.exists()
    assert not list(tmp_path.glob("cleanup.yaml.corrupt*"))
    repl._agent_loop.context_manager.remove_cleanup_prompt_messages.assert_not_called()


@pytest.mark.asyncio
async def test_normal_chat_blocks_agent_execution_when_cleanup_ledger_is_corrupt_with_prompt(tmp_path: Path):
    from iac_code.ui.repl import InlineREPL

    path = tmp_path / "cleanup.yaml"
    path.write_text("[broken", encoding="utf-8")
    cleanup_message = create_cleanup_prompt_message("cleanup prompt for stack-123")

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.NORMAL
    repl._pipeline_cleanup_ledger_path = path
    repl.renderer = MagicMock()
    repl.renderer.print_system_message = MagicMock()
    repl._agent_loop = MagicMock()
    repl._agent_loop.context_manager = MagicMock()
    repl._agent_loop.context_manager.get_messages = MagicMock(return_value=[cleanup_message])
    repl._agent_loop.run_streaming = MagicMock()

    assert await repl._handle_chat("hello") == []

    repl._agent_loop.run_streaming.assert_not_called()
    repl.renderer.print_system_message.assert_called_once()


@pytest.mark.asyncio
async def test_normal_chat_blocks_agent_execution_when_cleanup_ledger_is_missing_with_prompt(tmp_path: Path):
    from iac_code.ui.repl import InlineREPL

    cleanup_message = create_cleanup_prompt_message("cleanup prompt for stack-123")

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.NORMAL
    repl._pipeline_cleanup_ledger_path = tmp_path / "missing-cleanup.yaml"
    repl.renderer = MagicMock()
    repl.renderer.print_system_message = MagicMock()
    repl._session_storage = MagicMock()
    repl._session_storage.load.return_value = [cleanup_message]
    repl._original_cwd = "/workspace"
    repl._session_id = "session-1"
    repl._streaming_draft_input = ""
    repl._agent_loop = MagicMock()
    repl._agent_loop.context_manager = MagicMock()
    repl._agent_loop.context_manager.get_messages = MagicMock(return_value=[cleanup_message])
    repl._agent_loop.context_manager.remove_cleanup_prompt_messages = MagicMock()
    repl._agent_loop.run_streaming = MagicMock()

    assert await repl._handle_chat("hello") == []

    repl._agent_loop.run_streaming.assert_not_called()
    repl._agent_loop.context_manager.remove_cleanup_prompt_messages.assert_not_called()
    repl.renderer.print_system_message.assert_called_once()
    assert repl._streaming_draft_input == "hello"


@pytest.mark.asyncio
async def test_normal_chat_runs_pending_cleanup_before_user_prompt(tmp_path: Path):
    from iac_code.ui.repl import InlineREPL

    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-123",
                region_id="cn-hangzhou",
                cleanup_status="in_progress",
                progress_status="DELETE_REQUESTED",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    calls: list[object] = []
    messages: list[Message] = []

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.NORMAL
    repl._pipeline_cleanup_ledger_path = ledger.path
    repl.renderer = MagicMock()
    repl.renderer.print_system_message = MagicMock()
    repl.renderer.record_user_turn = MagicMock()
    repl.renderer._last_streaming_errors = []
    repl.store = MagicMock()
    repl._session_storage = MagicMock()
    repl._session_storage.load.return_value = []
    repl._original_cwd = "/workspace"
    repl._session_id = "session-1"
    repl.current_git_branch = MagicMock(return_value="main")
    repl._streaming_error_log = []
    repl._agent_loop = MagicMock()
    repl._agent_loop.context_manager = MagicMock()
    repl._agent_loop.context_manager.get_messages = MagicMock(side_effect=lambda: messages)

    def add_raw_message(raw):
        message = Message(role=raw["role"], content=raw["content"], metadata=raw.get("metadata", {}))
        messages.append(message)
        return message

    repl._agent_loop.context_manager.add_raw_message = MagicMock(side_effect=add_raw_message)
    repl._agent_loop.context_manager.remove_cleanup_prompt_messages = MagicMock(
        side_effect=lambda: messages.clear() or 0
    )

    async def cleanup_stream():
        yield StackProgressEvent(
            stack_id="stack-123",
            stack_name="demo",
            status="DELETE_COMPLETE",
            progress_percentage=100,
            elapsed_seconds=1.0,
            resources=[],
        )

    async def user_stream():
        if False:
            yield None

    def continue_streaming():
        calls.append("cleanup")
        return cleanup_stream()

    def run_streaming(prompt, **_kwargs):
        calls.append(("user", prompt))
        return user_stream()

    repl._agent_loop.continue_streaming = MagicMock(side_effect=continue_streaming)
    repl._agent_loop.run_streaming = MagicMock(side_effect=run_streaming)

    async def consume_events(events, **_kwargs):
        async for _event in events:
            pass
        return (0.0, [], "")

    repl.renderer.run_streaming_output = AsyncMock(side_effect=consume_events)
    repl._normalize_streaming_output_result = MagicMock(return_value=(0.0, [], ""))

    assert await repl._handle_chat("please continue") == []

    assert calls == ["cleanup", ("user", "please continue")]
    repl.renderer.record_user_turn.assert_called_once_with("please continue")
    assert ledger.pending_resources() == []


@pytest.mark.asyncio
async def test_cleanup_observer_prints_status_transitions_and_persists_history(tmp_path: Path):
    from iac_code.ui.repl import InlineREPL

    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-123",
                resource_name="demo",
                region_id="cn-hangzhou",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    repl = InlineREPL.__new__(InlineREPL)
    repl.renderer = MagicMock()
    repl.renderer.print_system_message = MagicMock()

    async def cleanup_events():
        yield ToolUseEndEvent(
            tool_use_id="toolu-delete",
            name="aliyun_api",
            input={
                "product": "ros",
                "action": "DeleteStack",
                "region_id": "cn-hangzhou",
                "params": {"StackId": "stack-123"},
            },
        )
        yield ToolResultEvent(
            tool_use_id="toolu-delete",
            tool_name="aliyun_api",
            result='{"RequestId":"req-1"}',
            is_error=False,
        )
        yield ToolUseEndEvent(
            tool_use_id="toolu-get",
            name="aliyun_api",
            input={
                "product": "ros",
                "action": "GetStack",
                "region_id": "cn-hangzhou",
                "params": {"StackId": "stack-123"},
            },
        )
        yield ToolResultEvent(
            tool_use_id="toolu-get",
            tool_name="aliyun_api",
            result='{"StackId":"stack-123","Status":"DELETE_COMPLETE"}',
            is_error=False,
        )

    async for _event in repl._wrap_cleanup_observer(cleanup_events(), ledger=ledger):
        pass

    rendered = "\n".join(call.args[0] for call in repl.renderer.print_system_message.call_args_list)
    assert "stack-123" in rendered
    assert "DELETE_STARTED" in rendered
    assert "DELETE_REQUESTED" in rendered
    assert "DELETE_COMPLETE" in rendered

    history_types = [entry["type"] for entry in ledger._load()["history"]]
    assert history_types == [
        "cleanup_required",
        "cleanup_started",
        "cleanup_progress",
        "cleanup_completed",
    ]


@pytest.mark.asyncio
async def test_normal_chat_preserves_user_prompt_when_cleanup_remains_pending(tmp_path: Path):
    from iac_code.ui.repl import InlineREPL

    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-123",
                region_id="cn-hangzhou",
                cleanup_status="in_progress",
                progress_status="DELETE_REQUESTED",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    messages: list[Message] = []

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.NORMAL
    repl._pipeline_cleanup_ledger_path = ledger.path
    repl.renderer = MagicMock()
    repl.renderer.print_system_message = MagicMock()
    repl.renderer.record_user_turn = MagicMock()
    repl.renderer._last_streaming_errors = []
    repl.store = MagicMock()
    repl._session_storage = MagicMock()
    repl._session_storage.load.return_value = []
    repl._original_cwd = "/workspace"
    repl._session_id = "session-1"
    repl.current_git_branch = MagicMock(return_value="main")
    repl._streaming_error_log = []
    repl._agent_loop = MagicMock()
    repl._agent_loop.context_manager = MagicMock()
    repl._agent_loop.context_manager.get_messages = MagicMock(side_effect=lambda: messages)

    def add_raw_message(raw):
        message = Message(role=raw["role"], content=raw["content"], metadata=raw.get("metadata", {}))
        messages.append(message)
        return message

    repl._agent_loop.context_manager.add_raw_message = MagicMock(side_effect=add_raw_message)
    repl._agent_loop.context_manager.remove_cleanup_prompt_messages = MagicMock(
        side_effect=lambda: messages.clear() or 0
    )
    repl._agent_loop.continue_streaming = MagicMock(return_value=_empty_stream())
    repl._agent_loop.run_streaming = MagicMock()

    async def consume_events(events, **_kwargs):
        async for _event in events:
            pass
        return (0.0, [], "")

    repl.renderer.run_streaming_output = AsyncMock(side_effect=consume_events)
    repl._normalize_streaming_output_result = MagicMock(return_value=(0.0, [], ""))

    assert await repl._handle_chat("please continue") == []

    repl._agent_loop.continue_streaming.assert_called_once()
    repl._agent_loop.run_streaming.assert_not_called()
    repl.renderer.record_user_turn.assert_not_called()
    assert repl._streaming_draft_input == "please continue"
    assert ledger.pending_resources()


@pytest.mark.asyncio
async def test_pipeline_cleanup_start_replaces_stale_runtime_cleanup_prompt(tmp_path: Path):
    from iac_code.ui.repl import InlineREPL

    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-done",
                region_id="cn-hangzhou",
                source_step_id="deploying",
            ),
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-pending",
                region_id="cn-hangzhou",
                source_step_id="deploying",
            ),
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    stale_prompt = ledger.build_pending_prompt()
    assert stale_prompt is not None
    messages = [create_cleanup_prompt_message(stale_prompt.prompt)]
    ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-done",
        region_id="cn-hangzhou",
        cleanup_status="completed",
        progress_status="DELETE_COMPLETE",
    )

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.NORMAL
    repl._pipeline_cleanup_ledger_path = ledger.path
    repl.renderer = MagicMock()
    repl.renderer.print_system_message = MagicMock()
    repl.renderer._last_streaming_errors = []
    repl.store = MagicMock()
    repl._session_storage = MagicMock()
    repl._original_cwd = "/workspace"
    repl._session_id = "session-1"
    repl.current_git_branch = MagicMock(return_value="main")
    repl._streaming_error_log = []
    repl._agent_loop = MagicMock()
    repl._agent_loop.context_manager = MagicMock()
    repl._agent_loop.context_manager.get_messages = MagicMock(side_effect=lambda: messages)

    def add_raw_message(raw):
        message = Message(role=raw["role"], content=raw["content"], metadata=raw.get("metadata", {}))
        messages.append(message)
        return message

    def remove_cleanup_prompt_messages():
        removed = len(messages)
        messages.clear()
        return removed

    repl._agent_loop.context_manager.add_raw_message = MagicMock(side_effect=add_raw_message)
    repl._agent_loop.context_manager.remove_cleanup_prompt_messages = MagicMock(
        side_effect=remove_cleanup_prompt_messages
    )

    async def empty_stream():
        if False:
            yield None

    repl._agent_loop.continue_streaming = MagicMock(return_value=empty_stream())

    async def consume_cleanup_events(events, **_kwargs):
        async for _event in events:
            pass
        return (0.0, [], "")

    repl.renderer.run_streaming_output = AsyncMock(side_effect=consume_cleanup_events)
    repl._normalize_streaming_output_result = MagicMock(return_value=(0.0, [], ""))

    assert await repl._start_pipeline_cleanup_from_ledger(ledger) is True

    cleanup_messages = [message for message in messages if message.metadata.get("type") == CLEANUP_PROMPT_METADATA_TYPE]
    assert len(cleanup_messages) == 1
    assert "stack-pending" in cleanup_messages[0].content
    assert "stack-done" not in cleanup_messages[0].content


@pytest.mark.asyncio
async def test_pipeline_cleanup_start_removes_stale_prompt_even_when_latest_prompt_exists(tmp_path: Path):
    from iac_code.ui.repl import InlineREPL

    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-done",
                region_id="cn-hangzhou",
                source_step_id="deploying",
            ),
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-pending",
                region_id="cn-hangzhou",
                source_step_id="deploying",
            ),
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    stale_prompt = ledger.build_pending_prompt()
    assert stale_prompt is not None
    stale_message = create_cleanup_prompt_message(stale_prompt.prompt)
    ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-done",
        region_id="cn-hangzhou",
        cleanup_status="completed",
        progress_status="DELETE_COMPLETE",
    )
    latest_prompt = ledger.build_pending_prompt()
    assert latest_prompt is not None
    latest_message = create_cleanup_prompt_message(latest_prompt.prompt)
    messages = [stale_message, latest_message]

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.NORMAL
    repl._pipeline_cleanup_ledger_path = ledger.path
    repl.renderer = MagicMock()
    repl.renderer.print_system_message = MagicMock()
    repl.renderer._last_streaming_errors = []
    repl.store = MagicMock()
    repl._session_storage = MagicMock()
    repl._original_cwd = "/workspace"
    repl._session_id = "session-1"
    repl.current_git_branch = MagicMock(return_value="main")
    repl._streaming_error_log = []
    repl._agent_loop = MagicMock()
    repl._agent_loop.context_manager = MagicMock()
    repl._agent_loop.context_manager.get_messages = MagicMock(side_effect=lambda: messages)

    def add_raw_message(raw):
        message = Message(role=raw["role"], content=raw["content"], metadata=raw.get("metadata", {}))
        messages.append(message)
        return message

    def remove_cleanup_prompt_messages():
        removed = len(messages)
        messages.clear()
        return removed

    repl._agent_loop.context_manager.add_raw_message = MagicMock(side_effect=add_raw_message)
    repl._agent_loop.context_manager.remove_cleanup_prompt_messages = MagicMock(
        side_effect=remove_cleanup_prompt_messages
    )

    async def empty_stream():
        if False:
            yield None

    repl._agent_loop.continue_streaming = MagicMock(return_value=empty_stream())

    async def consume_cleanup_events(events, **_kwargs):
        async for _event in events:
            pass
        return (0.0, [], "")

    repl.renderer.run_streaming_output = AsyncMock(side_effect=consume_cleanup_events)
    repl._normalize_streaming_output_result = MagicMock(return_value=(0.0, [], ""))

    assert await repl._start_pipeline_cleanup_from_ledger(ledger) is True

    cleanup_messages = [message for message in messages if message.metadata.get("type") == CLEANUP_PROMPT_METADATA_TYPE]
    assert len(cleanup_messages) == 1
    assert cleanup_messages[0].content == latest_prompt.prompt


@pytest.mark.asyncio
async def test_early_exit_handoff_clears_incomplete_state_without_user_abort():
    terminal_event = _pipeline_completed_event(early_exit=True)
    repl, pipeline, _injected = _make_repl_for_handoff(
        terminal_event,
        should_switch_to_normal=True,
        is_complete=False,
    )

    await repl._handle_pipeline_chat("start")

    assert repl._runtime_mode == RunMode.NORMAL
    pipeline.mark_normal_handoff.assert_has_calls(
        [
            call(status="pending", failed_reason=None),
            call(status="succeeded", failed_reason=None),
        ]
    )
    pipeline.clear_sidecar.assert_not_called()
    pipeline.mark_user_aborted.assert_not_called()
    assert repl._pipeline is None
    assert repl._pipeline_waiting_input is False


@pytest.mark.asyncio
async def test_failed_terminal_event_does_not_handoff_and_failed_cleanup_remains():
    terminal_event = _pipeline_completed_event(failed=True)
    repl, pipeline, _injected = _make_repl_for_handoff(
        terminal_event,
        should_switch_to_normal=False,
        is_complete=False,
        sidecar_status="failed",
    )

    await repl._handle_pipeline_chat("start")

    assert repl._runtime_mode == RunMode.PIPELINE
    repl._agent_loop.context_manager.add_raw_message.assert_not_called()
    repl._session_storage.append.assert_not_called()
    pipeline.clear_sidecar.assert_not_called()
    pipeline.mark_normal_handoff.assert_not_called()
    pipeline.mark_user_aborted.assert_not_called()
    assert repl._pipeline is None
    assert repl._pipeline_waiting_input is False


@pytest.mark.asyncio
async def test_handoff_injection_failure_still_switches_to_normal_and_preserves_sidecar():
    terminal_event = _pipeline_completed_event()
    repl, pipeline, _injected = _make_repl_for_handoff(terminal_event, should_switch_to_normal=True)
    repl._agent_loop.context_manager.add_raw_message.side_effect = RuntimeError("context unavailable")

    await repl._handle_pipeline_chat("start")

    assert repl._runtime_mode == RunMode.NORMAL
    repl._session_storage.append.assert_not_called()
    repl.renderer.print_system_message.assert_called()
    pipeline.mark_normal_handoff.assert_has_calls(
        [
            call(status="pending", failed_reason=None),
            call(status="failed", failed_reason="context unavailable"),
        ]
    )
    pipeline.clear_sidecar.assert_not_called()
    pipeline.mark_user_aborted.assert_not_called()
    pipeline.resume.assert_called_once_with(PipelineUserInput(content="start", display_text="start", has_images=False))
    pipeline.run.assert_not_called()
    assert repl._pipeline is None
    assert repl._pipeline_waiting_input is False


@pytest.mark.asyncio
async def test_handoff_summary_append_failure_still_switches_to_normal_and_preserves_sidecar():
    terminal_event = _pipeline_completed_event()
    repl, pipeline, _injected = _make_repl_for_handoff(terminal_event, should_switch_to_normal=True)
    repl._session_storage.append.side_effect = RuntimeError("disk unavailable")

    await repl._handle_pipeline_chat("start")

    assert repl._runtime_mode == RunMode.NORMAL
    repl._agent_loop.context_manager.add_raw_message.assert_called_once()
    repl.renderer.print_system_message.assert_called()
    pipeline.mark_normal_handoff.assert_has_calls(
        [
            call(status="pending", failed_reason=None),
            call(status="failed", failed_reason="disk unavailable"),
        ]
    )
    pipeline.clear_sidecar.assert_not_called()
    pipeline.mark_user_aborted.assert_not_called()
    assert repl._pipeline is None
    assert repl._pipeline_waiting_input is False


@pytest.mark.asyncio
async def test_outer_stream_returns_candidate_selection_terminal_event():
    from iac_code.ui.repl import InlineREPL

    terminal_event = _pipeline_completed_event()
    repl = InlineREPL.__new__(InlineREPL)
    repl._pipeline = MagicMock()
    repl._pipeline_step_names = []
    repl._pipeline_completed_indices = set()
    repl._update_pipeline_state_from_event = MagicMock()
    repl._render_pipeline_event = MagicMock()
    repl._render_candidate_selection_tabs = AsyncMock(return_value=terminal_event)
    repl._restart_pipeline_stream_after_interrupt = AsyncMock(return_value=_empty_stream())

    async def stream():
        yield PipelineEvent(
            type=PipelineEventType.STEP_STARTED,
            step_id="select",
            timestamp=time.time(),
            data={"index": 1, "total": 1, "ui_mode": "candidate_selection"},
        )

    result = await repl._render_pipeline_stream(stream())

    assert result is terminal_event
    repl._restart_pipeline_stream_after_interrupt.assert_not_awaited()


@pytest.mark.asyncio
async def test_outer_stream_returns_parallel_tabs_terminal_event():
    from iac_code.ui.repl import InlineREPL

    terminal_event = _pipeline_completed_event()
    repl = InlineREPL.__new__(InlineREPL)
    repl._pipeline = MagicMock()
    repl._pipeline_step_names = []
    repl._pipeline_completed_indices = set()
    repl._update_pipeline_state_from_event = MagicMock()
    repl._render_pipeline_event = MagicMock()
    repl._render_parallel_tabs = AsyncMock(return_value=terminal_event)
    repl._restart_pipeline_stream_after_interrupt = AsyncMock(return_value=_empty_stream())

    async def stream():
        yield PipelineEvent(
            type=PipelineEventType.STEP_STARTED,
            step_id="parallel",
            timestamp=time.time(),
            data={
                "index": 1,
                "total": 1,
                "ui_mode": "default",
                "step_type": "parallel_sub_pipeline",
            },
        )

    result = await repl._render_pipeline_stream(stream())

    assert result is terminal_event
    repl._restart_pipeline_stream_after_interrupt.assert_not_awaited()


@pytest.mark.asyncio
async def test_outer_stream_returns_candidate_selection_persistence_failure_without_marking_completed():
    from iac_code.ui.repl import InlineREPL

    failure_event = _pipeline_persistence_failure_event("select")
    repl = InlineREPL.__new__(InlineREPL)
    repl._pipeline = MagicMock()
    repl._pipeline_step_names = []
    repl._pipeline_completed_indices = set()
    repl._update_pipeline_state_from_event = InlineREPL._update_pipeline_state_from_event.__get__(repl)
    repl._render_pipeline_event = MagicMock()
    repl._render_candidate_selection_tabs = AsyncMock(return_value=failure_event)
    repl._restart_pipeline_stream_after_interrupt = AsyncMock(return_value=_empty_stream())

    async def stream():
        yield PipelineEvent(
            type=PipelineEventType.PIPELINE_STARTED,
            step_id=None,
            timestamp=time.time(),
            data={"step_names": ["select"]},
        )
        yield PipelineEvent(
            type=PipelineEventType.STEP_STARTED,
            step_id="select",
            timestamp=time.time(),
            data={"index": 1, "total": 1, "ui_mode": "candidate_selection"},
        )

    result = await repl._render_pipeline_stream(stream())

    assert result is failure_event
    assert repl._pipeline_completed_indices == set()


@pytest.mark.asyncio
async def test_outer_stream_returns_parallel_tabs_persistence_failure_without_marking_completed():
    from iac_code.ui.repl import InlineREPL

    failure_event = _pipeline_persistence_failure_event("parallel")
    repl = InlineREPL.__new__(InlineREPL)
    repl._pipeline = MagicMock()
    repl._pipeline_step_names = []
    repl._pipeline_completed_indices = set()
    repl._update_pipeline_state_from_event = InlineREPL._update_pipeline_state_from_event.__get__(repl)
    repl._render_pipeline_event = MagicMock()
    repl._render_parallel_tabs = AsyncMock(return_value=failure_event)
    repl._restart_pipeline_stream_after_interrupt = AsyncMock(return_value=_empty_stream())

    async def stream():
        yield PipelineEvent(
            type=PipelineEventType.PIPELINE_STARTED,
            step_id=None,
            timestamp=time.time(),
            data={"step_names": ["parallel"]},
        )
        yield PipelineEvent(
            type=PipelineEventType.STEP_STARTED,
            step_id="parallel",
            timestamp=time.time(),
            data={
                "index": 1,
                "total": 1,
                "ui_mode": "default",
                "step_type": "parallel_sub_pipeline",
            },
        )

    result = await repl._render_pipeline_stream(stream())

    assert result is failure_event
    assert repl._pipeline_completed_indices == set()


@pytest.mark.asyncio
async def test_render_parallel_tabs_returns_consumed_pipeline_completed_event(monkeypatch):
    from iac_code.ui.repl import InlineREPL

    terminal_event = _pipeline_completed_event()
    repl = InlineREPL.__new__(InlineREPL)
    repl.renderer = MagicMock()
    repl.renderer.console = Console(file=StringIO(), width=120, force_terminal=True)
    repl._pipeline = MagicMock()

    class FakeLive:
        def start(self):
            pass

        def stop(self):
            pass

        def update(self, *args, **kwargs):
            pass

    monkeypatch.setattr(InlineREPL, "_create_parallel_live", lambda self: FakeLive(), raising=False)

    async def stream():
        yield terminal_event

    result = await repl._render_parallel_tabs(stream())

    assert result is terminal_event


@pytest.mark.asyncio
async def test_render_candidate_selection_tabs_returns_consumed_persistence_failure_event(monkeypatch):
    from iac_code.ui.repl import InlineREPL

    failure_event = _pipeline_persistence_failure_event("select")
    repl = InlineREPL.__new__(InlineREPL)
    repl.renderer = MagicMock()
    repl.renderer.console = Console(file=StringIO(), width=120, force_terminal=True)
    repl._pipeline = MagicMock()
    repl._pipeline_waiting_input = False
    repl._pipeline_display_recorder = None
    repl._pipeline_state_persistence_failed = False

    class FakeLive:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def update(self, *args, **kwargs):
            pass

    class FakeCapture:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read_key(self, timeout):
            return None

    monkeypatch.setattr("iac_code.ui.repl.Live", FakeLive)
    monkeypatch.setattr("iac_code.ui.core.raw_input.RawInputCapture", FakeCapture)

    async def stream():
        yield failure_event

    result = await repl._render_candidate_selection_tabs(stream())

    assert result is failure_event
    assert repl._pipeline_state_persistence_failed is True


@pytest.mark.asyncio
async def test_render_parallel_tabs_returns_consumed_persistence_failure_event(monkeypatch):
    from iac_code.ui.repl import InlineREPL

    failure_event = _pipeline_persistence_failure_event("parallel")
    repl = InlineREPL.__new__(InlineREPL)
    repl.renderer = MagicMock()
    repl.renderer.console = Console(file=StringIO(), width=120, force_terminal=True)
    repl._pipeline = MagicMock()
    repl._pipeline_display_recorder = None
    repl._pipeline_state_persistence_failed = False

    class FakeLive:
        def start(self):
            pass

        def stop(self):
            pass

        def update(self, *args, **kwargs):
            pass

    monkeypatch.setattr(InlineREPL, "_create_parallel_live", lambda self: FakeLive(), raising=False)

    async def stream():
        yield failure_event

    result = await repl._render_parallel_tabs(stream())

    assert result is failure_event
    assert repl._pipeline_state_persistence_failed is True
