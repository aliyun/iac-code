"""Runtime progress stays separate from assistant content and survives visible replay."""

import pytest
from rich.console import Console

from iac_code.agent.message import Message
from iac_code.tools.base import ToolRegistry
from iac_code.types.stream_events import WikiSearchEvent
from iac_code.ui.renderer import Renderer
from iac_code.web.events import WebEventTranslator
from iac_code.web.session_manager import WebSessionManager


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["extracting", "searching", "ready", "skipped"])
async def test_cli_final_output_retains_completed_wiki_summaries_only(monkeypatch, tmp_path, phase):
    from iac_code.types.stream_events import MessageEndEvent, MessageStartEvent, TextDeltaEvent

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    console = Console(record=True, width=140, force_terminal=False)
    renderer = Renderer(console, ToolRegistry())

    async def events():
        yield WikiSearchEvent("q1", phase, snapshot(phase))
        yield MessageStartEvent(message_id="answer")
        yield TextDeltaEvent(text="Answer kept")
        yield MessageEndEvent(stop_reason="end_turn")

    await renderer.run_streaming_output(events(), permission_handler=None)
    archived = [segment for turn in renderer._message_history for segment in turn.segments]
    assert any("Answer kept" in segment.text for segment in archived)
    assert not any(segment.kind == "wiki_pending" for segment in archived)
    wiki = [segment for segment in archived if segment.kind == "wiki"]
    assert len(wiki) == (1 if phase in {"ready", "skipped"} else 0)
    output = console.export_text()
    assert "Extracting keywords" not in output and "Searching" not in output


def snapshot(status="ready"):
    return {
        "query_id": "q1",
        "dispatch_mode": "serial",
        "status": status,
        "keywords": ["B70"],
        "hits": [{"path": "common/hardware/intel-arc-pro-b70.md", "title": "Intel Arc Pro B70"}],
    }


def test_cli_history_displays_saved_summary_once_without_replaying_work():
    console = Console(record=True, width=140)
    renderer = Renderer(console, ToolRegistry())
    renderer.replay_history(
        [
            Message(role="user", content="Original B70 question", metadata={"wiki_auto_search": snapshot()}),
            Message(role="assistant", content="Saved answer"),
        ]
    )
    output = console.export_text()
    assert output.count("Original B70 question") == 1
    assert output.count("Saved answer") == 1
    assert output.count("Wiki") == 1
    assert "knowledge_search_results" not in output


def test_web_progress_is_a_context_event_bound_to_the_user_message():
    translator = WebEventTranslator("session-1")
    event = translator.translate_stream_event(WikiSearchEvent("q1", "extracting", snapshot("pending")), turn_id="t1")
    assert event["type"] == "context.wiki_search"
    assert event["payload"]["ownerMessageId"] == "user-t1"
    assert event["payload"]["queryId"] == "q1"
    assert event["payload"]["phase"] == "extracting"
    assert "content" not in event["payload"] and "messageId" not in event["payload"]


def test_web_steered_query_keeps_its_exact_owner():
    data = {**snapshot(), "owner_message_id": "user-t1-steer-abc"}
    event = WebEventTranslator("session-1").translate_stream_event(WikiSearchEvent("q1", "ready", data), turn_id="t1")
    assert event["payload"]["ownerMessageId"] == "user-t1-steer-abc"


def test_web_history_projects_snapshot_without_extra_assistant_message(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=cwd)
    session = manager.create_session(cwd=str(cwd), mode="normal")
    manager.storage.append(
        str(cwd),
        session.session_id,
        Message(role="user", content="Original B70 question", metadata={"wiki_auto_search": snapshot()}),
    )
    manager.storage.append(str(cwd), session.session_id, Message(role="assistant", content="Saved answer"))
    rows = manager.load_visible_transcript(session.session_id, cwd=str(cwd))["messages"]
    assert [(row["role"], row["content"]) for row in rows] == [
        ("user", "Original B70 question"),
        ("assistant", "Saved answer"),
    ]
    assert rows[0]["wikiSearch"]["queryId"] == "q1"
    assert rows[0]["wikiSearch"]["files"][0]["path"] == "common/hardware/intel-arc-pro-b70.md"


def test_pipeline_stage_progress_and_history_are_not_assistant_content():
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
    from iac_code.web.pipeline_transcript import PipelineTranscriptTranslator, build_pipeline_transcript_rows

    step = "solution_planning_and_selection"
    translator = PipelineEventTranslator(
        PipelineA2AContext(
            pipeline_run_id="pipeline-1",
            task_id="task-1",
            context_id="ctx-1",
            pipeline_name="selling_solution_first",
            parent_step_order=[step],
            candidate_step_order=[],
        )
    )
    start = PipelineEvent(PipelineEventType.STEP_STARTED, step, 1, {})
    finish = PipelineEvent(PipelineEventType.STEP_COMPLETED, step, 2, {})
    envelopes = translator.translate(start)
    envelopes += translator.translate(WikiSearchEvent("q1", "extracting", snapshot("pending")))
    envelopes += translator.translate(WikiSearchEvent("q1", "ready", snapshot()))
    envelopes += translator.translate(finish)
    events = PipelineTranscriptTranslator().translate_all(envelopes)
    assert not any(e["type"].startswith("assistant.") for e in events)
    wiki = [e for e in events if e["type"] == "context.wiki_search"]
    assert len(wiki) == 2 and wiki[0]["payload"]["ownerMessageId"] == wiki[1]["payload"]["ownerMessageId"]
    rows = build_pipeline_transcript_rows(envelopes)
    assert len(rows) == 1 and rows[0]["pipelineStep"]["wikiSearch"]["phase"] == "ready"
    interrupted = build_pipeline_transcript_rows(envelopes[:2])
    assert interrupted[0]["pipelineStep"]["wikiSearch"]["phase"] == "pending"
    assert "Interrupted" in interrupted[0]["pipelineStep"]["wikiSearch"]["summary"]
