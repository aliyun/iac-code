import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from iac_code.agent.message import Message
from iac_code.pipeline.config import RunMode
from iac_code.pipeline.engine.display_replay import (
    DisplayAttempt,
    DisplayCandidate,
    DisplayCandidateSelection,
    DisplayReplayModel,
    PipelineDisplayRecorder,
    load_display_events,
)
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.types.stream_events import CandidateDetailEvent, DiagramEvent, ToolUseStartEvent


async def _drain_streaming_output(events_iter, **_kwargs):
    async for _event in events_iter:
        pass


def _make_repl_with_display_recorder(tmp_path):
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._pipeline_display_recorder = PipelineDisplayRecorder(tmp_path / "pipeline" / "display.jsonl")
    repl._pipeline_display_current_step_id = None
    repl._pipeline = MagicMock()
    repl._pipeline.pause_agent_loops = MagicMock()
    repl._pipeline.resume_agent_loops = MagicMock()
    repl._pipeline_step_names = []
    repl._pipeline_completed_indices = set()
    repl._build_progress_bar = MagicMock(return_value="")
    repl._update_pipeline_state_from_event = MagicMock()
    repl._render_pipeline_event = MagicMock()
    repl._restart_pipeline_stream_after_interrupt = MagicMock()
    repl.renderer = MagicMock()
    repl.renderer.run_streaming_output = _drain_streaming_output
    repl.renderer.prompt_permission = None
    return repl


async def _simple_pipeline_stream():
    yield PipelineEvent(
        type=PipelineEventType.PIPELINE_STARTED,
        step_id=None,
        timestamp=time.time(),
        data={"pipeline_type": "selling", "step_names": ["intent_parsing"]},
    )
    yield PipelineEvent(
        type=PipelineEventType.STEP_STARTED,
        step_id="intent_parsing",
        timestamp=time.time(),
        data={"index": 1, "total": 1, "name": "intent_parsing"},
    )
    yield ToolUseStartEvent(tool_use_id="tu_1", name="complete_step")
    yield PipelineEvent(
        type=PipelineEventType.STEP_COMPLETED,
        step_id="intent_parsing",
        timestamp=time.time(),
        data={"duration_s": 1.0},
    )
    yield PipelineEvent(
        type=PipelineEventType.PIPELINE_COMPLETED,
        step_id=None,
        timestamp=time.time(),
        data={"total_steps": 1},
    )


@pytest.mark.asyncio
async def test_pipeline_stream_records_display_transcript(tmp_path):
    repl = _make_repl_with_display_recorder(tmp_path)

    await repl._render_pipeline_stream(_simple_pipeline_stream())

    events = load_display_events(tmp_path / "pipeline" / "display.jsonl")
    assert [event["type"] for event in events] == [
        "pipeline_started",
        "step_started",
        "tool_used",
        "step_completed",
        "pipeline_completed",
    ]
    assert events[0]["pipeline_name"] == "selling"
    assert events[2]["step_id"] == "intent_parsing"
    assert events[2]["payload"]["name"] == "complete_step"


def test_user_aborted_pipeline_records_display_terminal_event(tmp_path):
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._pipeline_display_recorder = PipelineDisplayRecorder(tmp_path / "pipeline" / "display.jsonl")
    repl._runtime_mode = RunMode.PIPELINE
    repl._original_cwd = "/workspace"
    repl._session_id = "sid"
    repl._session_storage = MagicMock()
    repl._session_storage.load.return_value = [Message(role="user", content="start")]
    repl._session_storage.repair_interrupted.side_effect = lambda messages: messages
    repl.current_git_branch = MagicMock(return_value="main")
    repl.renderer = MagicMock()
    repl.renderer.print_system_message = MagicMock()
    repl._agent_loop = MagicMock()
    repl._agent_loop.replace_session = MagicMock()
    repl._agent_loop.context_manager.add_raw_message.return_value = Message(
        role="assistant",
        content=InlineREPL._pipeline_abort_notice_text(),
    )

    repl._switch_user_aborted_pipeline_to_normal()

    events = load_display_events(tmp_path / "pipeline" / "display.jsonl")
    assert events[-1]["type"] == "pipeline_user_aborted"


def test_candidate_selected_records_display_event(tmp_path):
    repl = _make_repl_with_display_recorder(tmp_path)
    repl._pipeline_display_current_step_id = "confirm_and_select"

    repl._record_pipeline_display_candidate_selected(
        step_id="confirm_and_select",
        candidate_name="低成本方案",
        candidate_index=0,
    )

    events = load_display_events(tmp_path / "pipeline" / "display.jsonl")
    assert events == [
        {
            "payload": {"candidate_index": 0, "candidate_name": "低成本方案"},
            "step_id": "confirm_and_select",
            "timestamp": events[0]["timestamp"],
            "type": "candidate_selected",
            "version": 1,
        }
    ]


def test_normal_resume_replays_pipeline_before_abort_notice():
    from io import StringIO

    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    output = StringIO()
    repl.console = Console(file=output, width=100, force_terminal=False)
    repl.renderer = MagicMock()
    repl._load_pipeline_display_replay_model = MagicMock(
        return_value=DisplayReplayModel(
            pipeline_name="selling",
            interrupted=True,
            attempts=[DisplayAttempt(step_id="intent_parsing", attempt_no=1, index=1, total=5, status="interrupted")],
        )
    )

    def fake_replay_history(messages):
        for message in messages:
            repl.console.print(f"CHAT:{message.role}:{message.content}")

    repl.renderer.replay_history.side_effect = fake_replay_history

    repl._replay_resume_messages(
        [
            Message(role="user", content="选择一个已有vpc，创建一个vswitch"),
            Message(role="assistant", content=InlineREPL._pipeline_abort_notice_text()),
        ]
    )

    text = output.getvalue()
    assert text.index("CHAT:user:选择一个已有vpc") < text.index("AI Selling Pipeline")
    assert text.index("AI Selling Pipeline") < text.index("CHAT:assistant:")


def test_normal_resume_replays_completed_pipeline_before_handoff_context():
    from io import StringIO

    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    output = StringIO()
    repl.console = Console(file=output, width=100, force_terminal=False)
    repl.renderer = MagicMock()
    repl._load_pipeline_display_replay_model = MagicMock(
        return_value=DisplayReplayModel(
            pipeline_name="selling",
            completed=True,
            attempts=[DisplayAttempt(step_id="intent_parsing", attempt_no=1, index=1, total=5, status="completed")],
        )
    )

    def fake_replay_history(messages):
        for message in messages:
            repl.console.print(f"CHAT:{message.role}:{message.content}")

    repl.renderer.replay_history.side_effect = fake_replay_history

    repl._replay_resume_messages(
        [
            Message(role="user", content="选择一个已有vpc，创建一个vswitch"),
            Message(role="user", content="[Pipeline Handoff Context]\nPipeline: selling\nOutcome: completed"),
            Message(role="user", content="你刚才创建了什么？"),
            Message(role="assistant", content="创建了 VSwitch"),
        ]
    )

    text = output.getvalue()
    assert text.index("CHAT:user:选择一个已有vpc") < text.index("AI Selling Pipeline")
    assert text.index("AI Selling Pipeline") < text.index("CHAT:user:你刚才创建了什么？")
    assert "[Pipeline Handoff Context]" not in text


def test_normal_resume_replays_pipeline_after_last_pipeline_prompt_when_session_has_history():
    from io import StringIO

    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    output = StringIO()
    repl.console = Console(file=output, width=100, force_terminal=False)
    repl.renderer = MagicMock()
    repl._load_pipeline_display_replay_model = MagicMock(
        return_value=DisplayReplayModel(
            pipeline_name="selling",
            interrupted=True,
            attempts=[DisplayAttempt(step_id="architecture_planning", attempt_no=1, index=2, total=5)],
        )
    )

    def fake_replay_history(messages):
        for message in messages:
            repl.console.print(f"CHAT:{message.role}:{message.content}")

    repl.renderer.replay_history.side_effect = fake_replay_history

    repl._replay_resume_messages(
        [
            Message(role="user", content="前面普通聊天"),
            Message(role="assistant", content="普通回复"),
            Message(role="user", content="选择一个已有vpc，创建一个vswitch"),
            Message(role="assistant", content=InlineREPL._pipeline_abort_notice_text()),
        ]
    )

    text = output.getvalue()
    assert text.index("CHAT:assistant:普通回复") < text.index("CHAT:user:选择一个已有vpc")
    assert text.index("CHAT:user:选择一个已有vpc") < text.index("AI Selling Pipeline")
    assert text.index("AI Selling Pipeline") < text.index(InlineREPL._pipeline_abort_notice_text())


def test_swap_session_uses_pipeline_display_replay():
    from io import StringIO

    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    output = StringIO()
    repl.console = Console(file=output, width=100, force_terminal=False)
    repl.renderer = MagicMock()
    repl._original_cwd = "/workspace"
    repl._session_id = "old-session"
    repl._session_storage = MagicMock()
    messages = [
        Message(role="user", content="选择一个已有vpc，创建一个vswitch"),
        Message(role="assistant", content=InlineREPL._pipeline_abort_notice_text()),
    ]
    repl._session_storage.load.return_value = messages
    repl._session_storage.repair_interrupted.side_effect = lambda loaded: loaded
    repl._agent_loop = MagicMock()
    repl._load_current_session_name = MagicMock(return_value=None)
    repl.store = MagicMock()
    repl.store.get_state.return_value = SimpleNamespace(model="test-model", cwd="/workspace")

    def load_display_model():
        assert repl._session_id == "new-session"
        return DisplayReplayModel(
            pipeline_name="selling",
            interrupted=True,
            attempts=[DisplayAttempt(step_id="intent_parsing", attempt_no=1, index=1, total=5)],
        )

    repl._load_pipeline_display_replay_model = MagicMock(side_effect=load_display_model)

    def fake_replay_history(history):
        for message in history:
            repl.console.print(f"CHAT:{message.role}:{message.content}")

    repl.renderer.replay_history.side_effect = fake_replay_history

    repl.swap_session("new-session")

    text = output.getvalue()
    assert repl._session_id == "new-session"
    assert text.index("CHAT:user:选择一个已有vpc") < text.index("AI Selling Pipeline")
    assert text.index("AI Selling Pipeline") < text.index(InlineREPL._pipeline_abort_notice_text())


@pytest.mark.asyncio
async def test_resume_waiting_candidate_selection_uses_candidate_selection_event_stream():
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._pipeline = MagicMock()
    repl._pipeline_display_current_step_id = None
    repl._load_pipeline_display_replay_model = MagicMock(
        return_value=DisplayReplayModel(
            pipeline_name="selling",
            attempts=[
                DisplayAttempt(
                    step_id="confirm_and_select",
                    attempt_no=1,
                    status="waiting_input",
                    ui_mode="candidate_selection",
                    candidate_selection=DisplayCandidateSelection(
                        state="waiting",
                        prompt="请选择一个方案",
                        options=[{"name": "低成本方案", "summary": "单 ECS", "candidate_index": 0}],
                        candidates={
                            0: DisplayCandidate(
                                name="低成本方案",
                                candidate_index=0,
                                mermaid_source="graph TD\n A-->B",
                                summary="单 ECS",
                                cost_items=[{"name": "ECS", "spec": "1C1G", "monthly_cost": "¥30/月"}],
                                total_monthly_cost="¥30/月",
                            )
                        },
                    ),
                )
            ],
        )
    )
    seen = []

    async def fake_render(event_stream, progress_bar_fn=None):
        async for event in event_stream:
            seen.append(event)
        return None

    repl._render_candidate_selection_tabs = fake_render

    await repl._resume_waiting_candidate_selection_from_sidecar()

    assert isinstance(seen[0], DiagramEvent)
    assert seen[0].candidate_name == "低成本方案"
    assert isinstance(seen[1], CandidateDetailEvent)
    assert seen[1].candidate_name == "低成本方案"
    assert seen[1].total_monthly_cost == "¥30/月"
    assert seen[2].type == PipelineEventType.USER_INPUT_REQUIRED
    assert seen[2].data["options"][0]["name"] == "低成本方案"


def test_load_pipeline_display_replay_model_allows_completed_terminal_status(tmp_path, monkeypatch):
    from iac_code.pipeline.engine.session import PipelineIdentity, PipelineSession
    from iac_code.services.session_storage import SessionStorage
    from iac_code.ui.repl import InlineREPL

    cwd = "/workspace"
    session_id = "sid"
    storage = SessionStorage(projects_dir=tmp_path)
    sidecar_dir = storage.session_dir(cwd, session_id) / "pipeline"
    PipelineSession(sidecar_dir).save_completed_sync(
        "intent_parsing",
        {"current_index": 1, "rollback_count": 0, "interrupt_rollback_count": 0, "step_statuses": {}},
        {},
        PipelineIdentity(
            pipeline_name="selling",
            step_ids=["intent_parsing"],
            sub_pipeline_step_ids={},
            pipeline_fingerprint="fp",
        ),
    )
    recorder = PipelineDisplayRecorder(sidecar_dir / "display.jsonl")
    recorder.record(
        "pipeline_started",
        pipeline_name="selling",
        payload={"pipeline_type": "selling", "step_names": ["intent_parsing"], "total_steps": 1},
    )
    recorder.record(
        "step_started",
        step_id="intent_parsing",
        payload={"index": 1, "total": 1, "name": "intent_parsing", "step_type": "normal"},
    )
    recorder.record("step_completed", step_id="intent_parsing", payload={"duration_s": 1.0})
    recorder.record("pipeline_completed", payload={"total_steps": 1})

    repl = InlineREPL.__new__(InlineREPL)
    repl._original_cwd = cwd
    repl._session_id = session_id
    repl._session_storage = storage
    monkeypatch.delenv("IAC_CODE_CWD", raising=False)

    model = repl._load_pipeline_display_replay_model()

    assert model is not None
    assert model.completed is True
    assert [attempt.step_id for attempt in model.attempts] == ["intent_parsing"]


def test_startup_display_replay_renders_pipeline_history_without_waiting_selection_snapshot():
    from io import StringIO

    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    output = StringIO()
    repl.console = Console(file=output, width=100, force_terminal=False)
    repl.renderer = MagicMock()
    repl.renderer.replay_history.side_effect = lambda messages: [
        repl.console.print(f"CHAT:{message.role}:{message.content}") for message in messages
    ]
    repl._session_storage = MagicMock()
    repl._session_storage.load.return_value = [Message(role="user", content="选择一个已有vpc，创建一个vswitch")]
    repl._session_storage.repair_interrupted.side_effect = lambda messages: messages
    repl._original_cwd = "/workspace"
    repl._session_id = "sid"
    repl._pipeline = MagicMock()
    repl._pipeline.state_machine._order = ["intent_parsing", "architecture_planning", "confirm_and_select"]
    repl._render_pipeline_display_transcript_window = MagicMock(return_value=None)
    repl._load_pipeline_display_transcript_messages = MagicMock(return_value=[])
    repl._load_pipeline_display_replay_model = MagicMock(
        return_value=DisplayReplayModel(
            pipeline_name="selling",
            attempts=[
                DisplayAttempt(
                    step_id="intent_parsing",
                    attempt_no=1,
                    index=1,
                    total=3,
                    status="completed",
                ),
                DisplayAttempt(
                    step_id="architecture_planning",
                    attempt_no=1,
                    index=2,
                    total=3,
                    status="completed",
                ),
                DisplayAttempt(
                    step_id="confirm_and_select",
                    attempt_no=1,
                    index=3,
                    total=3,
                    status="waiting_input",
                    ui_mode="candidate_selection",
                    candidate_selection=DisplayCandidateSelection(
                        state="waiting",
                        prompt="请选择一个方案",
                        options=[{"name": "低成本方案", "summary": "单 ECS", "candidate_index": 0}],
                        candidates={0: DisplayCandidate(name="低成本方案", candidate_index=0, summary="单 ECS")},
                    ),
                ),
            ],
        )
    )

    repl._render_pipeline_display_replay_on_startup()

    text = output.getvalue()
    assert "CHAT:user:选择一个已有vpc" in text
    assert "AI Selling Pipeline" in text
    assert "Intent parsing (1/3)" in text
    assert "Architecture planning (2/3)" in text
    assert "Confirm and select (3/3)" in text
    assert "低成本方案" not in text
    assert repl._pipeline_step_names == ["intent_parsing", "architecture_planning", "confirm_and_select"]
    assert repl._pipeline_completed_indices == {0, 1}


def test_pipeline_display_transcript_window_keeps_recent_lines():
    from iac_code.ui.repl import InlineREPL

    text = "\n".join(f"line {index}" for index in range(10))

    assert InlineREPL._tail_pipeline_display_window(text, max_lines=4) == "line 6\nline 7\nline 8\nline 9"


def test_pipeline_progress_bar_uses_display_step_names():
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)

    progress = repl._build_progress_bar(
        ["intent_parsing", "architecture_planning", "evaluate_candidates", "confirm_and_select", "deploying"],
        completed={0, 1, 2, 3},
        current_index=4,
        spinner_frame=3,
    )

    assert progress.plain == (
        "✓ Intent parsing → ✓ Architecture planning → ✓ Evaluate candidates → ✓ Confirm and select → ◒ Deploying"
    )
