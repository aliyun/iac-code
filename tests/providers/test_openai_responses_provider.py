import asyncio
import copy
import json

import httpx
import pytest
from openai import AsyncOpenAI, BadRequestError

import iac_code.providers.openai_provider as openai_provider
from iac_code.providers.base import ContentBlock, Message, ToolDefinition
from iac_code.providers.manager import create_provider
from iac_code.providers.request_headers import use_provider_request_headers
from iac_code.providers.responses_codec import (
    ResponsesContextLimitError,
    ResponsesProtocolError,
    ResponsesStreamAdapter,
)
from iac_code.providers.responses_provider import ResponsesProvider
from iac_code.types.stream_events import MessageEndEvent, TextDeltaEvent, ThinkingDeltaEvent, ToolUseEndEvent
from tests.providers._responses_fakes import FakeResponsesClient, call, message, reasoning, response, terminal

TOOLS = [ToolDefinition("lookup", "Look up a value", {"type": "object", "properties": {"value": {"type": "integer"}}})]


def provider(client, **kwargs):
    return ResponsesProvider(model="gpt-6-sol", client=client, **kwargs)


@pytest.mark.asyncio
async def test_request_converts_images_tools_effort_and_usage():
    client = FakeResponsesClient(response([reasoning(), call(), message("checking", phase="commentary")]))
    p = provider(client, effort="high", max_completion_tokens=321)
    messages = [
        Message(
            role="user",
            content=[
                ContentBlock(type="text", text="inspect"),
                ContentBlock(type="image", media_type="image/png", data="ZmFrZQ=="),
            ],
        )
    ]
    with use_provider_request_headers({"X-Trace": "fake"}):
        result = await p.complete(messages, "system", TOOLS, max_tokens=100)
    request = client.calls[0]
    assert request["store"] is False and request["stream"] is False
    assert request["instructions"] == "system"
    assert request["reasoning"] == {"effort": "high"}
    assert request["max_output_tokens"] == 321
    assert request["extra_headers"] == {"X-Trace": "fake"}
    assert request["include"] == ["reasoning.encrypted_content"]
    assert request["input"][0]["content"][1] == {"type": "input_image", "image_url": "data:image/png;base64,ZmFrZQ=="}
    assert request["tools"][0]["name"] == "lookup" and request["tools"][0]["strict"] is False
    assert "messages" not in request and "reasoning_effort" not in request
    assert result.stop_reason == "tool_use" and result.tool_uses[0]["input"] == {"value": 1}
    assert result.usage.normalized_total_tokens == 25
    assert result.usage.cache_read_input_tokens == 7 and result.usage.reported
    assert result.provider_metadata["responses"]["output"][2]["phase"] == "commentary"


@pytest.mark.asyncio
async def test_real_sdk_posts_responses_with_extension_fields_offline():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "object": "response",
                "created_at": 1,
                "model": "gpt-6-sol",
                **response([reasoning(), message(phase="final_answer")]),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
        async with AsyncOpenAI(api_key="fake", http_client=http_client) as sdk:
            result = await provider(sdk).complete([Message.user("hi")], "system")
    assert requests[0].url.path == "/v1/responses"
    assert result.provider_metadata["responses"]["output"][1]["phase"] == "final_answer"
    assert result.provider_metadata["responses"]["output"][0]["encrypted_content"] == "opaque-fixture"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "key,model,base,path,profile",
    [
        ("azure_openai", "my-deployment", "https://resource.openai.azure.com/openai/v1", "/openai/v1", "openai"),
        ("openai_compatible", "custom-model", "http://localhost:8765/custom/v1", "/custom/v1", "openai"),
        ("openai", "custom-model", "https://proxy.example.test/v1", "/v1", "openai"),
        (
            "dashscope_token_plan",
            "qwen3.8-max",
            "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "/compatible-mode/v1",
            "dashscope",
        ),
        (
            "openai_compatible",
            "qwen3.8-max",
            "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "/compatible-mode/v1",
            "dashscope",
        ),
    ],
)
async def test_configured_transports_use_real_sdk_for_two_tool_turns_offline(
    monkeypatch, streaming, key, model, base, path, profile
):
    requests = []
    first = response([reasoning(encrypted_content="opaque-fixture" if profile == "openai" else None), call()])
    if profile == "dashscope":
        del first["output"][0]["encrypted_content"]
    second = response()

    def handle(request):
        requests.append(request)
        data = first if len(requests) == 1 else second
        if not streaming:
            return httpx.Response(200, json=data)
        events = tool_events([call()]) if len(requests) == 1 else []
        for event in events:
            event["output_index"] += 1
        events.append(terminal(data))
        body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
        monkeypatch.setattr(
            openai_provider, "AsyncOpenAI", lambda **kwargs: AsyncOpenAI(http_client=http_client, **kwargs)
        )
        p = create_provider(
            model,
            {key: "fake"},
            provider_key_override=key,
            provider_config_override={"apiBase": base, "models": {model: {"apiMode": "responses"}}},
        )
        messages = [Message.user("go")]
        if streaming:
            events = [event async for event in p.stream(messages, "system", TOOLS)]
            assert [event.input for event in events if isinstance(event, ToolUseEndEvent)] == [{"value": 1}]
            result = events[-1]
        else:
            result = await p.complete(messages, "system", TOOLS)
            assert result.tool_uses[0]["input"] == {"value": 1}
        history = [
            *messages,
            Message(role="assistant", metadata=result.provider_metadata),
            Message.tool_result(tool_use_id="call1", content="found"),
        ]
        if streaming:
            result = [event async for event in p.stream(history, "system", TOOLS)][-1]
        else:
            result = await p.complete(history, "system", TOOLS)
        assert result.stop_reason == "end_turn"
        await p._client.close()
    assert len(requests) == 2 and all(request.url.path == path + "/responses" for request in requests)
    assert all(request.headers["authorization"] == "Bearer fake" for request in requests)
    kwargs = json.loads(requests[1].content)
    assert kwargs["model"] == model and kwargs["store"] is False
    assert kwargs["input"][1 : 1 + len(first["output"])] == first["output"]
    assert kwargs["input"][-1]["call_id"] == "call1"
    if profile == "openai":
        assert kwargs["include"] == ["reasoning.encrypted_content"]
        assert kwargs["tools"][0]["strict"] is False
    else:
        assert "include" not in kwargs and "strict" not in kwargs["tools"][0]


@pytest.mark.asyncio
async def test_unsupported_service_returns_error_without_chat_downgrade(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(400, json={"error": {"message": "Responses is not supported", "code": "unsupported_api"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
        monkeypatch.setattr(
            openai_provider, "AsyncOpenAI", lambda **kwargs: AsyncOpenAI(http_client=http_client, **kwargs)
        )
        p = create_provider(
            "custom-model",
            {"aliyun_codingplan": "fake"},
            provider_key_override="aliyun_codingplan",
            provider_config_override={"models": {"custom-model": {"apiMode": "responses"}}},
        )
        with pytest.raises(BadRequestError):
            await p.complete([Message.user("go")], "system")
        await p._client.close()
    assert len(requests) == 1 and requests[0].url.path == "/v1/responses"


@pytest.mark.asyncio
async def test_native_history_is_replayed_once_and_scope_checked():
    original = response([reasoning(), message("checking", phase="commentary"), call()])
    client = FakeResponsesClient(original, response(), response())
    p = provider(client)
    result = await p.complete([Message.user("go")], "system", TOOLS)
    assistant = Message(
        role="assistant",
        content=[
            ContentBlock(type="text", text="checking"),
            ContentBlock(type="tool_use", tool_use_id="call1", name="lookup", input={"value": 1}),
        ],
        metadata=result.provider_metadata,
    )
    history = [Message.user("go"), assistant, Message.tool_result(tool_use_id="call1", content="found")]
    before = copy.deepcopy(assistant.metadata)
    await p.complete(history, "system", TOOLS)
    assert client.calls[1]["input"][1:4] == original["output"]
    assert assistant.metadata == before
    other = ResponsesProvider(model="gpt-6-luna", client=client)
    await other.complete(history, "system", TOOLS)
    assert not any(item.get("type") == "reasoning" for item in client.calls[2]["input"])
    assert not any("phase" in item for item in client.calls[2]["input"])


def tool_events(items):
    events = []
    for index, item in enumerate(items):
        events.append(
            {
                "type": "response.output_item.added",
                "output_index": index,
                "item": {**item, "status": "in_progress", "arguments": ""},
            }
        )
    for index, item in enumerate(items):
        parts = [item["arguments"][:7], item["arguments"][7:]]
        events.extend(
            {
                "type": "response.function_call_arguments.delta",
                "output_index": index,
                "item_id": item["id"],
                "delta": part,
            }
            for part in parts
        )
        events.append(
            {
                "type": "response.function_call_arguments.done",
                "output_index": index,
                "item_id": item["id"],
                "arguments": item["arguments"],
            }
        )
        events.append({"type": "response.output_item.done", "output_index": index, "item": item})
    return events


@pytest.mark.asyncio
async def test_stream_multiple_functions_and_terminal_metadata():
    calls = [call(), call("call2", arguments='{"value": 2}')]
    data = response([*calls, reasoning(), message("checking")])
    client = FakeResponsesClient(
        [
            *tool_events(calls),
            {"type": "response.reasoning_summary_text.delta", "delta": "thought"},
            {"type": "response.output_text.delta", "delta": "checking"},
            terminal(data),
        ]
    )
    events = [event async for event in provider(client).stream([Message.user("go")], "system", TOOLS)]
    assert [event.input for event in events if isinstance(event, ToolUseEndEvent)] == [{"value": 1}, {"value": 2}]
    assert "".join(event.text for event in events if isinstance(event, ThinkingDeltaEvent)) == "thought"
    assert events[-1].provider_metadata["responses"]["output"] == data["output"]
    assert isinstance(events[-1], MessageEndEvent) and client.streams[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_refusal_is_not_empty_success(streaming):
    data = response([{**message(), "content": [{"type": "refusal", "refusal": "declined"}]}])
    client = FakeResponsesClient(
        [
            {"type": "response.refusal.delta", "delta": "declined"},
            {"type": "response.refusal.done", "refusal": "declined"},
            terminal(data),
        ]
        if streaming
        else data
    )
    p = provider(client)
    if streaming:
        result = [event async for event in p.stream([Message.user("go")], "system")][-1]
    else:
        result = await p.complete([Message.user("go")], "system")
    assert result.stop_reason == "refusal" and result.provider_metadata == {}


@pytest.mark.asyncio
async def test_incomplete_text_has_no_native_history():
    data = response([message("partial")], status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    client = FakeResponsesClient([terminal(data)])
    events = [event async for event in provider(client).stream([Message.user("go")], "system")]
    assert events[-1].stop_reason == "max_tokens" and events[-1].provider_metadata == {}
    assert any(isinstance(event, TextDeltaEvent) and event.text == "partial" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        response([]),
        response([call(arguments="{")]),
        response([call(name="unknown")]),
        response([call(), call()]),
        response([{"type": "web_search_call"}]),
        response(status="failed"),
        response([call(status="in_progress")], status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
        response([call()], status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
        response(status="incomplete", incomplete_details={"reason": "content_filter"}),
    ],
)
async def test_unusable_response_cannot_complete(data):
    with pytest.raises(ResponsesProtocolError):
        await provider(FakeResponsesClient(data)).complete([Message.user("go")], "system", TOOLS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "events",
    [
        [],
        [RuntimeError("connection lost")],
        [{"type": "response.failed"}],
        [{"type": "response.output_item.done", "output_index": 0, "item": call()}],
        [{"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "unknown", "delta": "{"}],
        [*tool_events([call()]), terminal(response([call(arguments='{"value": 2}')]))],
    ],
)
async def test_bad_stream_has_no_terminal_or_tool_execution(events):
    client = FakeResponsesClient(events)
    emitted = []
    with pytest.raises(RuntimeError):
        async for event in provider(client).stream([Message.user("go")], "system", TOOLS):
            emitted.append(event)
    assert not any(isinstance(event, MessageEndEvent) for event in emitted)
    assert client.streams[0].closed


@pytest.mark.asyncio
async def test_missing_usage_and_generator_close():
    client = FakeResponsesClient([{"type": "response.output_text.delta", "delta": "partial"}], response(usage=None))
    p = provider(client)
    stream = p.stream([Message.user("go")], "system")
    await anext(stream)
    await anext(stream)
    await stream.aclose()
    assert client.streams[0].closed
    result = await p.complete([Message.user("go")], "system")
    assert not result.usage.reported and result.usage.normalized_total_tokens == 0


@pytest.mark.asyncio
async def test_context_limit_is_distinguishable_from_other_400_errors():
    error = BadRequestError(
        "too long",
        response=httpx.Response(400, request=httpx.Request("POST", "https://fake.test")),
        body={"code": "context_length_exceeded"},
    )
    with pytest.raises(ResponsesContextLimitError):
        await provider(FakeResponsesClient(error)).complete([Message.user("go")], "system")


@pytest.mark.asyncio
async def test_request_local_stream_state_supports_concurrent_calls():
    client = FakeResponsesClient([terminal(response([message("a")]))], [terminal(response([message("b")]))])
    p = provider(client)

    async def collect():
        return [event async for event in p.stream([Message.user("go")], "system")]

    streams = await asyncio.gather(collect(), collect())
    assert ["".join(event.text for event in events if isinstance(event, TextDeltaEvent)) for events in streams] == [
        "a",
        "b",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        call(arguments='{"value": 2}'),
        call(name="changed"),
        call(id="changed"),
        call("changed"),
    ],
)
async def test_terminal_checks_item_identity_and_arguments_without_done_events(changed):
    client = FakeResponsesClient(
        [
            {"type": "response.output_item.added", "output_index": 0, "item": call(arguments="", status="in_progress")},
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "item_id": "fc_call1",
                "delta": '{"value": 1}',
            },
            terminal(response([changed])),
        ]
    )
    tools = [*TOOLS, ToolDefinition("changed", "Other tool", {})]
    events = []
    with pytest.raises(ResponsesProtocolError):
        async for event in provider(client).stream([Message.user("go")], "system", tools):
            events.append(event)
    assert not any(isinstance(event, (ToolUseEndEvent, MessageEndEvent)) for event in events)


def test_adapter_duplicate_terminals_fail_and_duplicate_function_done_is_idempotent():
    p = provider(FakeResponsesClient())
    adapter = ResponsesStreamAdapter(p._responses_identity, TOOLS)
    events = tool_events([call()])
    emitted = [unified for event in [*events, events[-1]] for unified in adapter.feed(event)]
    assert sum(isinstance(event, ToolUseEndEvent) for event in emitted) == 1
    adapter.feed(terminal(response([call()])))
    with pytest.raises(ResponsesProtocolError, match="after its terminal"):
        adapter.feed(terminal(response([call()])))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        {"type": "error", "code": "context_length_exceeded"},
        {"type": "response.failed", "response": {"error": {"code": "context_window_exceeded"}}},
    ],
)
async def test_stream_context_limit_uses_local_compaction_error(event):
    with pytest.raises(ResponsesContextLimitError):
        async for _ in provider(FakeResponsesClient([event])).stream([Message.user("go")], "system"):
            pass


def test_actual_sdk_endpoint_is_used_for_validation_and_history_scope():
    with pytest.raises(ValueError, match="base URL"):
        provider(FakeResponsesClient(base_url="ftp://custom.invalid/v1"))
    p = provider(FakeResponsesClient(base_url="http://localhost:8765/v1"))
    assert p._responses_identity["endpoint"] == p._endpoint_id("http://localhost:8765/v1")
    assert p._responses_identity["endpoint"] != p._endpoint_id("https://api.openai.com/v1")
