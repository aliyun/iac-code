"""Wiki preparation must remain distinct from the answer's model and wire milestones."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from google.protobuf.json_format import MessageToDict

from iac_code.a2a.events import publish_stream_event
from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
from iac_code.agent.agent_loop import AgentLoop
from iac_code.agent.message import Message
from iac_code.providers.base import NonStreamingResponse
from iac_code.providers.manager import ProviderManager
from iac_code.services import first_token_timing as timing_module
from iac_code.services.wiki_auto_search import PROMPT
from iac_code.tools.base import ToolRegistry
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    Usage,
)
from tests.a2a.test_first_token_timing import Clock, frame, run_wire


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "pipeline"])
@pytest.mark.parametrize("new_session", [False, True])
@pytest.mark.parametrize("wiki_enabled", [False, True])
async def test_wiki_does_not_claim_answer_milestones(mode, new_session, wiki_enabled, monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_WIKI_AUTO_SEARCH", str(wiki_enabled).lower())
    monkeypatch.setenv("IAC_CODE_AUTO_MEMORY", "false")
    clock = Clock()
    monkeypatch.setattr(timing_module.time, "monotonic_ns", clock)
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(json.loads(record.getMessage()))

    monkeypatch.setattr(timing_module.logger, "handlers", [Capture()])
    monkeypatch.setattr(timing_module.logger, "propagate", False)
    caplog.set_level(logging.INFO, logger=timing_module.logger.name)

    class Provider:
        _PROVIDER_KEY = "anthropic"
        _logical_provider_key = "anthropic"

        async def complete(self, messages, system, tools=None, **kwargs):
            assert system == PROMPT
            clock.advance(100)
            return NonStreamingResponse(
                message_id="wiki-response",
                text='{"keywords":["ECS"]}',
                tool_uses=[],
                stop_reason="end_turn",
                usage=Usage(),
            )

        async def stream(self, messages, system, tools=None, **kwargs):
            yield MessageStartEvent(message_id="main-response")
            # A follow-up extracts concurrently; let that auxiliary call finish first.
            await asyncio.sleep(0)
            clock.advance(200)
            yield ThinkingDeltaEvent(text="fixture hidden reasoning")
            clock.advance(5)
            yield TextDeltaEvent(text="fixture answer")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    async def local_search(*args, **kwargs):
        clock.advance(50)
        return []

    monkeypatch.setattr("iac_code.providers.manager.create_provider", lambda *args, **kwargs: Provider())
    monkeypatch.setattr("iac_code.providers.manager._MODEL_REFUSAL_FALLBACK_MAP", {})
    monkeypatch.setattr("iac_code.services.wiki_auto_search.search", local_search)
    manager = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "fake"}, ignore_llm_source=True)
    loop = AgentLoop(
        provider_manager=manager,
        system_prompt="Answer briefly",
        tool_registry=ToolRegistry(),
        cwd=str(tmp_path),
        session_id="fixture-session",
        pipeline_mode=mode == "pipeline",
        wiki_search_context={"enabled": True, "scope_id": "fixture-step"},
        resume_messages=None
        if new_session
        else [Message(role="user", content="prior question"), Message(role="assistant", content="prior reply")],
    )
    translator = PipelineEventTranslator(
        PipelineA2AContext(
            pipeline_run_id="run-1",
            task_id="task-1",
            context_id="ctx-1",
            pipeline_name="selling_solution_first",
        )
    )

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})

        async def emit(payload):
            await send({"type": "http.response.body", "body": frame(payload), "more_body": True})

        class Queue:
            async def enqueue_event(self, event):
                await emit({"result": {"statusUpdate": MessageToDict(event)}})

        async for event in loop.run_streaming("Explain ECS billing"):
            # Match the default A2A exposure: model thinking is measured but not sent.
            if isinstance(event, ThinkingDeltaEvent):
                continue
            if mode == "normal":
                await publish_stream_event(Queue(), task_id="task-1", context_id="ctx-1", event=event)
            else:
                for envelope in translator.translate(event):
                    await emit({"result": {"statusUpdate": {"metadata": {"iac_code": {"pipeline": envelope}}}}})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    await run_wire([], app=app, new_session=new_session)
    primary = next(record for record in records if record["stage"] == "first_token_summary")
    model_summaries = [record for record in records if record["stage"] == "first_model_content_summary"]
    content_summaries = [record for record in records if record["stage"] == "first_content_summary"]
    wiki_ms = 150 if wiki_enabled else 0
    assert primary["elapsed_ms"] == wiki_ms + 205
    assert primary["first_event_type"] == "text"
    if new_session:
        assert len(model_summaries) == len(content_summaries) == 1
        assert model_summaries[0]["elapsed_ms"] == wiki_ms + 200
        assert model_summaries[0]["first_model_content_type"] == "thinking"
        assert content_summaries[0]["first_content_type"] == "text"
        assert content_summaries[0]["model_content_to_send_ms"] == 5
    else:
        assert not model_summaries and not content_summaries
    if wiki_enabled:
        assert primary["phase_totals_ms"]["wiki_keyword_extract"] == 100
        assert primary["phase_totals_ms"]["wiki_keyword_extract_request"] == 100
        assert primary["phase_totals_ms"]["wiki_search"] == 50
        auxiliary = [event for event in primary["timeline"] if event.get("llm_call_purpose")]
        assert auxiliary and all(event["llm_call_purpose"] == "wiki_keyword_extract" for event in auxiliary)
        assert all(not event["stage"].startswith("llm_") for event in auxiliary)
        if new_session:
            assert primary["milestones_ms"]["llm_request_started"] == wiki_ms
    else:
        assert not any(stage.startswith("wiki_") for stage in primary["phase_totals_ms"])
    assert "fixture hidden reasoning" not in json.dumps(records) and "fixture answer" not in json.dumps(records)
