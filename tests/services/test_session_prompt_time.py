from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from iac_code.agent.agent_loop import AgentLoop
from iac_code.agent.message import Message
from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
from iac_code.services import session_prompt_time
from iac_code.services.agent_factory import AgentFactoryOptions, create_agent_runtime
from iac_code.services.session_backup import BackupReason, SessionBackupService
from iac_code.services.session_metadata import read_session_metadata, write_session_metadata
from iac_code.services.session_prompt_time import SessionPromptTime
from iac_code.services.session_storage import SessionStorage
from iac_code.tools.base import Tool, ToolRegistry, ToolResult
from iac_code.types.stream_events import (
    AskUserQuestionEvent,
    MessageEndEvent,
    MessageStartEvent,
    TextDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)


@pytest.fixture
def clock(monkeypatch, tmp_path):
    class Clock:
        current = datetime(2026, 11, 26, 10, 0, tzinfo=datetime.now().astimezone().tzinfo)

        @classmethod
        def now(cls):
            return cls.current

    monkeypatch.setattr(session_prompt_time, "datetime", Clock)
    # Parsing must continue to use the standard datetime implementation.
    Clock.fromisoformat = staticmethod(datetime.fromisoformat)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_DIR", raising=False)
    monkeypatch.delenv("IAC_CODE_WIKI_AUTO_SEARCH", raising=False)
    return Clock


def _time(prompt):
    return next(line for line in prompt.splitlines() if line.startswith("- Current time: "))


def _pipeline(tmp_path, storage, provider, *, resume=False):
    path = tmp_path / "pipeline"
    path.mkdir(exist_ok=True)
    (path / "step.md").write_text("Complete the current step.", encoding="utf-8")
    (path / "pipeline.yaml").write_text(
        json.dumps(
            {
                "name": "clock-test",
                "base_prompt_sections": {"include": ["runtime_context"]},
                "context_dependencies": {"first": [], "second": ["first"]},
                "steps": [
                    {
                        "id": name,
                        "conclusion_field": name,
                        "forward": forward,
                        "prompt": "step.md",
                        "auto_advance": True,
                        "inject_tools": ["ask_user_question"],
                        "conclusion_schema": {"type": "object"},
                    }
                    for name, forward in [("first", "second"), ("second", None)]
                ],
            }
        ),
        encoding="utf-8",
    )
    return PipelineRunner(
        path, provider, ToolRegistry(), storage, "session", cwd=str(tmp_path), surface="a2a", resume_from_sidecar=resume
    )


class Provider:
    def __init__(self, clock, *, questions=False):
        self.clock = clock
        self.questions = questions
        self.prompts = []

    def get_model_name(self):
        return "offline"

    async def stream(self, messages, system, tools=None):
        self.prompts.append(system)
        number = len(self.prompts)
        yield MessageStartEvent(message_id=str(number))
        pipeline_tools = bool(tools and any(tool.name == "complete_step" for tool in tools))
        if not pipeline_tools:
            yield TextDeltaEvent(text="Hello")
        else:
            name = "ask_user_question" if self.questions and number <= 2 else "complete_step"
            arguments = (
                {"question": "Which region?", "options": [{"id": "a", "label": "A"}]}
                if name == "ask_user_question"
                else {"conclusion": {"value": "ok"}}
            )
            yield ToolUseStartEvent(tool_use_id=str(number), name=name)
            yield ToolUseEndEvent(tool_use_id=str(number), name=name, input=arguments)
            if number == 3:
                # The next step is still part of this turn, even after 30 minutes.
                self.clock.current += timedelta(minutes=40)
        yield MessageEndEvent(stop_reason="tool_use" if pipeline_tools else "end_turn", usage=Usage())


@pytest.mark.parametrize("seconds,renewed", [(0, False), (1799, False), (1800, False), (1801, True)])
def test_validity_and_no_writes_for_unexpired_turns(clock, tmp_path, monkeypatch, seconds, renewed):
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    prompt_time = SessionPromptTime(storage, str(tmp_path), "session")
    prompt_time.begin_turn()
    original = prompt_time.value
    write = Mock(wraps=session_prompt_time.write_session_metadata)
    monkeypatch.setattr(session_prompt_time, "write_session_metadata", write)
    clock.current += timedelta(seconds=seconds)
    prompt_time.begin_turn()
    assert (prompt_time.value != original) is renewed
    assert write.call_count == int(renewed)


def test_continuous_requests_do_not_extend_the_original_expiry(clock, tmp_path):
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    prompt_time = SessionPromptTime(storage, str(tmp_path), "session")
    prompt_time.begin_turn()
    for minutes, expected in [(20, "10:00:00"), (31, "10:31:00"), (50, "10:31:00")]:
        clock.current = clock.current.replace(hour=10, minute=minutes)
        # Recreating a runtime must use the session anchor, not its creation time.
        prompt_time = SessionPromptTime(storage, str(tmp_path), "session")
        prompt_time.begin_turn()
        assert prompt_time.value.endswith(expected)


@pytest.mark.parametrize("saved", [None, "broken", "2026-11-26T10:00:00", "2026-11-27T10:00:00+00:00"])
def test_old_sessions_invalid_values_and_clock_rollback(clock, tmp_path, saved):
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    directory = storage.ensure_v2_session_dir_for_new_session(str(tmp_path), "session")
    metadata = read_session_metadata(directory)
    write_session_metadata(directory, replace(metadata, prompt_time=saved))
    prompt_time = SessionPromptTime(storage, str(tmp_path), "session")
    prompt_time.begin_turn()
    assert prompt_time.value.endswith("10:00:00")
    assert datetime.fromisoformat(read_session_metadata(directory).prompt_time) == clock.current


def test_rename_git_metadata_and_history_preserve_prompt_time(clock, tmp_path):
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    prompt_time = SessionPromptTime(storage, str(tmp_path), "session")
    prompt_time.begin_turn()
    storage.append(str(tmp_path), "session", Message(role="user", content="Hello"), git_branch="main")
    storage.rename_session(str(tmp_path), "session", "renamed")
    saved = storage.read_metadata(str(tmp_path), "session")
    assert saved.name == "renamed"
    assert datetime.fromisoformat(saved.prompt_time) == clock.current


@pytest.mark.asyncio
async def test_normal_followup_and_no_renewal_inside_model_execution(clock, tmp_path):
    storage = SessionStorage()
    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.6-plus",
            session_id="session",
            cwd=str(tmp_path),
            disable_external_services=True,
        )
    )
    loop = runtime.agent_loop
    prompt_time = loop._prompt_time
    provider = Provider(clock)
    loop._memory_recall_service = None
    loop.set_provider(provider)
    await loop.run("Hello")
    first = _time(provider.prompts[-1])
    clock.current += timedelta(minutes=20)
    await loop.run("Follow up")
    assert _time(provider.prompts[-1]) == first
    clock.current += timedelta(minutes=11)
    await loop.run("Another follow up")
    assert _time(provider.prompts[-1]).endswith("10:31:00")
    clock.current += timedelta(minutes=40)
    loop._refresh_system_prompt()
    assert _time(loop.system_prompt).endswith("10:31:00")
    # A reconstructed runtime only reads the anchor; transport/recovery does not renew it.
    reconstructed = SessionPromptTime(storage, str(tmp_path), "session")
    assert reconstructed.value == prompt_time.value
    await runtime.aclose()


@pytest.mark.asyncio
async def test_normal_tool_round_does_not_renew_time_when_execution_exceeds_validity(clock, tmp_path):
    class Echo(Tool):
        name = "echo"
        description = "Offline read-only tool"
        input_schema = {"type": "object"}

        def is_read_only(self, input=None):
            return True

        async def execute(self, *, tool_input, context):
            return ToolResult.success("done")

    class MultiCallProvider(Provider):
        async def stream(self, messages, system, tools=None):
            if self.prompts:
                async for event in super().stream(messages, system, tools):
                    yield event
                return
            self.prompts.append(system)
            yield MessageStartEvent(message_id="first")
            clock.current += timedelta(minutes=40)
            yield ToolUseStartEvent(tool_use_id="call", name="echo")
            yield ToolUseEndEvent(tool_use_id="call", name="echo", input={})
            yield MessageEndEvent(stop_reason="tool_use", usage=Usage())

    storage = SessionStorage(projects_dir=tmp_path / "projects")
    prompt_time = SessionPromptTime(storage, str(tmp_path), "session")
    provider = MultiCallProvider(clock)
    tools = ToolRegistry()
    tools.register(Echo())
    loop = AgentLoop(
        provider,
        "# Runtime Context\n- Current time: initial\n",
        tools,
        session_storage=storage,
        session_id="session",
        cwd=str(tmp_path),
        prompt_time=prompt_time,
    )
    await loop.run("Use the tool")
    assert len(provider.prompts) == 2
    assert all(_time(prompt).endswith("10:00:00") for prompt in provider.prompts)
    await loop.run("New input")
    assert _time(provider.prompts[-1]).endswith("10:40:00")


@pytest.mark.asyncio
async def test_only_real_injected_or_queued_input_renews_time(clock, tmp_path):
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    prompt_time = SessionPromptTime(storage, str(tmp_path), "session")
    prompt_time.begin_turn()
    loop = AgentLoop(
        Provider(clock),
        "# Runtime Context\n- Current time: initial\n",
        ToolRegistry(),
        session_storage=storage,
        session_id="session",
        cwd=str(tmp_path),
        prompt_time=prompt_time,
    )
    clock.current += timedelta(minutes=40)
    events = [event async for event in loop._submit_queued_inputs_after_tool_call(lambda: [" "])]
    assert events == []
    assert prompt_time.value.endswith("10:00:00")
    events = [event async for event in loop._submit_queued_inputs_after_tool_call(lambda: ["New input"])]
    assert len(events) == 1
    assert prompt_time.value.endswith("10:40:00")
    clock.current += timedelta(minutes=40)
    loop.inject_user_message("Supplement")
    loop._drain_pending_injections()
    assert prompt_time.value.endswith("11:20:00")


@pytest.mark.asyncio
async def test_pipeline_live_question_followups_and_step_rebuild_share_time(clock, tmp_path):
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    provider = Provider(clock, questions=True)
    runner = _pipeline(tmp_path, storage, provider)
    questions = 0
    async for event in runner.run("Help me plan nginx"):
        if isinstance(event, AskUserQuestionEvent):
            questions += 1
            clock.current += timedelta(minutes=20 if questions == 1 else 11)
            event.response_future.set_result({"selected_id": "a", "selected_label": "A", "free_text": ""})
    assert questions == 2
    assert len(provider.prompts) == 4
    assert [_time(prompt).split()[-1] for prompt in provider.prompts] == [
        "10:00:00",
        "10:00:00",
        "10:31:00",
        "10:31:00",
    ]
    # A new step executor must use the same clock rather than the wall clock (11:11).
    context = runner._step_executor.build_agent_loop_context(
        runner._loaded.steps[-1], runner.context, "session", user_message="Continue"
    )
    assert _time(context.agent_loop.system_prompt).endswith("10:31:00")


@pytest.mark.parametrize("mode", ["normal", "pipeline"])
@pytest.mark.parametrize("minutes", [20, 31])
@pytest.mark.asyncio
async def test_real_backup_restore_preserves_anchor_before_next_user_turn(clock, tmp_path, monkeypatch, mode, minutes):
    cwd = str(tmp_path)
    storage = SessionStorage(projects_dir=tmp_path / "original-projects")
    original = SessionPromptTime(storage, cwd, "session")
    original.begin_turn()
    storage.append(cwd, "session", Message(role="user", content="Hello"))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "backup"))
    backup = SessionBackupService(storage)
    backup.initialize_session(cwd, "session")
    result = backup.backup_session(cwd, "session", reason=BackupReason.NORMAL_TURN_END, critical=True)
    assert result.copied_files > 0
    restored_storage = SessionStorage(projects_dir=tmp_path / "restored-projects")
    restore = SessionBackupService(restored_storage).restore_session(cwd, "session")
    assert restore.restored and restore.copied_files > 0
    clock.current += timedelta(minutes=minutes)
    restored = SessionPromptTime(restored_storage, cwd, "session")
    assert restored.value == original.value
    provider = Provider(clock)
    if mode == "normal":
        loop = AgentLoop(
            provider,
            "# Runtime Context\n- Current time: old\n",
            ToolRegistry(),
            session_storage=restored_storage,
            session_id="session",
            cwd=cwd,
            prompt_time=restored,
        )
        await loop.run("Continue")
    else:
        runner = _pipeline(tmp_path, restored_storage, provider)
        async for _ in runner.run("Continue"):
            pass
    expected = "10:00:00" if minutes == 20 else "10:31:00"
    assert _time(provider.prompts[0]).endswith(expected)


@pytest.mark.parametrize("minutes", [20, 31])
@pytest.mark.asyncio
async def test_pipeline_restores_pending_question_with_session_prompt_time(clock, tmp_path, monkeypatch, minutes):
    storage = SessionStorage(projects_dir=tmp_path / "original-projects")
    runner = _pipeline(tmp_path, storage, Provider(clock, questions=True))
    questions = asyncio.Queue()

    async def consume():
        async for event in runner.run("Plan nginx"):
            if isinstance(event, AskUserQuestionEvent):
                await runner.persist_pending_ask_user_question(event)
                await questions.put(event)

    task = asyncio.create_task(consume())
    try:
        question = await asyncio.wait_for(questions.get(), timeout=5)
        monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "backup"))
        backup = SessionBackupService(storage)
        backup.initialize_session(str(tmp_path), "session")
        backup.backup_session(str(tmp_path), "session", reason=BackupReason.NORMAL_TURN_END, critical=True)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    restored_storage = SessionStorage(projects_dir=tmp_path / "restored-projects")
    result = SessionBackupService(restored_storage).restore_session(str(tmp_path), "session")
    assert result.restored and result.copied_files > 0
    clock.current += timedelta(minutes=minutes)
    provider = Provider(clock)
    resumed = _pipeline(tmp_path, restored_storage, provider, resume=True)
    assert resumed._prompt_time.value.endswith("10:00:00")
    pending = resumed.pending_ask_user_question()
    assert pending is not None
    async for _ in resumed.resume_ask_user_question(
        {"selected_id": "a", "selected_label": "A", "free_text": ""},
        tool_use_id=question.tool_use_id,
        pending_input=pending,
    ):
        pass
    expected = "10:00:00" if minutes == 20 else "10:31:00"
    assert len(provider.prompts) == 2
    assert all(_time(prompt).endswith(expected) for prompt in provider.prompts)


def test_sessions_are_isolated_and_passive_refresh_never_renews(clock, tmp_path):
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    first = SessionPromptTime(storage, str(tmp_path), "first")
    first.begin_turn()
    clock.current += timedelta(minutes=40)
    second = SessionPromptTime(storage, str(tmp_path), "second")
    second.begin_turn()
    assert first.value.endswith("10:00:00")
    assert second.value.endswith("10:40:00")
    prompt = "# Runtime Context\n- Current time: old\n\n# Skill\nDo not change this."
    assert first.apply(prompt) == prompt.replace("old", first.value)
    assert first.value.endswith("10:00:00")
