import pytest

from iac_code.providers.base import Message, ToolDefinition
from iac_code.providers.responses_codec import ResponsesProtocolError
from iac_code.providers.responses_provider import DashScopeResponsesProvider, validate_responses_endpoint
from iac_code.services.context_manager import ContextManager, get_context_window_config
from iac_code.types.stream_events import ThinkingDeltaEvent
from tests.providers._responses_fakes import FakeResponsesClient, call, message, reasoning, response, terminal

BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
TOOLS = [ToolDefinition("lookup", "Lookup", {"type": "object"})]


def provider(client, **kwargs):
    return DashScopeResponsesProvider(model="qwen3.8-max", client=client, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "effort,enabled,expected",
    [
        (None, None, "xhigh"),
        ("medium", None, "medium"),
        ("high", None, "xhigh"),
        ("max", None, "xhigh"),
        ("ultra", None, "xhigh"),
        ("minimal", None, "low"),
        ("none", None, "none"),
        ("high", False, "none"),
    ],
)
async def test_qwen_request_uses_responses_parameters(effort, enabled, expected):
    client = FakeResponsesClient(response(), base_url=BASE)
    await provider(client, effort=effort, thinking_enabled=enabled).complete([Message.user("go")], "system", TOOLS)
    kwargs = client.calls[0]
    assert kwargs["reasoning"] == {"effort": expected}
    assert kwargs["store"] is False
    assert kwargs["instructions"] == "system"
    assert "strict" not in kwargs["tools"][0]
    assert "extra_body" not in kwargs and "include" not in kwargs and "parallel_tool_calls" not in kwargs
    assert "cache_control" not in str(kwargs)


@pytest.mark.asyncio
async def test_qwen_replays_multiple_function_results_adjacent_to_calls():
    client = FakeResponsesClient(
        response([reasoning(encrypted_content=None), call(), call("call2"), message()]), response(), base_url=BASE
    )
    p = provider(client)
    result = await p.complete([Message.user("go")], "system", TOOLS)
    history = [
        Message.user("go"),
        Message(role="assistant", metadata=result.provider_metadata),
        Message.tool_result(tool_use_id="call2", content="two"),
        Message.tool_result(tool_use_id="call1", content="one"),
    ]
    await p.complete(history, "system", TOOLS)
    items = client.calls[1]["input"]
    calls = [index for index, item in enumerate(items) if item.get("type") == "function_call"]
    assert len(calls) == 2
    for index in calls:
        assert items[index + 1]["type"] == "function_call_output"
        assert items[index + 1]["call_id"] == items[index]["call_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "history",
    [
        [Message.assistant_tool_use(tool_use_id="call1", name="lookup", input={})],
        [Message.tool_result(tool_use_id="call1", content="orphan")],
        [
            Message.assistant_tool_use(tool_use_id="call1", name="lookup", input={}),
            Message.tool_result(tool_use_id="call1", content="one"),
            Message.tool_result(tool_use_id="call1", content="two"),
        ],
    ],
)
async def test_invalid_tool_history_fails_before_sdk_call(history):
    client = FakeResponsesClient(base_url=BASE)
    with pytest.raises(ResponsesProtocolError):
        await provider(client).complete(history, "system", TOOLS)
    assert client.calls == []


@pytest.mark.asyncio
async def test_qwen_reasoning_event_maps_to_existing_thinking_event():
    data = response([reasoning(encrypted_content=None), message()])
    client = FakeResponsesClient(
        [{"type": "response.reasoning_text.delta", "delta": "thought"}, terminal(data)], base_url=BASE
    )
    events = [event async for event in provider(client).stream([Message.user("go")], "system")]
    assert "".join(event.text for event in events if isinstance(event, ThinkingDeltaEvent)) == "thought"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["qwen3.8-max", "qwen3.7-max", "qwen3.6-plus", "qwen3.5-plus"])
async def test_output_limit_is_sent_as_configured(model):
    client = FakeResponsesClient(response(), base_url=BASE)
    p = DashScopeResponsesProvider(model=model, client=client, max_completion_tokens=123)
    await p.complete([Message.user("go")], "system", max_tokens=1000)
    assert client.calls[0]["max_output_tokens"] == 123


@pytest.mark.asyncio
async def test_qwen_validates_output_limit_locally():
    client = FakeResponsesClient(base_url=BASE)
    with pytest.raises(ValueError, match="at least 16"):
        await provider(client, max_completion_tokens=15).complete([Message.user("go")], "system")
    assert client.calls == []


@pytest.mark.asyncio
async def test_legacy_assistant_text_uses_compatible_easy_message():
    from iac_code.providers.base import ContentBlock

    client = FakeResponsesClient(response(), base_url=BASE)
    await provider(client).complete(
        [
            Message.user("go"),
            Message("assistant", [ContentBlock(type="text", text="old Chat answer")]),
            Message.user("continue"),
        ],
        "system",
    )
    assert client.calls[0]["input"][1] == {"role": "assistant", "content": "old Chat answer"}


def test_qwen_responses_input_window_does_not_change_chat_window():
    context = ContextManager("system", "qwen3.8-max")
    original = get_context_window_config("qwen3.8-max").context_window
    context.set_input_window_ratio(DashScopeResponsesProvider.input_window_ratio)
    assert context.context_window < original * 0.8
    assert get_context_window_config("qwen3.8-max").context_window == original
    context.set_input_window_ratio()
    assert context.context_window == original


@pytest.mark.parametrize(
    "base",
    [
        BASE,
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "https://workspace.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
    ],
)
def test_documented_endpoints_are_accepted(base):
    validate_responses_endpoint("dashscope", base, "qwen3.8-max")


@pytest.mark.parametrize(
    "base",
    [
        "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "https://coding.dashscope.aliyuncs.com/v1",
        "https://custom.test/v1",
        BASE + "/chat/completions",
    ],
)
def test_other_endpoints_are_not_enabled(base):
    with pytest.raises(ValueError):
        validate_responses_endpoint("dashscope", base, "qwen3.8-max")
