"""Responses wire history through the real manager, agent and JSONL storage."""

import json
from unittest.mock import AsyncMock

import pytest
from google.protobuf.json_format import MessageToDict

from iac_code.agent.agent_loop import AgentLoop
from iac_code.agent.message import Message, TextBlock, ToolResultBlock, ToolUseBlock
from iac_code.providers.base import ToolDefinition
from iac_code.providers.manager import ProviderManager
from iac_code.providers.responses_codec import ResponsesContextLimitError, decode_response, encode_input
from iac_code.providers.responses_provider import DashScopeResponsesProvider, ResponsesProvider
from iac_code.providers.retry import RetryConfig
from iac_code.services.session_storage import SessionStorage
from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from iac_code.types.permissions import PermissionResult
from iac_code.types.stream_events import CompactionEvent, ErrorEvent, MessageEndEvent, TombstoneEvent
from iac_code.web.events import WebEventTranslator
from iac_code.web.files import _visible_payload_for_message
from tests.providers._responses_fakes import FakeResponsesClient, call, message, reasoning, response, terminal


class Lookup(Tool):
    name = "lookup"
    description = "Read a fixture value"
    input_schema = {"type": "object", "properties": {"value": {"type": "integer"}}}

    def __init__(self):
        self.calls = []

    def is_read_only(self, input=None):
        return True

    async def check_permissions(self, input, context=None):
        return PermissionResult(behavior="allow")

    async def execute(self, *, tool_input: dict, context: ToolContext):
        self.calls.append(tool_input)
        return ToolResult.success("fixture result " + str(tool_input["value"]))


def setup_loop(tmp_path, client, profile="openai", **kwargs):
    model = "gpt-6-sol" if profile == "openai" else "qwen3.8-max"
    provider_cls = ResponsesProvider if profile == "openai" else DashScopeResponsesProvider
    client.base_url = (
        "https://api.openai.com/v1" if profile == "openai" else ("https://dashscope.aliyuncs.com/compatible-mode/v1")
    )
    provider = provider_cls(model=model, client=client)
    manager = ProviderManager(
        model=model,
        credentials={profile: "fake-key"},
        provider_key_override=profile,
        provider_config_override={"models": {model: {"apiMode": "responses"}}},
        ignore_llm_source=True,
        retry_config=RetryConfig(max_retries=0),
    )
    manager._provider = provider
    tool = Lookup()
    registry = ToolRegistry()
    registry.register(tool)
    storage = SessionStorage(tmp_path / "projects")
    loop = AgentLoop(
        provider_manager=manager,
        system_prompt="system",
        tool_registry=registry,
        cwd=str(tmp_path),
        session_storage=storage,
        session_id="responses-session",
        max_turns=2,
        **kwargs,
    )
    return loop, provider, tool, storage


def native_history(provider, count=5):
    """Several complete turns leave compactible history and intact tool pairs."""
    result = []
    for i in range(count):
        output = [reasoning(id=f"rs{i}"), message(f"checking {i}", id=f"msg{i}", phase="commentary"), call(f"c{i}")]
        native = decode_response(
            response(output),
            provider._responses_identity,
            [ToolDefinition("lookup", Lookup.description, Lookup.input_schema)],
        )
        result.extend(
            [
                Message(role="user", content=f"question {i}"),
                Message(
                    role="assistant",
                    content=[
                        TextBlock(text=f"checking {i}"),
                        ToolUseBlock(id=f"c{i}", name="lookup", input={"value": 1}),
                    ],
                    metadata=native.provider_metadata,
                ),
                Message(role="user", content=[ToolResultBlock(tool_use_id=f"c{i}", content="fixture result")]),
                Message(role="assistant", content="done"),
            ]
        )
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["openai", "dashscope"])
async def test_tools_native_items_round_trip_session_and_resume(tmp_path, profile):
    thought = reasoning() if profile == "openai" else reasoning(encrypted_content=None)
    output = [thought, message("checking", phase="commentary"), call(), call("call2", arguments='{"value": 2}')]
    client = FakeResponsesClient([terminal(response(output))], [terminal(response([message("done")]))])
    loop, provider, tool, storage = setup_loop(tmp_path, client, profile)
    events = [event async for event in loop.run_streaming("go")]
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert tool.calls == [{"value": 1}, {"value": 2}]
    ends = [event for event in events if isinstance(event, MessageEndEvent)]
    assert ends[0].usage.input_tokens == 20 and ends[0].usage.cache_read_input_tokens == 7
    assert ends[0].usage.provider == profile
    next_input = client.calls[1]["input"]
    assert next_input.count(thought) == 1
    assert next_input.count(output[1]) == 1
    for item in output[2:]:
        result = next(
            value
            for value in next_input
            if value.get("type") == "function_call_output" and value["call_id"] == item["call_id"]
        )
        assert next_input.count(item) == 1
        if profile == "dashscope":
            assert next_input[next_input.index(item) + 1] == result
    saved = storage.load(str(tmp_path), "responses-session")
    native_assistant = next(msg for msg in saved if msg.metadata.get("responses", {}).get("output") == output)
    current = next(msg for msg in loop.context_manager.get_messages() if msg.metadata == native_assistant.metadata)
    assert current.to_dict() == native_assistant.to_dict()
    assert "opaque-fixture" not in json.dumps(_visible_payload_for_message(native_assistant))
    assert "responses" not in WebEventTranslator("s").translate_stream_event(ends[0], turn_id="t")["payload"]
    from iac_code.a2a.events import publish_stream_event

    queue = AsyncMock()
    await publish_stream_event(queue, task_id="task", context_id="context", event=ends[0])
    assert "opaque-fixture" not in json.dumps(MessageToDict(queue.enqueue_event.call_args.args[0]))
    resumed_client = FakeResponsesClient([terminal(response([message("resumed")]))])
    resumed, _, _, _ = setup_loop(tmp_path, resumed_client, profile, resume_messages=saved)
    resumed_events = [event async for event in resumed.run_streaming("continue")]
    assert not any(isinstance(event, ErrorEvent) for event in resumed_events)
    assert resumed_client.calls[0]["input"].count(thought) == 1
    assert resumed_client.calls[0]["input"].count(output[1]) == 1
    # Changing a model/endpoint/profile falls back to visible text and tool blocks.
    for key in ("model", "endpoint", "profile", "provider"):
        changed = {**provider._responses_identity, key: "different"}
        converted = encode_input(resumed._get_provider_messages(), changed)
        assert not any(item.get("type") == "reasoning" or "phase" in item for item in converted)
        assert sum(item.get("type") == "function_call" for item in converted) == 2


@pytest.mark.asyncio
async def test_stream_failure_fallback_persists_only_validated_complete_items(tmp_path):
    partial = {"type": "response.output_text.delta", "delta": "discarded"}
    output = [reasoning(), message("fallback", phase="final_answer")]
    client = FakeResponsesClient([partial, RuntimeError("offline interrupted")], response(output))
    loop, _, tool, storage = setup_loop(tmp_path, client)
    events = [event async for event in loop.run_streaming("go")]
    assert any(isinstance(event, TombstoneEvent) for event in events)
    assert tool.calls == []
    assert [request["stream"] for request in client.calls] == [True, False]
    saved = storage.load(str(tmp_path), "responses-session")
    assert saved[-1].get_text() == "fallback"
    assert saved[-1].metadata["responses"]["output"] == output
    assert "discarded" not in json.dumps([msg.to_dict() for msg in saved])


@pytest.mark.asyncio
async def test_tool_end_before_failed_response_cannot_execute_or_persist(tmp_path):
    item = call()
    added = {"type": "response.output_item.added", "output_index": 0, "item": item}
    done = {"type": "response.output_item.done", "output_index": 0, "item": item}
    client = FakeResponsesClient([added, done, {"type": "response.failed"}], response([], status="failed"))
    loop, _, tool, storage = setup_loop(tmp_path, client)
    events = [event async for event in loop.run_streaming("go")]
    assert any(isinstance(event, ErrorEvent) for event in events)
    assert tool.calls == []
    saved = storage.load(str(tmp_path), "responses-session")
    assert not any(msg.role == "assistant" or msg.metadata.get("responses") for msg in saved)


@pytest.mark.asyncio
async def test_incomplete_turn_cannot_commit_or_execute_even_completed_function(tmp_path):
    item = call()
    incomplete = response(
        [message("partial"), item], status="incomplete", incomplete_details={"reason": "max_output_tokens"}
    )
    events = [
        {"type": "response.output_item.added", "output_index": 1, "item": item},
        {"type": "response.output_item.done", "output_index": 1, "item": item},
        terminal(incomplete),
    ]
    loop, _, tool, storage = setup_loop(tmp_path, FakeResponsesClient(events, incomplete))
    emitted = [event async for event in loop.run_streaming("go")]
    assert any(isinstance(event, ErrorEvent) for event in emitted)
    assert tool.calls == []
    assert not any(msg.role == "assistant" for msg in storage.load(str(tmp_path), "responses-session"))


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_again", [False, True])
async def test_context_limit_compacts_once_retries_and_keeps_tail_tool_pairs(tmp_path, fail_again):
    limit = ResponsesContextLimitError("offline context limit")
    retried = limit if fail_again else [terminal(response([message("retried")]))]
    client = FakeResponsesClient(limit, response([message("local summary")]), retried)
    loop, provider, _, storage = setup_loop(tmp_path, client)
    loop.context_manager.load_messages(native_history(provider))
    before = encode_input(loop._get_provider_messages(), provider._responses_identity)
    events = [event async for event in loop.run_streaming("new question")]
    assert [request["stream"] for request in client.calls] == [True, False, True]
    assert sum(isinstance(event, CompactionEvent) and event.phase == "started" for event in events) == 1
    assert any(isinstance(event, CompactionEvent) and event.summary == "local summary" for event in events)
    assert any(isinstance(event, ErrorEvent) for event in events) == fail_again
    after = client.calls[-1]["input"]
    assert len(after) < len(before)
    assert not any(item.get("id") == "rs0" for item in after)
    calls = {item["call_id"] for item in after if item.get("type") == "function_call"}
    results = {item["call_id"] for item in after if item.get("type") == "function_call_output"}
    assert calls and calls == results
    saved = storage.load(str(tmp_path), "responses-session")
    assert any(msg.metadata.get("responses", {}).get("output", [{}])[0].get("id") == "rs0" for msg in saved)


@pytest.mark.asyncio
async def test_context_limit_without_compactible_history_reports_error(tmp_path):
    client = FakeResponsesClient(ResponsesContextLimitError("offline limit"))
    loop, _, _, _ = setup_loop(tmp_path, client)
    events = [event async for event in loop.run_streaming("go")]
    assert len(client.calls) == 1
    assert any(isinstance(event, ErrorEvent) and event.context_limit_exceeded for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("input_fraction,output_limit", [(0.72, None), (0.65, 128_000)])
async def test_dashscope_proactive_compaction_precedes_service_truncation_threshold(
    tmp_path, input_fraction, output_limit
):
    client = FakeResponsesClient(response([message("summary")]), [terminal(response([message("done")]))])
    loop, provider, _, _ = setup_loop(tmp_path, client, "dashscope")
    provider._max_completion_tokens = output_limit
    loop.context_manager.load_messages(native_history(provider))
    original_window = loop.context_manager._config.context_window
    for msg in loop.context_manager.get_messages():
        msg.token_count = int(original_window * input_fraction / len(loop.context_manager.get_messages()))
    assert not loop.context_manager.needs_compaction()
    events = [event async for event in loop.run_streaming("go")]
    assert any(isinstance(event, CompactionEvent) and event.phase == "started" for event in events)
    assert [request["stream"] for request in client.calls] == [False, True]
    assert loop.context_manager._config.context_window == int(original_window * 0.75)


@pytest.mark.asyncio
async def test_dashscope_huge_tail_is_rejected_without_sending_truncated_history(tmp_path):
    client = FakeResponsesClient()
    loop, provider, _, _ = setup_loop(tmp_path, client, "dashscope")
    history = native_history(provider, count=1)
    loop.context_manager.load_messages(history)
    history[2].token_count = loop.context_manager.context_window
    events = [event async for event in loop.run_streaming("continue")]
    assert client.calls == []
    assert any(isinstance(event, ErrorEvent) and "safe context budget" in event.error for event in events)
    # No truncation or deletion of the large result or its matching function call.
    assert history[1] in loop.context_manager.get_messages()
    assert history[2] in loop.context_manager.get_messages()
