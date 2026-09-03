from __future__ import annotations

import copy
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from iac_code.agent.agent_loop import AgentLoop
from iac_code.agent.system_prompt import DYNAMIC_BOUNDARY
from iac_code.cli.output_formats import stream_json_event_data
from iac_code.providers.base import ContentBlock, Message, NonStreamingResponse, ToolDefinition
from iac_code.providers.dashscope_provider import DashScopeProvider
from iac_code.providers.manager import ProviderManager, create_provider
from iac_code.providers.model_family import is_qwen_model
from iac_code.providers.openai_provider import OpenAIProvider
from iac_code.providers.qwen_provider import QwenProvider
from iac_code.providers.request_policy import ProviderRequestPolicy
from iac_code.providers.retry import RetryConfig
from iac_code.providers.schema_compat import relax_qwen_tool_schema
from iac_code.providers.streaming import CumulativeDeltaNormalizer, UnsafeStreamProtocolError
from iac_code.providers.thinking import EffortLevel, get_thinking_spec
from iac_code.services.session_usage import SessionUsageStore
from iac_code.services.telemetry.names import Events, GenAiAttr, IacCodeAttr, Metrics
from iac_code.types.stream_events import (
    ErrorEvent,
    MessageEndEvent,
    MessageStartEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    TombstoneEvent,
    ToolUseEndEvent,
    Usage,
)
from tests.providers._fakes import FakeOpenAIClient, ns


def _tool(name: str = "read_file") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="Read a file",
        input_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    )


def _chunk(*, content=None, reasoning_content=None, reasoning=None, tool_calls=None, finish_reason=None, usage=None):
    delta = ns(content=content, tool_calls=tool_calls)
    if reasoning_content is not None:
        delta.reasoning_content = reasoning_content
    if reasoning is not None:
        delta.reasoning = reasoning
    return ns(usage=usage, choices=[ns(finish_reason=finish_reason, delta=delta)])


def _response(*, content="", reasoning_content=None, reasoning=None, tool_calls=None, finish_reason="stop"):
    message = ns(content=content, tool_calls=tool_calls)
    if reasoning_content is not None:
        message.reasoning_content = reasoning_content
    if reasoning is not None:
        message.reasoning = reasoning
    return ns(id="response-1", choices=[ns(finish_reason=finish_reason, message=message)], usage=None)


class _RequiredThinkingError(RuntimeError):
    status_code = 400
    code = "InvalidParameter"


class _AsyncChunks:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _SequentialCompletions:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return _AsyncChunks(result) if kwargs.get("stream") else result


def _sequential_client(*results):
    completions = _SequentialCompletions(results)
    return ns(chat=ns(completions=completions), base_url="https://fake.local"), completions


class _ManagerFakeProvider:
    _PROVIDER_KEY = "dashscope_token_plan"
    _logical_provider_key = "openai_compatible"
    _ADAPTER_NAME = "qwen"

    def __init__(
        self,
        model,
        *,
        streams=None,
        completion=None,
        base_url="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    ):
        self.model = model
        self._base_url = base_url
        self.streams = list(streams or [])
        self.completion = completion or _manager_response(model)
        self.stream_calls = []
        self.complete_calls = 0

    def prepare_system_prompt(self, system, tools):
        return f"{system}\nqwen:{self.model}"

    def stream(self, messages, system, tools=None, max_tokens=8192):
        self.stream_calls.append((system, tools))
        outcome = self.streams.pop(0)

        async def generate():
            for item in outcome:
                if isinstance(item, BaseException):
                    raise item
                yield item

        return generate()

    async def complete(self, messages, system, tools=None, max_tokens=8192, **kwargs):
        self.complete_calls += 1
        if isinstance(self.completion, BaseException):
            raise self.completion
        return self.completion


def _manager_response(model="qwen3.8-max"):
    return NonStreamingResponse(
        message_id=f"complete-{model}",
        text="ok",
        tool_uses=[],
        stop_reason="end_turn",
        usage=Usage(input_tokens=3, output_tokens=2, reported=True),
        thinking="",
        thinking_blocks=[],
        usage_attribution=None,
    )


def _valid_manager_stream(message_id="msg-1"):
    return [
        MessageStartEvent(message_id=message_id),
        TextDeltaEvent(text="ok"),
        MessageEndEvent(
            stop_reason="end_turn",
            usage=Usage(input_tokens=3, output_tokens=2, reported=True),
        ),
    ]


class TestQwenRouting:
    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            (" qwen3.8-max ", True),
            ("Qwen/Qwen3.5-122B-A10B", True),
            ("coder-model", True),
            ("qwq-plus", False),
            ("not-qwen-model", False),
        ],
    )
    def test_model_family(self, model, expected):
        assert is_qwen_model(model) is expected

    @pytest.mark.parametrize(
        "provider_key",
        ["dashscope", "dashscope_token_plan", "aliyun_codingplan", "aliyun_codingplan_intl"],
    )
    def test_explicit_dashscope_qwen_uses_subclass(self, provider_key):
        provider = create_provider(
            "qwen3.7-plus",
            {provider_key: "fake"},
            provider_key_override=provider_key,
            provider_config_override={},
        )
        assert type(provider) is QwenProvider
        assert provider._PROVIDER_KEY == provider_key
        assert provider._logical_provider_key == provider_key

    @pytest.mark.parametrize("model", ["deepseek-v4-pro", "glm-5.2", "MiniMax-M2.5", "kimi-k2.6"])
    def test_non_qwen_remains_dashscope(self, model):
        provider = create_provider(
            model,
            {"dashscope": "fake"},
            provider_key_override="dashscope",
            provider_config_override={},
        )
        assert type(provider) is DashScopeProvider

    @pytest.mark.parametrize(
        ("url", "wire"),
        [
            ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "dashscope"),
            ("https://dashscope-us.aliyuncs.com/compatible-mode/v1", "dashscope"),
            ("https://token-plan.us-east-1.maas.aliyuncs.com/compatible-mode/v1", "dashscope_token_plan"),
            ("https://coding.dashscope.aliyuncs.com/v1", "aliyun_codingplan"),
            ("https://coding-intl.dashscope.aliyuncs.com/v1", "aliyun_codingplan_intl"),
        ],
    )
    def test_official_compatible_qwen_routes_to_wire_family(self, url, wire):
        provider = create_provider(
            "qwen3.7-plus",
            {"openai_compatible": "fake"},
            provider_key_override="openai_compatible",
            base_url=url,
            provider_config_override={},
        )
        assert type(provider) is QwenProvider
        assert provider._PROVIDER_KEY == wire
        assert provider._logical_provider_key == "openai_compatible"

    def test_custom_openai_compatible_qwen_does_not_use_qwen_provider(self):
        provider = create_provider(
            "qwen3.7-plus",
            {"openai_compatible": "fake"},
            provider_key_override="openai_compatible",
            base_url="https://proxy.example/v1",
            provider_config_override={},
        )
        assert not isinstance(provider, QwenProvider)

    def test_new_official_route_does_not_reclassify_non_qwen(self):
        provider = create_provider(
            "glm-5.2",
            {"openai_compatible": "fake"},
            provider_key_override="openai_compatible",
            base_url="https://dashscope-us.aliyuncs.com/compatible-mode/v1",
            provider_config_override={},
        )
        assert provider._PROVIDER_KEY == "openai_compatible"

    @pytest.mark.parametrize(
        "url",
        [
            "http://dashscope.aliyuncs.com/compatible-mode/v1",
            "https://dashscope.aliyuncs.com:8443/compatible-mode/v1",
            "https://dashscope.aliyuncs.com.example/compatible-mode/v1",
            "https://dashscope.aliyuncs.com/compatible-mode-evil/v1",
            "https://dashscope.aliyuncs.com/apps/anthropic",
            "https://example.com/compatible-mode/v1?target=dashscope.aliyuncs.com",
        ],
    )
    def test_malicious_or_wrong_protocol_urls_do_not_route(self, url):
        provider = create_provider(
            "qwen3.7-plus",
            {"openai_compatible": "fake"},
            provider_key_override="openai_compatible",
            base_url=url,
            provider_config_override={},
        )
        assert type(provider) is OpenAIProvider

    @pytest.mark.parametrize(
        ("url", "wire"),
        [
            ("https://dashscope.aliyuncs.com/compatible-mode/v1", "dashscope"),
            ("https://cn-hongkong.dashscope.aliyuncs.com/compatible-mode/v1", "dashscope"),
            (
                "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                "dashscope_token_plan",
            ),
        ],
    )
    def test_legacy_official_routes_still_apply_to_non_qwen(self, url, wire):
        provider = create_provider(
            "glm-5.2",
            {"openai_compatible": "fake"},
            provider_key_override="openai_compatible",
            base_url=url,
            provider_config_override={},
        )
        assert type(provider) is DashScopeProvider
        assert provider._PROVIDER_KEY == wire

    def test_global_token_plan_query_cannot_trigger_legacy_non_qwen_route(self):
        provider = create_provider(
            "glm-5.2",
            {"openai_compatible": "fake"},
            provider_key_override="openai_compatible",
            base_url=(
                "https://token-plan.us-east-1.maas.aliyuncs.com/compatible-mode/v1"
                "?target=token-plan.cn-beijing.maas.aliyuncs.com"
            ),
            provider_config_override={},
        )
        assert provider._PROVIDER_KEY == "openai_compatible"

    def test_modelscope_qwen_does_not_use_dashscope_adapter(self):
        provider = create_provider(
            "Qwen/Qwen3.5-122B-A10B",
            {"modelscope": "fake"},
            provider_key_override="modelscope",
            provider_config_override={},
        )
        assert not isinstance(provider, (DashScopeProvider, QwenProvider))


class TestQwenRequestAdaptation:
    def test_inheritance_and_adapter_identity(self):
        provider = QwenProvider(model="qwen3.7-plus", api_key="fake")
        assert isinstance(provider, DashScopeProvider)
        assert provider._ADAPTER_NAME == "qwen"

    @pytest.mark.parametrize("model", ["qwen3-coder-plus", "qwen-vl-max", "qwen3.7-plus"])
    def test_prompt_is_dynamic_bounded_and_idempotent(self, model):
        provider = QwenProvider(model=model, api_key="fake")
        base = f"stable\n{DYNAMIC_BOUNDARY}\ndynamic"
        prepared = provider.prepare_system_prompt(base, [_tool()])
        assert prepared.startswith(f"stable\n{DYNAMIC_BOUNDARY}")
        assert "read_file" in prepared
        assert provider.prepare_system_prompt(prepared, [_tool()]) == prepared
        assert provider.prepare_system_prompt(base, None) == base

    def test_prompt_styles_match_qwen_code_formats_and_keep_static_prefix_stable(self):
        base = f"stable prefix\n{DYNAMIC_BOUNDARY}\nproject facts"
        coder = QwenProvider(model="qwen3-coder-plus", api_key="fake").prepare_system_prompt(base, [_tool()])
        vision = QwenProvider(model="qwen-vl-max", api_key="fake").prepare_system_prompt(base, [_tool()])
        generic = QwenProvider(model="qwen3.7-plus", api_key="fake").prepare_system_prompt(base, [_tool()])
        assert "<function=read_file>" in coder and "<parameter=path>" in coder
        assert '<tool_call>{"name":"read_file","arguments":{"path":"VALUE"}}</tool_call>' in vision
        assert '<tool_call>{"name"' not in generic
        assert all(item.split(DYNAMIC_BOUNDARY, 1)[0] == "stable prefix\n" for item in (coder, vision, generic))
        assert all(len(item) - len(base) < 900 for item in (coder, vision, generic))

    def test_custom_proxy_disables_official_cache_protocol(self):
        provider = QwenProvider(model="qwen3.7-plus", api_key="fake", base_url="https://proxy.example/v1")
        assert provider._request_headers() == {}
        messages = provider._build_api_messages([], "system")
        assert messages == [{"role": "system", "content": "system"}]

    def test_official_endpoint_enables_header_and_stream_tool_marker(self):
        provider = QwenProvider(model="qwen3.7-plus", api_key="fake")
        assert provider._request_headers() == {"X-DashScope-CacheControl": "enable"}
        api_tools = provider._build_api_tools([_tool()], streaming=True)
        assert api_tools[-1]["cache_control"] == {"type": "ephemeral"}
        assert "$schema" not in api_tools[0]["function"]["parameters"]

    def test_cache_protocol_requires_policy_model_and_official_endpoint(self):
        unsupported = QwenProvider(model="qwen2-legacy", api_key="fake")
        assert unsupported._request_headers() == {}
        disabled = QwenProvider(model="qwen3.7-plus", api_key="fake")
        assert disabled._request_headers(cache_policy="no_explicit_cache") == {}
        tools = disabled._build_api_tools([_tool()], streaming=True, cache_policy="no_explicit_cache")
        assert "cache_control" not in tools[-1]

    def test_schema_wire_copy_does_not_mutate_original(self):
        tool = _tool()
        original = copy.deepcopy(tool.input_schema)
        provider = QwenProvider(model="qwen3.7-plus", api_key="fake")
        prepared = provider._build_api_tools([tool], streaming=False)
        assert tool.input_schema == original
        assert prepared[0]["function"]["parameters"]["additionalProperties"] is False

    def test_schema_relaxation_is_recursive_and_preserves_map_keyword_names(self):
        schema = {
            "$schema": "draft",
            "$id": "root",
            "type": "object",
            "properties": {
                "$schema": {"type": "string", "uniqueItems": True},
                "optional": {
                    "type": "object",
                    "properties": {"x": {"type": "array", "uniqueItems": True}},
                    "additionalProperties": False,
                },
            },
            "required": ["$schema"],
            "additionalProperties": False,
            "enum": [{"$schema": "literal", "uniqueItems": True}],
        }
        original = copy.deepcopy(schema)
        relaxed = relax_qwen_tool_schema(schema)
        assert schema == original
        assert "$schema" not in relaxed and "$id" not in relaxed
        assert "$schema" in relaxed["properties"]
        assert "uniqueItems" not in relaxed["properties"]["$schema"]
        assert "additionalProperties" not in relaxed
        assert "additionalProperties" not in relaxed["properties"]["optional"]
        assert relaxed["enum"] == [{"$schema": "literal", "uniqueItems": True}]

    def test_qwen38_efforts_and_token_plan_mandatory(self):
        default = QwenProvider(model="qwen3.8-max", api_key="fake")
        assert default._build_thinking_kwargs() == {
            "reasoning_effort": "xhigh",
            "extra_body": {"preserve_thinking": True},
        }
        provider = QwenProvider(model="qwen3.8-max-latest", api_key="fake", effort="high")
        assert provider._build_thinking_kwargs()["reasoning_effort"] == "high"
        provider = QwenProvider(model="qwen3.8-max-2026-08-01", api_key="fake", effort="max")
        assert provider._build_thinking_kwargs()["reasoning_effort"] == "xhigh"
        mandatory = QwenProvider(
            model="qwen3.8-max",
            api_key="fake",
            provider_key="dashscope_token_plan",
            thinking_enabled=False,
        )
        assert mandatory._build_thinking_kwargs()["extra_body"]["enable_thinking"] is True
        assert EffortLevel.HIGH in get_thinking_spec("dashscope", "qwen3.8-max-latest").allowed_efforts

    def test_qwen38_standard_disable_and_cross_source_precedence(self):
        standard = QwenProvider(model="qwen3.8-max", api_key="fake", thinking_enabled=False)
        assert standard._build_thinking_kwargs()["reasoning_effort"] == "none"

        request_effort = create_provider(
            "qwen3.8-max",
            {"dashscope": "fake"},
            provider_key_override="dashscope",
            provider_config_override={"thinkingEnabled": False},
            request_policy_override=ProviderRequestPolicy(effort="high"),
        )
        assert request_effort._build_thinking_kwargs()["reasoning_effort"] == "high"

        request_disable = create_provider(
            "qwen3.8-max",
            {"dashscope": "fake"},
            provider_key_override="dashscope",
            provider_config_override={"effort": "high"},
            request_policy_override=ProviderRequestPolicy(thinking_enabled=False, effort="high"),
        )
        assert request_disable._build_thinking_kwargs()["reasoning_effort"] == "none"

    def test_qwen_thinking_budget_effort_and_disable_precedence(self):
        request_budget = create_provider(
            "qwen3.8-max",
            {"dashscope": "fake"},
            provider_key_override="dashscope",
            provider_config_override={"thinkingEnabled": False, "effort": "low"},
            request_policy_override=ProviderRequestPolicy(thinking_budget=4096),
        )
        assert request_budget._build_thinking_kwargs() == {
            "extra_body": {
                "enable_thinking": True,
                "preserve_thinking": True,
                "thinking_budget": 4096,
            }
        }

        request_effort = create_provider(
            "qwen3.8-max",
            {"dashscope": "fake"},
            provider_key_override="dashscope",
            provider_config_override={"models": {"qwen3.8-max": {"thinkingBudget": 4096}}},
            request_policy_override=ProviderRequestPolicy(effort="high"),
        )
        assert request_effort._build_thinking_kwargs() == {
            "reasoning_effort": "high",
            "extra_body": {"preserve_thinking": True},
        }

        same_layer_effort = create_provider(
            "qwen3.8-max",
            {"dashscope": "fake"},
            provider_key_override="dashscope",
            provider_config_override={},
            request_policy_override=ProviderRequestPolicy(effort="medium", thinking_budget=2048),
        )
        assert same_layer_effort._build_thinking_kwargs()["reasoning_effort"] == "medium"
        assert "thinking_budget" not in same_layer_effort._build_thinking_kwargs().get("extra_body", {})

        same_layer_disable = create_provider(
            "qwen3.8-max",
            {"dashscope": "fake"},
            provider_key_override="dashscope",
            provider_config_override={},
            request_policy_override=ProviderRequestPolicy(
                thinking_enabled=False,
                effort="high",
                thinking_budget=2048,
            ),
        )
        assert same_layer_disable._build_thinking_kwargs()["reasoning_effort"] == "none"

        legacy_budget = create_provider(
            "qwen3.7-plus",
            {"dashscope": "fake"},
            provider_key_override="dashscope",
            provider_config_override={"thinkingEnabled": False},
            request_policy_override=ProviderRequestPolicy(thinking_budget=1024),
        )
        assert legacy_budget._build_thinking_kwargs()["extra_body"] == {
            "enable_thinking": True,
            "preserve_thinking": True,
            "thinking_budget": 1024,
        }

    def test_legacy_disable_alias_respects_higher_request_enable(self):
        provider = create_provider(
            "qwen3.7-plus",
            {"dashscope": "fake"},
            provider_key_override="dashscope",
            provider_config_override={"effort": "off"},
            request_policy_override=ProviderRequestPolicy(thinking_enabled=True),
        )
        assert provider._build_thinking_kwargs()["extra_body"]["enable_thinking"] is True

    def test_reasoning_is_nullish_first_and_history_writes_one_field(self):
        provider = QwenProvider(model="qwen3.7-plus", api_key="fake")
        value = SimpleNamespace(reasoning_content="", reasoning="fallback")
        assert provider._extract_reasoning_text(value) == ""
        api = provider._convert_content_blocks(
            "assistant",
            [ContentBlock(type="thinking", text="thought"), ContentBlock(type="text", text="answer")],
        )
        assert api[0]["reasoning_content"] == "thought"
        assert "reasoning" not in api[0]


class TestCumulativeDeltaNormalizer:
    def test_incremental_short_repeat_and_prefix_transition(self):
        normalizer = CumulativeDeltaNormalizer()
        assert normalizer.feed("abc") == "abc"
        assert normalizer.feed("abc") == "abc"
        assert normalizer.feed("abcdef") == "def"
        assert normalizer.mode == "cumulative"

    def test_long_repeat_rewind_divergence_and_detection_window(self):
        repeated = "x" * 64
        normalizer = CumulativeDeltaNormalizer()
        assert normalizer.feed(repeated) == repeated
        assert normalizer.feed(repeated) == ""
        assert normalizer.feed(repeated[:20]) == ""
        assert normalizer.feed("fresh") == "fresh"
        assert normalizer.mode == "incremental"

        normalizer = CumulativeDeltaNormalizer()
        chunks = ["a" * 600, "b" * 600]
        assert "".join(normalizer.feed(chunk) for chunk in chunks) == "".join(chunks)
        cumulative = "".join(chunks) + "tail"
        assert normalizer.feed(cumulative) == "tail"


@pytest.mark.asyncio
class TestQwenManagerLeaseAndAttribution:
    async def test_real_qwen_provider_flows_through_agent_usage_store_and_cli_surface(self, monkeypatch, tmp_path):
        client = FakeOpenAIClient(
            stream_chunks=[
                _chunk(content="Qwen answer"),
                _chunk(
                    finish_reason="stop",
                    usage=ns(prompt_tokens=7, completion_tokens=3),
                ),
            ],
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        provider = QwenProvider(
            model="qwen3.8-max",
            client=client,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            provider_key="dashscope",
        )
        provider._logical_provider_key = "dashscope"
        monkeypatch.setattr("iac_code.providers.manager.create_provider", lambda *_args, **_kwargs: provider)
        manager = ProviderManager(
            "qwen3.8-max",
            {"dashscope": "fake"},
            provider_key_override="dashscope",
            provider_config_override={},
        )
        usage_path = tmp_path / "usage.jsonl"
        usage_store = SessionUsageStore(path_provider=lambda _cwd, _session_id: usage_path)
        registry = MagicMock()
        registry.list_tools.return_value = [
            SimpleNamespace(
                name="read_file",
                description="Read a file",
                input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            )
        ]
        registry.get.return_value = None
        loop = AgentLoop(
            provider_manager=manager,
            system_prompt="base system",
            tool_registry=registry,
            session_id="qwen-integration",
            cwd=str(tmp_path / "project"),
            session_usage_store=usage_store,
        )
        monkeypatch.setattr(loop, "_refresh_git_branch", lambda: None)

        events = [event async for event in loop.run_streaming("hello")]

        terminal = next(event for event in events if isinstance(event, MessageEndEvent))
        assert terminal.usage_attribution is not None
        assert terminal.usage_attribution.logical_provider_key == "dashscope"
        assert terminal.usage_attribution.wire_provider_key == "dashscope"
        assert terminal.usage_attribution.telemetry_provider_name == "dashscope"
        assert terminal.usage_attribution.adapter_name == "qwen"
        assert terminal.usage.input_tokens == 7
        assert terminal.usage.output_tokens == 3
        persisted = json.loads(usage_path.read_text(encoding="utf-8"))
        assert persisted["provider"] == "dashscope"
        assert persisted["model"] == "qwen3.8-max"
        public_terminal = stream_json_event_data(terminal)
        assert "usage_attribution" not in public_terminal
        assert public_terminal["usage"]["provider"] == "dashscope"
        assert "<!-- iac-code:qwen-tools:start -->" in loop.get_last_provider_request_snapshot()["system_prompt"]

    async def test_lease_binds_old_provider_model_and_prompt_across_reconfigure(self, monkeypatch):
        old = _ManagerFakeProvider("qwen3.8-max", streams=[_valid_manager_stream("old")])
        new = _ManagerFakeProvider("qwen3.7-plus", streams=[_valid_manager_stream("new")])

        def factory(model, *_args, **_kwargs):
            return old if model == "qwen3.8-max" else new

        monkeypatch.setattr("iac_code.providers.manager.create_provider", factory)
        manager = ProviderManager(
            "qwen3.8-max",
            {"dashscope_token_plan": "fake"},
            provider_key_override="dashscope_token_plan",
        )
        lease = manager.begin_request("base")
        assert lease.system_prompt.endswith("qwen:qwen3.8-max")
        manager.reconfigure(
            "qwen3.7-plus",
            {"dashscope_token_plan": "fake"},
            provider_key_override="dashscope_token_plan",
        )
        events = [
            event
            async for event in manager.stream(
                [Message.user("hi")],
                lease.system_prompt,
                lease=lease,
            )
        ]
        end = next(event for event in events if isinstance(event, MessageEndEvent))
        assert old.stream_calls == [(lease.system_prompt, None)]
        assert not new.stream_calls
        assert end.usage_attribution.requested_model == "qwen3.8-max"
        assert end.usage_attribution.actual_model == "qwen3.8-max"
        manager.release_request(lease)

        events = [event async for event in manager.stream([Message.user("hi")], "base")]
        assert next(event for event in events if isinstance(event, MessageEndEvent)).usage_attribution.actual_model == (
            "qwen3.7-plus"
        )

    async def test_lease_rejects_cross_manager_double_consume_and_double_release(self, monkeypatch):
        provider = _ManagerFakeProvider("qwen3.8-max", streams=[_valid_manager_stream()])
        monkeypatch.setattr("iac_code.providers.manager.create_provider", lambda *_args, **_kwargs: provider)
        first = ProviderManager("qwen3.8-max", {"dashscope_token_plan": "fake"})
        second = ProviderManager("qwen3.8-max", {"dashscope_token_plan": "fake"})
        lease = first.begin_request("base")
        with pytest.raises(ValueError, match="different manager"):
            second._consume_request_lease(lease)
        first._consume_request_lease(lease)
        with pytest.raises(ValueError, match="consumed"):
            first._consume_request_lease(lease)
        first.release_request(lease)
        with pytest.raises(ValueError, match="already released"):
            first.release_request(lease)

    @pytest.mark.parametrize(
        ("base_url", "adapter", "official"),
        [
            (
                "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                "qwen",
                True,
            ),
            ("https://proxy.example/v1", "qwen", False),
            ("https://dashscope.aliyuncs.com/compatible-mode/v1", "", True),
        ],
    )
    async def test_provider_adapter_and_official_endpoint_are_on_stream_and_complete_telemetry(
        self,
        monkeypatch,
        base_url,
        adapter,
        official,
    ):
        provider = _ManagerFakeProvider(
            "qwen3.8-max",
            streams=[_valid_manager_stream()],
            base_url=base_url,
        )
        provider._PROVIDER_KEY = "dashscope"
        provider._ADAPTER_NAME = adapter
        monkeypatch.setattr("iac_code.providers.manager.create_provider", lambda *_args, **_kwargs: provider)
        telemetry_events = []
        telemetry_metrics = []
        stream_span = MagicMock()
        complete_span = MagicMock()
        detached = MagicMock(return_value=stream_span)
        attached = MagicMock(return_value=nullcontext(complete_span))
        monkeypatch.setattr("iac_code.providers.manager.start_detached_span", detached)
        monkeypatch.setattr("iac_code.providers.manager.start_span", attached)
        monkeypatch.setattr(
            "iac_code.providers.manager.log_event",
            lambda name, attrs: telemetry_events.append((name, attrs)),
        )
        monkeypatch.setattr(
            "iac_code.providers.manager.add_metric",
            lambda name, value, attrs: telemetry_metrics.append((name, value, attrs)),
        )

        manager = ProviderManager("qwen3.8-max", {"dashscope": "fake"})
        _ = [event async for event in manager.stream([Message.user("hi")], "base")]
        await manager.complete([Message.user("hi")], "base")

        span_attrs = [detached.call_args.args[1], attached.call_args.args[1]]
        lifecycle_attrs = [
            attrs
            for name, attrs in telemetry_events
            if name
            in {
                Events.API_REQUEST_STARTED,
                Events.API_REQUEST_SUCCEEDED,
                Events.API_REQUEST_FAILED,
            }
        ]
        assert len(lifecycle_attrs) == 4
        for attrs in [*span_attrs, *lifecycle_attrs]:
            assert attrs[IacCodeAttr.OFFICIAL_ENDPOINT] is official
            if adapter:
                assert attrs[IacCodeAttr.PROVIDER_ADAPTER] == adapter
            else:
                assert IacCodeAttr.PROVIDER_ADAPTER not in attrs
        metric_attrs = [
            attrs
            for name, _value, attrs in telemetry_metrics
            if name
            in {
                Metrics.API_REQUEST_COUNT,
                Metrics.API_REQUEST_DURATION,
                Metrics.TOKEN_USAGE_REPORT_COUNT,
                Metrics.TOKEN_TOTAL,
                Metrics.TOKEN_USAGE,
            }
        ]
        assert metric_attrs
        for attrs in metric_attrs:
            assert attrs["provider"] == "dashscope"
            assert attrs[IacCodeAttr.OFFICIAL_ENDPOINT] is official
            if adapter:
                assert attrs[IacCodeAttr.PROVIDER_ADAPTER] == adapter
            else:
                assert IacCodeAttr.PROVIDER_ADAPTER not in attrs

    async def test_terminal_attribution_separates_logical_wire_service_adapter_and_models(self, monkeypatch):
        provider = _ManagerFakeProvider("qwen3.8-max", streams=[_valid_manager_stream()])
        monkeypatch.setattr("iac_code.providers.manager.create_provider", lambda *_args, **_kwargs: provider)
        manager = ProviderManager(
            "qwen3.8-max",
            {"openai_compatible": "fake"},
            provider_key_override="openai_compatible",
        )
        events = [event async for event in manager.stream([Message.user("hi")], "base")]
        attribution = next(
            event.usage_attribution for event in events if isinstance(event, MessageEndEvent)
        )
        assert (
            attribution.logical_provider_key,
            attribution.wire_provider_key,
            attribution.telemetry_provider_name,
            attribution.adapter_name,
        ) == ("openai_compatible", "dashscope_token_plan", "dashscope", "qwen")

        response = await manager.complete([Message.user("hi")], "base")
        assert response.usage_attribution == attribution

    async def test_model_fallback_attributes_actual_success_model(self, monkeypatch):
        class ServerError(RuntimeError):
            status_code = 500

        telemetry_events = []
        monkeypatch.setattr(
            "iac_code.providers.manager.log_event",
            lambda name, attrs: telemetry_events.append((name, attrs)),
        )
        primary = _ManagerFakeProvider("qwen3.8-max", completion=ServerError("temporary"))
        fallback = _ManagerFakeProvider(
            "qwen3.7-plus",
            completion=_manager_response("qwen3.7-plus"),
            base_url="https://proxy.example/v1",
        )

        def factory(model, *_args, **_kwargs):
            return primary if model == "qwen3.8-max" else fallback

        monkeypatch.setattr("iac_code.providers.manager.create_provider", factory)
        manager = ProviderManager(
            "qwen3.8-max",
            {"dashscope": "fake"},
            retry_config=RetryConfig(max_retries=0),
            provider_key_override="dashscope",
        )
        response = await manager.complete([Message.user("hi")], "base")
        assert response.usage_attribution.requested_model == "qwen3.8-max"
        assert response.usage_attribution.actual_model == "qwen3.7-plus"
        started = [attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_STARTED]
        assert [attrs[IacCodeAttr.OFFICIAL_ENDPOINT] for attrs in started] == [True, False]
        assert [attrs[IacCodeAttr.PROVIDER_ADAPTER] for attrs in started] == ["qwen", "qwen"]

    async def test_unsafe_stream_tombstones_and_replays_twice_without_complete(self, monkeypatch):
        telemetry_events = []
        monkeypatch.setattr(
            "iac_code.providers.manager.log_event",
            lambda name, attrs: telemetry_events.append((name, attrs)),
        )
        unsafe = UnsafeStreamProtocolError("tag leak")
        provider = _ManagerFakeProvider(
            "qwen3.8-max",
            streams=[
                [MessageStartEvent(message_id="one"), TextDeltaEvent(text="hidden"), unsafe],
                [MessageStartEvent(message_id="two"), unsafe],
                [MessageStartEvent(message_id="three"), unsafe],
            ],
        )
        monkeypatch.setattr("iac_code.providers.manager.create_provider", lambda *_args, **_kwargs: provider)
        manager = ProviderManager("qwen3.8-max", {"dashscope": "fake"})
        events = [event async for event in manager.stream([Message.user("hi")], "base")]
        assert len(provider.stream_calls) == 3
        assert provider.complete_calls == 0
        assert [event.message_id for event in events if isinstance(event, TombstoneEvent)] == [
            "one",
            "two",
            "three",
        ]
        assert isinstance(events[-1], ErrorEvent)
        assert not any(isinstance(event, MessageEndEvent) for event in events)
        assert [name for name, _attrs in telemetry_events].count(Events.API_REQUEST_STARTED) == 3
        assert [name for name, _attrs in telemetry_events].count(Events.API_REQUEST_FAILED) == 3
        assert [name for name, _attrs in telemetry_events].count(Events.API_REQUEST_SUCCEEDED) == 0

    async def test_unsafe_stream_successful_replay_keeps_terminal_attribution(self, monkeypatch):
        telemetry_events = []
        primary_span = MagicMock()
        replay_span = MagicMock()
        monkeypatch.setattr(
            "iac_code.providers.manager.log_event",
            lambda name, attrs: telemetry_events.append((name, attrs)),
        )
        monkeypatch.setattr(
            "iac_code.providers.manager.start_detached_span",
            MagicMock(side_effect=[primary_span, replay_span]),
        )
        provider = _ManagerFakeProvider(
            "qwen3.8-max",
            streams=[
                [MessageStartEvent(message_id="one"), UnsafeStreamProtocolError("tag leak")],
                _valid_manager_stream("two"),
            ],
        )
        monkeypatch.setattr("iac_code.providers.manager.create_provider", lambda *_args, **_kwargs: provider)
        manager = ProviderManager("qwen3.8-max", {"dashscope": "fake"})
        events = [event async for event in manager.stream([Message.user("hi")], "base")]
        assert provider.complete_calls == 0
        assert any(isinstance(event, TombstoneEvent) and event.message_id == "one" for event in events)
        end = next(event for event in events if isinstance(event, MessageEndEvent))
        assert end.usage_attribution.actual_model == "qwen3.8-max"
        assert end.usage.provider == "dashscope"
        assert end.usage.model == "qwen3.8-max"
        assert [name for name, _attrs in telemetry_events].count(Events.API_REQUEST_STARTED) == 2
        assert [name for name, _attrs in telemetry_events].count(Events.API_REQUEST_FAILED) == 1
        assert [name for name, _attrs in telemetry_events].count(Events.API_REQUEST_SUCCEEDED) == 1
        first_token_events = [attrs for name, attrs in telemetry_events if name == Events.API_RESPONSE_FIRST_TOKEN]
        assert len(first_token_events) == 1
        assert first_token_events[0]["provider"] == "dashscope"
        assert first_token_events[0]["replay_attempt"] == 1
        assert first_token_events[0]["first_token_source"] == "text_delta"
        assert first_token_events[0][IacCodeAttr.PROVIDER_ADAPTER] == "qwen"
        assert first_token_events[0][IacCodeAttr.OFFICIAL_ENDPOINT] is True
        replay_ttft = [
            call
            for call in replay_span.set_attribute.call_args_list
            if call.args[0] == GenAiAttr.RESPONSE_TIME_TO_FIRST_TOKEN
        ]
        assert len(replay_ttft) == 1
        assert replay_ttft[0].args[1] == first_token_events[0][GenAiAttr.RESPONSE_TIME_TO_FIRST_TOKEN]
        for name, attrs in telemetry_events:
            if name in {
                Events.API_REQUEST_STARTED,
                Events.API_REQUEST_FAILED,
                Events.API_REQUEST_SUCCEEDED,
            }:
                assert attrs[IacCodeAttr.PROVIDER_ADAPTER] == "qwen"
                assert attrs[IacCodeAttr.OFFICIAL_ENDPOINT] is True


@pytest.mark.asyncio
class TestQwenResponses:
    async def test_streaming_and_non_streaming_cache_marker_scope(self):
        stream_client = FakeOpenAIClient(
            stream_chunks=[_chunk(content="ok"), _chunk(finish_reason="stop")]
        )
        stream_provider = QwenProvider(model="qwen3.7-plus", client=stream_client)
        _ = [
            event
            async for event in stream_provider.stream(
                [Message.user("hello")],
                "system",
                [_tool()],
            )
        ]
        stream_call = stream_client.chat.completions.calls[0]
        assert stream_call["extra_headers"] == {"X-DashScope-CacheControl": "enable"}
        assert stream_call["tools"][-1]["cache_control"] == {"type": "ephemeral"}
        assert any(
            isinstance(part, dict) and "cache_control" in part
            for message in stream_call["messages"]
            if message["role"] == "user"
            for part in message["content"]
        )

        complete_client = FakeOpenAIClient(create_response=_response(content="ok"))
        complete_provider = QwenProvider(model="qwen3.7-plus", client=complete_client)
        await complete_provider.complete([Message.user("hello")], "system", [_tool()])
        complete_call = complete_client.chat.completions.calls[0]
        assert "cache_control" not in complete_call["tools"][-1]
        assert complete_call["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert all(
            "cache_control" not in part
            for message in complete_call["messages"]
            if message["role"] == "user"
            for part in message["content"]
        )

    @pytest.mark.parametrize("streaming", [False, True])
    async def test_required_thinking_error_rebuilds_once_and_is_learned(self, streaming):
        error = _RequiredThinkingError(
            "The value of the enable_thinking parameter is restricted to True."
        )
        success = (
            [_chunk(content="ok"), _chunk(finish_reason="stop")]
            if streaming
            else _response(content="ok")
        )
        client, completions = _sequential_client(error, success, success)
        provider = QwenProvider(
            model="qwen3.8-max",
            client=client,
            thinking_enabled=False,
        )
        provider._extra_request_kwargs = {"tool_choice": "required"}
        if streaming:
            _ = [event async for event in provider.stream([Message.user("hi")], "sys", [_tool()])]
        else:
            await provider.complete([Message.user("hi")], "sys", [_tool()])
        assert completions.calls[0]["reasoning_effort"] == "none"
        assert completions.calls[0]["tool_choice"] == "required"
        assert completions.calls[1]["extra_body"]["enable_thinking"] is True
        assert "reasoning_effort" not in completions.calls[1]
        assert "tool_choice" not in completions.calls[1]

        if streaming:
            _ = [event async for event in provider.stream([Message.user("again")], "sys", [_tool()])]
        else:
            await provider.complete([Message.user("again")], "sys", [_tool()])
        assert completions.calls[2]["extra_body"]["enable_thinking"] is True

    async def test_required_thinking_retry_reports_both_real_api_attempts(self, monkeypatch):
        error = _RequiredThinkingError(
            "The value of the enable_thinking parameter is restricted to True."
        )
        client, completions = _sequential_client(
            error,
            [_chunk(content="ok"), _chunk(finish_reason="stop")],
        )
        provider = QwenProvider(
            model="qwen3.8-max",
            client=client,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            provider_key="dashscope",
            thinking_enabled=False,
        )
        provider._extra_request_kwargs = {"tool_choice": "required"}
        events = []
        metrics = []
        monkeypatch.setattr("iac_code.providers.manager.create_provider", lambda *_args, **_kwargs: provider)
        monkeypatch.setattr(
            "iac_code.providers.manager.log_event",
            lambda name, attrs: events.append((name, attrs)),
        )
        monkeypatch.setattr(
            "iac_code.providers.qwen_provider.log_event",
            lambda name, attrs: events.append((name, attrs)),
        )
        monkeypatch.setattr(
            "iac_code.providers.manager.add_metric",
            lambda name, value, attrs: metrics.append((name, value, attrs)),
        )
        monkeypatch.setattr(
            "iac_code.providers.qwen_provider.add_metric",
            lambda name, value, attrs: metrics.append((name, value, attrs)),
        )

        manager = ProviderManager("qwen3.8-max", {"dashscope": "fake"})
        result = [event async for event in manager.stream([Message.user("hi")], "sys", [_tool()])]

        assert len(completions.calls) == 2
        assert any(isinstance(event, MessageEndEvent) for event in result)
        failures = [attrs for name, attrs in events if name == Events.API_REQUEST_FAILED]
        retries = [attrs for name, attrs in events if name == Events.API_REQUEST_RETRIED]
        successes = [attrs for name, attrs in events if name == Events.API_REQUEST_SUCCEEDED]
        assert len(failures) == len(retries) == len(successes) == 1
        assert failures[0]["status"] == "compatibility_retry"
        assert retries[0]["reason"] == "mandatory_thinking_compatibility"
        assert retries[0]["streaming"] is True
        for attrs in [*failures, *retries, *successes]:
            assert attrs["provider"] == "dashscope"
            assert attrs[IacCodeAttr.PROVIDER_ADAPTER] == "qwen"
            assert attrs[IacCodeAttr.OFFICIAL_ENDPOINT] is True
        request_metrics = [attrs for name, _value, attrs in metrics if name == Metrics.API_REQUEST_COUNT]
        assert [attrs["status"] for attrs in request_metrics] == ["compatibility_retry", "ok"]
        assert all(attrs["provider"] == "dashscope" for attrs in request_metrics)
        assert all(attrs[IacCodeAttr.PROVIDER_ADAPTER] == "qwen" for attrs in request_metrics)

    async def test_mandatory_learning_does_not_rewrite_an_existing_stream_context(self):
        error = _RequiredThinkingError(
            "The value of the enable_thinking parameter is restricted to True."
        )
        client, completions = _sequential_client(
            error,
            _response(content="learned"),
            [_chunk(content="old-context"), _chunk(finish_reason="stop")],
            _response(content="next-context"),
        )
        provider = QwenProvider(model="qwen3.8-max", client=client, thinking_enabled=False)

        existing_stream = provider.stream([Message.user("existing")], "sys")
        assert isinstance(await anext(existing_stream), MessageStartEvent)

        await provider.complete([Message.user("learn")], "sys")
        _ = [event async for event in existing_stream]
        await provider.complete([Message.user("next")], "sys")

        assert completions.calls[0]["reasoning_effort"] == "none"
        assert completions.calls[1]["extra_body"]["enable_thinking"] is True
        assert completions.calls[2]["reasoning_effort"] == "none"
        assert completions.calls[3]["extra_body"]["enable_thinking"] is True

    async def test_ordinary_400_or_enabled_request_is_not_mandatory_retry(self):
        ordinary = _RequiredThinkingError("some other invalid parameter")
        client, completions = _sequential_client(ordinary)
        provider = QwenProvider(model="qwen3.8-max", client=client, thinking_enabled=False)
        with pytest.raises(_RequiredThinkingError):
            await provider.complete([Message.user("hi")], "sys")
        assert len(completions.calls) == 1

        required = _RequiredThinkingError("enable_thinking must be true")
        client, completions = _sequential_client(required)
        provider = QwenProvider(model="qwen3.8-max", client=client, thinking_enabled=True)
        with pytest.raises(_RequiredThinkingError):
            await provider.complete([Message.user("hi")], "sys")
        assert len(completions.calls) == 1

    async def test_cumulative_content_normalizes_before_balanced_think_guard(self):
        client = FakeOpenAIClient(
            stream_chunks=[
                _chunk(content="<think>a"),
                _chunk(content="<think>ab</think>"),
                _chunk(finish_reason="stop"),
            ]
        )
        provider = QwenProvider(model="qwen3.7-plus", client=client)
        events = [event async for event in provider.stream([Message.user("hi")], "sys")]
        assert "".join(event.text for event in events if isinstance(event, TextDeltaEvent)) == "<think>ab</think>"
        assert not any(isinstance(event, ThinkingDeltaEvent) for event in events)

    async def test_reasoning_tag_split_across_chunks_blocks_safe_closing_cleanup(self):
        client = FakeOpenAIClient(
            stream_chunks=[
                _chunk(reasoning="<thi"),
                _chunk(
                    reasoning="nk>",
                    content="</think>",
                    tool_calls=[
                        ns(
                            index=0,
                            id="call_1",
                            function=ns(name="read_file", arguments='{"path":"a.py"}'),
                        )
                    ],
                ),
                _chunk(finish_reason="tool_calls"),
            ]
        )
        provider = QwenProvider(model="qwen3.7-plus", client=client)
        with pytest.raises(UnsafeStreamProtocolError):
            _ = [event async for event in provider.stream([Message.user("hi")], "sys", [_tool()])]

    async def test_reasoning_channels_are_independent_and_nullish_first(self):
        client = FakeOpenAIClient(
            stream_chunks=[
                _chunk(reasoning="abc"),
                _chunk(reasoning="abcdef"),
                _chunk(content="answer"),
                _chunk(finish_reason="stop"),
            ]
        )
        provider = QwenProvider(model="qwen3.7-plus", client=client)
        events = [event async for event in provider.stream([Message.user("hi")], "sys")]
        assert "".join(event.text for event in events if isinstance(event, ThinkingDeltaEvent)) == "abcdef"
        assert "".join(event.text for event in events if isinstance(event, TextDeltaEvent)) == "answer"

    async def test_reasoning_content_wins_once_and_empty_blocks_fallback(self):
        client = FakeOpenAIClient(
            stream_chunks=[
                _chunk(reasoning_content="primary", reasoning="secondary"),
                _chunk(reasoning_content="", reasoning="must-not-appear"),
                _chunk(finish_reason="stop"),
            ]
        )
        provider = QwenProvider(model="qwen3.7-plus", client=client)
        events = [event async for event in provider.stream([Message.user("hi")], "sys")]
        assert "".join(event.text for event in events if isinstance(event, ThinkingDeltaEvent)) == "primary"

        client = FakeOpenAIClient(
            create_response=_response(content="answer", reasoning_content="", reasoning="must-not-appear")
        )
        response = await QwenProvider(model="qwen3.7-plus", client=client).complete(
            [Message.user("hi")], "sys"
        )
        assert response.thinking == ""
        assert response.thinking_blocks == []

    async def test_unclosed_thinking_tag_is_unsafe(self):
        client = FakeOpenAIClient(
            stream_chunks=[_chunk(content="<think>secret"), _chunk(finish_reason="stop")]
        )
        provider = QwenProvider(model="qwen3.7-plus", client=client)
        with pytest.raises(UnsafeStreamProtocolError):
            _ = [event async for event in provider.stream([Message.user("hi")], "sys")]

    async def test_user_visible_stream_protocol_error_uses_runtime_translation(self, monkeypatch):
        import iac_code.providers.streaming as streaming

        monkeypatch.setattr(streaming, "_", lambda message: f"translated:{message}")
        client = FakeOpenAIClient(
            stream_chunks=[_chunk(content="<think>secret"), _chunk(finish_reason="stop")]
        )
        provider = QwenProvider(model="qwen3.7-plus", client=client)
        with pytest.raises(UnsafeStreamProtocolError, match=r"^translated:Qwen emitted"):
            _ = [event async for event in provider.stream([Message.user("hi")], "sys")]

    async def test_tag_probe_literal_conflict_and_balanced_sequences(self):
        literal = FakeOpenAIClient(
            stream_chunks=[_chunk(content="<t"), _chunk(content="ext"), _chunk(finish_reason="stop")]
        )
        events = [
            event
            async for event in QwenProvider(model="qwen3.7-plus", client=literal).stream(
                [Message.user("hi")], "sys"
            )
        ]
        assert "".join(event.text for event in events if isinstance(event, TextDeltaEvent)) == "<text"

        balanced = FakeOpenAIClient(
            stream_chunks=[
                _chunk(content="<think>a</think><thinking>b</thinking> tail"),
                _chunk(finish_reason="stop"),
            ]
        )
        events = [
            event
            async for event in QwenProvider(model="qwen3.7-plus", client=balanced).stream(
                [Message.user("hi")], "sys"
            )
        ]
        assert "".join(event.text for event in events if isinstance(event, TextDeltaEvent)).endswith(" tail")

        conflict = FakeOpenAIClient(
            stream_chunks=[
                _chunk(reasoning="structured", content="<think>leak</think>"),
                _chunk(finish_reason="stop"),
            ]
        )
        with pytest.raises(UnsafeStreamProtocolError):
            _ = [
                event
                async for event in QwenProvider(model="qwen3.7-plus", client=conflict).stream(
                    [Message.user("hi")], "sys"
                )
            ]

        trailing_unclosed = FakeOpenAIClient(
            stream_chunks=[
                _chunk(content="<think>ok</think><think>leak"),
                _chunk(finish_reason="stop"),
            ]
        )
        with pytest.raises(UnsafeStreamProtocolError):
            _ = [
                event
                async for event in QwenProvider(model="qwen3.7-plus", client=trailing_unclosed).stream(
                    [Message.user("hi")], "sys"
                )
            ]

    async def test_standalone_closing_tag_is_removed_only_for_strict_native_tool_call(self):
        valid = FakeOpenAIClient(
            stream_chunks=[
                _chunk(
                    content="</think>",
                    reasoning="thought",
                    tool_calls=[ns(index=0, id="call_1", function=ns(name="read_file", arguments="{}"))],
                ),
                _chunk(finish_reason="tool_calls"),
            ]
        )
        events = [
            event
            async for event in QwenProvider(model="qwen3.7-plus", client=valid).stream(
                [Message.user("hi")], "sys", [_tool()]
            )
        ]
        assert not any(isinstance(event, TextDeltaEvent) for event in events)
        assert "".join(event.text for event in events if isinstance(event, ThinkingDeltaEvent)) == "thought"
        assert len([event for event in events if isinstance(event, ToolUseEndEvent)]) == 1

        invalid = FakeOpenAIClient(
            stream_chunks=[
                _chunk(
                    content="</think>",
                    tool_calls=[ns(index=0, id="call_1", function=ns(name="read_file", arguments="   "))],
                ),
                _chunk(finish_reason="tool_calls"),
            ]
        )
        with pytest.raises(UnsafeStreamProtocolError):
            _ = [
                event
                async for event in QwenProvider(model="qwen3.7-plus", client=invalid).stream(
                    [Message.user("hi")], "sys", [_tool()]
                )
            ]

    async def test_strict_native_tool_call_and_xml_fallback(self):
        native = FakeOpenAIClient(
            stream_chunks=[
                _chunk(
                    tool_calls=[ns(index=0, id="call_1", function=ns(name="read_file", arguments='{"path":'))]
                ),
                _chunk(tool_calls=[ns(index=0, id=None, function=ns(name=None, arguments='"a.py"}'))]),
                _chunk(finish_reason="tool_calls"),
            ]
        )
        provider = QwenProvider(model="qwen3.7-plus", client=native)
        events = [event async for event in provider.stream([Message.user("hi")], "sys", [_tool()])]
        assert [event.input for event in events if isinstance(event, ToolUseEndEvent)] == [{"path": "a.py"}]

        xml = (
            '<function_calls><invoke name="read_file"><parameter name="path">a.py</parameter>'
            "</invoke></function_calls>"
        )
        fallback = FakeOpenAIClient(stream_chunks=[_chunk(content=xml), _chunk(finish_reason="stop")])
        provider = QwenProvider(model="qwen3.7-plus", client=fallback)
        events = [event async for event in provider.stream([Message.user("hi")], "sys", [_tool()])]
        assert [event.input for event in events if isinstance(event, ToolUseEndEvent)] == [{"path": "a.py"}]
        assert not any(isinstance(event, TextDeltaEvent) for event in events)

    async def test_real_dashscope_empty_tool_delimiters_do_not_create_anonymous_call(self):
        client = FakeOpenAIClient(
            stream_chunks=[
                _chunk(
                    tool_calls=[ns(index=0, id="call_1", function=ns(name="read_file", arguments=""))]
                ),
                _chunk(tool_calls=[ns(index=0, id="", function=ns(name=None, arguments=""))]),
                _chunk(tool_calls=[ns(index=0, id="", function=ns(name=None, arguments='{"path": '))]),
                _chunk(tool_calls=[ns(index=0, id="", function=ns(name=None, arguments='"a.py"}'))]),
                _chunk(tool_calls=[ns(index=0, id="", function=ns(name=None, arguments=""))]),
                _chunk(finish_reason="tool_calls"),
            ]
        )
        events = [
            event
            async for event in QwenProvider(model="qwen3.7-plus", client=client).stream(
                [Message.user("hi")], "sys", [_tool()]
            )
        ]
        assert [event.input for event in events if isinstance(event, ToolUseEndEvent)] == [
            {"path": "a.py"}
        ]
        assert next(event for event in events if isinstance(event, MessageEndEvent)).stop_reason == "tool_use"

    async def test_malformed_native_call_does_not_fall_through_to_xml(self):
        client = FakeOpenAIClient(
            stream_chunks=[
                _chunk(
                    content='<invoke name="read_file"><parameter name="path">a.py</parameter></invoke>',
                    tool_calls=[ns(index=0, id="call_1", function=ns(name="read_file", arguments="   "))],
                ),
                _chunk(finish_reason="tool_calls"),
            ]
        )
        provider = QwenProvider(model="qwen3.7-plus", client=client)
        with pytest.raises(ValueError):
            _ = [event async for event in provider.stream([Message.user("hi")], "sys", [_tool()])]

    async def test_non_streaming_reasoning_and_xml(self):
        xml = '<invoke name="read_file"><parameter name="path">a.py</parameter></invoke>'
        client = FakeOpenAIClient(create_response=_response(content=xml, reasoning="thought"))
        provider = QwenProvider(model="qwen3.7-plus", client=client)
        response = await provider.complete([Message.user("hi")], "sys", [_tool()])
        assert response.thinking == "thought"
        assert response.text == ""
        assert response.tool_uses[0]["input"] == {"path": "a.py"}

    @pytest.mark.parametrize(
        "quoted",
        [
            '> <invoke name="read_file"><parameter name="path">a.py</parameter></invoke>',
            (
                '> <function_calls><invoke name="read_file"><parameter name="path">a.py</parameter>'
                "</invoke></function_calls>"
            ),
        ],
    )
    async def test_markdown_quoted_xml_is_never_executed(self, quoted):
        stream_client = FakeOpenAIClient(
            stream_chunks=[_chunk(content=quoted), _chunk(finish_reason="stop")]
        )
        events = [
            event
            async for event in QwenProvider(model="qwen3.7-plus", client=stream_client).stream(
                [Message.user("hi")], "sys", [_tool()]
            )
        ]
        assert "".join(event.text for event in events if isinstance(event, TextDeltaEvent)) == quoted
        assert not any(isinstance(event, ToolUseEndEvent) for event in events)

        complete_client = FakeOpenAIClient(create_response=_response(content=quoted))
        response = await QwenProvider(model="qwen3.7-plus", client=complete_client).complete(
            [Message.user("hi")], "sys", [_tool()]
        )
        assert response.text == quoted
        assert response.tool_uses == []

    @pytest.mark.parametrize("split_at", range(1, len("<invoke")))
    async def test_streaming_xml_prefix_split_and_leading_text(self, split_at):
        xml = '<invoke name="read_file"><parameter name="path">a.py</parameter></invoke>'
        client = FakeOpenAIClient(
            stream_chunks=[
                _chunk(content="lead" + xml[:split_at]),
                _chunk(content=xml[split_at:]),
                _chunk(finish_reason="stop"),
            ]
        )
        events = [
            event
            async for event in QwenProvider(model="qwen3.7-plus", client=client).stream(
                [Message.user("hi")], "sys", [_tool()]
            )
        ]
        assert "".join(event.text for event in events if isinstance(event, TextDeltaEvent)) == "lead"
        assert [event.input for event in events if isinstance(event, ToolUseEndEvent)] == [{"path": "a.py"}]
        assert next(event for event in events if isinstance(event, MessageEndEvent)).stop_reason == "tool_use"

    async def test_native_tool_call_wins_over_xml_and_missing_finish_does_not_recover(self):
        xml = '<invoke name="read_file"><parameter name="path">xml.py</parameter></invoke>'
        native = FakeOpenAIClient(
            stream_chunks=[
                _chunk(
                    content=xml,
                    tool_calls=[
                        ns(index=0, id="call_1", function=ns(name="read_file", arguments='{"path":"native.py"}'))
                    ],
                ),
                _chunk(finish_reason="tool_calls"),
            ]
        )
        events = [
            event
            async for event in QwenProvider(model="qwen3.7-plus", client=native).stream(
                [Message.user("hi")], "sys", [_tool()]
            )
        ]
        assert "".join(event.text for event in events if isinstance(event, TextDeltaEvent)) == xml
        assert [event.input for event in events if isinstance(event, ToolUseEndEvent)] == [
            {"path": "native.py"}
        ]

        no_finish = FakeOpenAIClient(stream_chunks=[_chunk(content=xml)])
        events = [
            event
            async for event in QwenProvider(model="qwen3.7-plus", client=no_finish).stream(
                [Message.user("hi")], "sys", [_tool()]
            )
        ]
        assert "".join(event.text for event in events if isinstance(event, TextDeltaEvent)) == xml
        assert not any(isinstance(event, ToolUseEndEvent) for event in events)
