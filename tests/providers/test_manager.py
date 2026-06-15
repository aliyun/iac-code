import asyncio
from unittest.mock import AsyncMock

import pytest

from iac_code.providers.base import Message, NonStreamingResponse
from iac_code.providers.manager import ProviderManager, _detect_provider_name, create_provider
from iac_code.types.stream_events import MessageEndEvent, MessageStartEvent, TextDeltaEvent, Usage


async def _collect_stream_events(stream):
    return [event async for event in stream]


class TestCreateProvider:
    def test_anthropic(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        p = create_provider("claude-sonnet-4-6", credentials={"anthropic": "key"})
        assert p.get_model_name() == "claude-sonnet-4-6"

    def test_openai(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "openai")
        p = create_provider("gpt-4.1", credentials={"openai": "key"})
        assert p.get_model_name() == "gpt-4.1"

    def test_dashscope(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "dashscope")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {})
        p = create_provider("qwen3.6-plus", credentials={"dashscope": "key"})
        assert p.get_model_name() == "qwen3.6-plus"
        assert getattr(p, "_effort", None) is None

    def test_dashscope_loads_effort_from_settings(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "dashscope")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {"effort": "max"})
        p = create_provider("deepseek-v4-pro", credentials={"dashscope": "key"})
        assert getattr(p, "_effort", None) == "max"

    def test_unknown_raises(self, monkeypatch):
        """Unknown model with no saved provider config raises ValueError."""
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
        with pytest.raises(ValueError, match="Cannot determine provider"):
            create_provider("unknown-model", credentials={})

    def test_openapi_compatible(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "openapi_compatible")
        monkeypatch.setattr(
            "iac_code.config.get_provider_config",
            lambda name: {"apiBase": "https://my.llm.local/v1"},
        )
        p = create_provider("any-model", credentials={"openapi_compatible": "sk-x"})
        assert p.get_model_name() == "any-model"
        assert p._base_url == "https://my.llm.local/v1"

    def test_dashscope_token_plan(self, monkeypatch):
        from iac_code.providers.dashscope_provider import (
            DASHSCOPE_TOKEN_PLAN_BASE_URL,
            DashScopeProvider,
        )

        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "dashscope_token_plan")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {})
        p = create_provider("qwen3.6-plus", credentials={"dashscope_token_plan": "tp-key"})
        assert isinstance(p, DashScopeProvider)
        assert p.get_model_name() == "qwen3.6-plus"
        assert p._base_url == DASHSCOPE_TOKEN_PLAN_BASE_URL
        assert p._PROVIDER_KEY == "dashscope_token_plan"
        assert getattr(p, "_effort", None) is None

    def test_dashscope_token_plan_uses_token_plan_credential_slot(self, monkeypatch):
        # The dashscope (regular) credential must NOT leak into the token plan
        # provider — only credentials["dashscope_token_plan"] is consumed.
        from iac_code.providers.dashscope_provider import DashScopeProvider

        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "dashscope_token_plan")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {})
        p = create_provider(
            "qwen3.6-plus",
            credentials={"dashscope": "regular-key", "dashscope_token_plan": "tp-key"},
        )
        assert isinstance(p, DashScopeProvider)
        assert p._client.api_key == "tp-key"


class TestProviderManager:
    def test_get_fallback(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        m = ProviderManager(model="claude-opus-4-7", credentials={})
        assert m._get_fallback_model() == "claude-haiku-4-5-20251001"

    def test_no_fallback_cheapest(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        m = ProviderManager(model="claude-haiku-4-5-20251001", credentials={})
        assert m._get_fallback_model() is None

    def test_deferred_init_when_no_active_provider(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
        m = ProviderManager(model="custom-model", credentials={})
        assert m._provider is None

    def test_ensure_provider_raises_when_still_unconfigured(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
        m = ProviderManager(model="custom-model", credentials={})
        with pytest.raises(ValueError, match="Cannot determine provider"):
            m._ensure_provider()

    def test_ensure_provider_lazy_success(self, monkeypatch):
        # First call: no provider configured, model name not auto-mappable
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
        m = ProviderManager(model="custom-model", credentials={"anthropic": "k"})
        assert m._provider is None
        # Second call: user configured provider via /auth
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        provider = m._ensure_provider()
        assert provider.get_model_name() == "custom-model"

    def test_unknown_model_no_fallback(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        m = ProviderManager(model="some-model-without-fallback", credentials={})
        assert m._get_fallback_model() is None

    def test_provider_key_and_display_use_runtime_override(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "openai")
        monkeypatch.setattr("iac_code.config.get_provider_config", lambda name: {})
        m = ProviderManager(
            model="qwen3.6-plus",
            credentials={"dashscope_token_plan": "tp-key"},
            provider_key_override="dashscope_token_plan",
        )

        assert m.get_provider_key() == "dashscope_token_plan"
        assert m.get_provider_display() == "Alibaba Cloud Bailian Token Plan"

    def test_reconfigure_swaps_model_and_credentials(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        m = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "old"})
        original_provider = m._provider
        assert original_provider is not None

        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "openai")
        m.reconfigure("gpt-5.5", {"openai": "new"})

        assert m.get_model_name() == "gpt-5.5"
        assert m._credentials == {"openai": "new"}
        # Underlying provider was rebuilt — different instance from before.
        assert m._provider is not None
        assert m._provider is not original_provider
        assert m._provider.get_model_name() == "gpt-5.5"

    def test_reconfigure_recovers_from_unconfigured(self, monkeypatch):
        # Start with no active provider — manager defers provider init.
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
        m = ProviderManager(model="custom-model", credentials={})
        assert m._provider is None

        # User runs /auth — reconfigure should now build the provider.
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        m.reconfigure("claude-sonnet-4-6", {"anthropic": "k"})
        assert m._provider is not None

    def test_reconfigure_stays_lazy_when_no_provider_configured(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        m = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        assert m._provider is not None

        # Reconfigure with no active provider key → underlying provider drops
        # to None and stays None until the user configures something.
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
        m.reconfigure("some-model", {})
        assert m._provider is None
        assert m.get_model_name() == "some-model"

    def test_failure_telemetry_uses_public_error_summary(self, monkeypatch):
        from iac_code.services.telemetry.names import Events

        telemetry_events = []
        monkeypatch.setattr(
            "iac_code.providers.manager.log_event",
            lambda name, attrs: telemetry_events.append((name, attrs)),
        )
        monkeypatch.setattr("iac_code.providers.manager.add_metric", lambda *args, **kwargs: None)

        ProviderManager._emit_failure_telemetry(
            "dashscope",
            "qwen3.6-plus",
            0.0,
            RuntimeError("Authorization: Bearer sk-live at /Users/alice/.iac-code/settings.yml"),
        )

        _, attrs = next(item for item in telemetry_events if item[0] == Events.API_REQUEST_FAILED)
        assert attrs["error_id"]
        assert "sk-live" not in attrs["error_message"]
        assert "/Users/alice" not in attrs["error_message"]
        assert "[REDACTED]" in attrs["error_message"]


@pytest.mark.asyncio
class TestProviderManagerStreaming:
    @pytest.fixture(autouse=True)
    def _active_provider(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")

    async def test_stream_success(self):
        mock_provider = AsyncMock()

        async def fake_stream(*a, **kw):
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="hello")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        mock_provider.stream = fake_stream
        mock_provider.get_model_name.return_value = "test"
        mgr = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        mgr._provider = mock_provider
        events = [e async for e in mgr.stream(messages=[Message.user("hi")], system="sys")]
        types = [e.type for e in events]
        assert "message_start" in types and "text_delta" in types and "message_end" in types

    async def test_stream_fallback_tombstone(self):
        mock_provider = AsyncMock()

        async def failing_stream(*a, **kw):
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="partial")
            raise ConnectionError("stream died")

        mock_provider.stream = failing_stream
        mock_provider.get_model_name.return_value = "test"
        mock_provider.complete = AsyncMock(
            return_value=NonStreamingResponse(
                message_id="m2",
                text="complete",
                tool_uses=[],
                stop_reason="end_turn",
                usage=Usage(input_tokens=10, output_tokens=20),
            )
        )
        mgr = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        mgr._provider = mock_provider
        events = [e async for e in mgr.stream(messages=[Message.user("hi")], system="sys")]
        types = [e.type for e in events]
        assert "tombstone" in types and "text_delta" in types and "message_end" in types

    async def test_fallback_complete_also_fails_yields_error_event(self):
        mock_provider = AsyncMock()

        async def failing_stream(*a, **kw):
            yield MessageStartEvent(message_id="m1")
            raise ConnectionError("stream died")

        mock_provider.stream = failing_stream
        mock_provider.get_model_name.return_value = "test"
        mock_provider.complete = AsyncMock(side_effect=ValueError("irrecoverable"))

        mgr = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        mgr._provider = mock_provider
        # Shrink retry window so test is fast
        mgr._retry_config.max_retries = 0

        events = [e async for e in mgr.stream(messages=[Message.user("hi")], system="sys")]
        types = [e.type for e in events]
        assert "tombstone" in types
        assert "error" in types
        err = next(e for e in events if e.type == "error")
        assert err.error.startswith("ValueError:")
        assert "irrecoverable" in err.error
        assert err.error_id

    async def test_fallback_complete_error_event_redacts_public_error(self):
        mock_provider = AsyncMock()

        async def failing_stream(*a, **kw):
            yield MessageStartEvent(message_id="m1")
            raise ConnectionError("stream died")

        mock_provider.stream = failing_stream
        mock_provider.get_model_name.return_value = "test"
        mock_provider.complete = AsyncMock(
            side_effect=RuntimeError("Authorization: Bearer sk-live at /Users/alice/.iac-code/settings.yml")
        )

        mgr = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        mgr._provider = mock_provider
        mgr._retry_config.max_retries = 0

        events = [e async for e in mgr.stream(messages=[Message.user("hi")], system="sys")]
        err = next(e for e in events if e.type == "error")
        assert "sk-live" not in err.error
        assert "/Users/alice" not in err.error
        assert err.error.startswith("RuntimeError:")
        assert err.error_id

    async def test_fallback_error_event_preserves_original_exception_type_via_retry_wrapper(self):
        class RateLimitError(Exception):
            status_code = 429

        mock_provider = AsyncMock()

        async def failing_stream(*a, **kw):
            yield MessageStartEvent(message_id="m1")
            raise ConnectionError("stream died")

        mock_provider.stream = failing_stream
        mock_provider.get_model_name.return_value = "test"
        mock_provider.complete = AsyncMock(side_effect=RateLimitError("slow down"))

        mgr = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        mgr._provider = mock_provider
        mgr._retry_config.max_retries = 0

        events = [e async for e in mgr.stream(messages=[Message.user("hi")], system="sys")]
        err = next(e for e in events if e.type == "error")
        # RetryableError wraps RateLimitError; both names should appear in the diagnostic
        assert "RetryableError" in err.error
        assert "RateLimitError" in err.error
        assert "slow down" in err.error
        assert err.error_id

    async def test_stream_idle_timeout_recovers_with_non_streaming_fallback(self):
        class HangingStreamProvider:
            def get_model_name(self) -> str:
                return "claude-sonnet-4-6"

            async def stream(self, messages, system, tools=None, max_tokens=8192):
                await asyncio.sleep(999)
                yield MessageEndEvent(stop_reason="never", usage=Usage())

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                return NonStreamingResponse(
                    message_id="fallback-after-timeout",
                    text="recovered",
                    tool_uses=[],
                    stop_reason="end_turn",
                    usage=Usage(input_tokens=3, output_tokens=4),
                )

        mgr = ProviderManager(
            model="claude-sonnet-4-6",
            credentials={"anthropic": "k"},
            stream_idle_timeout=0.01,
        )
        mgr._provider = HangingStreamProvider()

        events = await asyncio.wait_for(
            _collect_stream_events(mgr.stream(messages=[Message.user("hi")], system="sys")),
            timeout=0.5,
        )

        assert [event.type for event in events] == ["message_start", "text_delta", "message_end"]
        assert events[0].message_id == "fallback-after-timeout"
        assert events[1].text == "recovered"

    async def test_stream_idle_timeout_after_partial_message_yields_tombstone_then_fallback(self):
        class HangingAfterStartProvider:
            def get_model_name(self) -> str:
                return "claude-sonnet-4-6"

            async def stream(self, messages, system, tools=None, max_tokens=8192):
                yield MessageStartEvent(message_id="partial-message")
                await asyncio.sleep(999)
                yield MessageEndEvent(stop_reason="never", usage=Usage())

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                return NonStreamingResponse(
                    message_id="fallback-after-partial-timeout",
                    text="recovered",
                    tool_uses=[],
                    stop_reason="end_turn",
                    usage=Usage(input_tokens=3, output_tokens=4),
                )

        mgr = ProviderManager(
            model="claude-sonnet-4-6",
            credentials={"anthropic": "k"},
            stream_idle_timeout=0.01,
        )
        mgr._provider = HangingAfterStartProvider()

        events = await asyncio.wait_for(
            _collect_stream_events(mgr.stream(messages=[Message.user("hi")], system="sys")),
            timeout=0.5,
        )

        assert [event.type for event in events] == [
            "message_start",
            "tombstone",
            "message_start",
            "text_delta",
            "message_end",
        ]
        assert events[0].message_id == "partial-message"
        assert events[1].message_id == "partial-message"
        assert events[2].message_id == "fallback-after-partial-timeout"
        assert events[3].text == "recovered"

    async def test_stream_cancelled_error_propagates_without_fallback(self):
        class CancellingStreamProvider:
            def get_model_name(self) -> str:
                return "claude-sonnet-4-6"

            async def stream(self, messages, system, tools=None, max_tokens=8192):
                yield MessageStartEvent(message_id="partial-before-cancel")
                raise asyncio.CancelledError()

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                raise AssertionError("cancellation must not call non-streaming fallback")

        mgr = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        mgr._provider = CancellingStreamProvider()

        with pytest.raises(asyncio.CancelledError):
            await _collect_stream_events(mgr.stream(messages=[Message.user("hi")], system="sys"))

    async def test_stream_fallback_records_fallback_response_model_without_mutating_state(self, monkeypatch):
        from iac_code.providers.retry import RetryConfig
        from iac_code.services.telemetry.names import Events, GenAiAttr
        from iac_code.services.telemetry.sanitize import sanitize_model_name

        class Status503Error(Exception):
            status_code = 503

        class PrimaryProvider:
            def get_model_name(self) -> str:
                return "claude-sonnet-4-6"

            async def stream(self, messages, system, tools=None, max_tokens=8192):
                yield MessageStartEvent(message_id="primary-stream")
                raise ConnectionError("stream died")

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                raise Status503Error("primary complete outage")

        class FallbackProvider:
            def get_model_name(self) -> str:
                return "claude-haiku-4-5-20251001"

            async def stream(self, messages, system, tools=None, max_tokens=8192):
                raise AssertionError("stream fallback should use non-streaming complete")

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                return NonStreamingResponse(
                    message_id="fallback-response",
                    text="fallback text",
                    tool_uses=[],
                    stop_reason="end_turn",
                    usage=Usage(input_tokens=5, output_tokens=6),
                )

        class RecordingSpan:
            def __init__(self):
                self.attributes = {}

            def set_attribute(self, key, value):
                self.attributes[key] = value

        class RecordingSpanContext:
            def __init__(self, span):
                self.span = span

            def __enter__(self):
                return self.span

            def __exit__(self, exc_type, exc, tb):
                return None

        span = RecordingSpan()
        telemetry_events = []

        monkeypatch.setattr(
            "iac_code.providers.manager.create_provider",
            lambda model, credentials, *, base_url=None, provider_key_override=None: (
                FallbackProvider() if model == "claude-haiku-4-5-20251001" else PrimaryProvider()
            ),
        )
        monkeypatch.setattr(
            "iac_code.providers.manager.start_span",
            lambda name, attrs=None: RecordingSpanContext(span),
        )
        monkeypatch.setattr(
            "iac_code.providers.manager.log_event",
            lambda name, attrs: telemetry_events.append((name, attrs)),
        )

        mgr = ProviderManager(
            model="claude-sonnet-4-6",
            credentials={"anthropic": "k"},
            retry_config=RetryConfig(max_retries=0, base_delay=0, jitter_factor=0),
        )

        events = await _collect_stream_events(mgr.stream(messages=[Message.user("hi")], system="sys"))

        success_event = next(attrs for name, attrs in telemetry_events if name == Events.API_REQUEST_SUCCEEDED)
        assert [event.type for event in events] == [
            "message_start",
            "tombstone",
            "message_start",
            "text_delta",
            "message_end",
        ]
        assert span.attributes[GenAiAttr.RESPONSE_MODEL] == "claude-haiku-4-5-20251001"
        assert success_event["provider"] == "fallback"
        assert success_event["model"] == sanitize_model_name("claude-haiku-4-5-20251001")
        assert mgr.get_model_name() == "claude-sonnet-4-6"

    async def test_qwenpaw_config_error_yields_error_event_instead_of_system_exit(self, monkeypatch):
        from iac_code.services.qwenpaw_source import QwenPawError

        monkeypatch.setattr(
            "iac_code.config._get_env_overrides",
            lambda: {"api_key": None, "model": None, "base_url": None, "provider_key": None},
        )
        monkeypatch.setattr("iac_code.config.get_llm_source", lambda: "qwenpaw")
        monkeypatch.setattr(
            "iac_code.services.qwenpaw_source.load_from_qwenpaw",
            lambda: (_ for _ in ()).throw(QwenPawError("bad qwenpaw config")),
        )

        mgr = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})

        events = await _collect_stream_events(mgr.stream(messages=[Message.user("hi")], system="sys"))

        assert len(events) == 1
        assert events[0].type == "error"
        assert "bad qwenpaw config" in events[0].error
        assert events[0].is_retryable is False
        assert events[0].error_id


@pytest.mark.asyncio
class TestProviderManagerCompleteRetry:
    @pytest.fixture(autouse=True)
    def _active_provider(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")

    async def test_retryable_status_429_retries_then_succeeds(self):
        from iac_code.providers.base import NonStreamingResponse
        from iac_code.providers.retry import RetryConfig
        from iac_code.types.stream_events import Usage

        class RateLimitError(Exception):
            status_code = 429

        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(
            side_effect=[
                RateLimitError("slow down"),
                NonStreamingResponse(message_id="m", text="ok", tool_uses=[], stop_reason="end_turn", usage=Usage()),
            ]
        )
        mgr = ProviderManager(
            model="claude-sonnet-4-6",
            credentials={"anthropic": "k"},
            retry_config=RetryConfig(max_retries=2, base_delay=0.01, jitter_factor=0.0),
        )
        mgr._provider = mock_provider

        result = await mgr.complete(messages=[Message.user("hi")], system="")
        assert result.text == "ok"
        assert mock_provider.complete.call_count == 2

    async def test_connection_error_is_retryable(self):
        from iac_code.providers.base import NonStreamingResponse
        from iac_code.providers.retry import RetryConfig
        from iac_code.types.stream_events import Usage

        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(
            side_effect=[
                ConnectionError("net"),
                NonStreamingResponse(message_id="m", text="ok", tool_uses=[], stop_reason="end_turn", usage=Usage()),
            ]
        )
        mgr = ProviderManager(
            model="claude-sonnet-4-6",
            credentials={"anthropic": "k"},
            retry_config=RetryConfig(max_retries=2, base_delay=0.01, jitter_factor=0.0),
        )
        mgr._provider = mock_provider

        result = await mgr.complete(messages=[Message.user("hi")], system="")
        assert result.text == "ok"
        assert mock_provider.complete.call_count == 2

    async def test_non_retryable_error_propagates(self):
        from iac_code.providers.retry import RetryConfig

        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(side_effect=ValueError("bad input"))
        mgr = ProviderManager(
            model="claude-sonnet-4-6",
            credentials={"anthropic": "k"},
            retry_config=RetryConfig(max_retries=3, base_delay=0.01, jitter_factor=0.0),
        )
        mgr._provider = mock_provider

        with pytest.raises(ValueError, match="bad input"):
            await mgr.complete(messages=[Message.user("hi")], system="")
        # ValueError has no status_code and isn't ConnectionError/TimeoutError/OSError,
        # so it should NOT be retried.
        assert mock_provider.complete.call_count == 1

    async def test_fallback_success_does_not_mutate_manager_state(self, monkeypatch):
        from iac_code.providers.retry import RetryConfig

        class Status503Error(Exception):
            status_code = 503

        class FakeProvider:
            def __init__(self, model: str, *, fail: bool = False):
                self.model = model
                self.fail = fail

            def get_model_name(self) -> str:
                return self.model

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                if self.fail:
                    raise Status503Error("temporary outage")
                return NonStreamingResponse(
                    message_id="fallback-response",
                    text="fallback ok",
                    tool_uses=[],
                    stop_reason="end_turn",
                    usage=Usage(input_tokens=1, output_tokens=2),
                )

        created_models: list[str] = []

        def fake_create_provider(model, credentials, *, base_url=None, provider_key_override=None):
            created_models.append(model)
            return FakeProvider(model, fail=model == "claude-sonnet-4-6")

        monkeypatch.setattr("iac_code.providers.manager.create_provider", fake_create_provider)
        mgr = ProviderManager(
            model="claude-sonnet-4-6",
            credentials={"anthropic": "k"},
            retry_config=RetryConfig(max_retries=0, base_delay=0, jitter_factor=0),
        )
        original_provider = mgr._provider

        response = await mgr.complete(messages=[Message.user("hi")], system="")

        assert response.text == "fallback ok"
        assert created_models == ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
        assert mgr.get_model_name() == "claude-sonnet-4-6"
        assert mgr._provider is original_provider

    async def test_fallback_provider_creation_failure_preserves_original_error(self, monkeypatch):
        from iac_code.providers.retry import RetryableError, RetryConfig

        class Status503Error(Exception):
            status_code = 503

        class PrimaryProvider:
            def get_model_name(self) -> str:
                return "claude-sonnet-4-6"

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                raise Status503Error("primary temporary outage")

        def fake_create_provider(model, credentials, *, base_url=None, provider_key_override=None):
            if model == "claude-sonnet-4-6":
                return PrimaryProvider()
            raise RuntimeError("fallback provider unavailable")

        monkeypatch.setattr("iac_code.providers.manager.create_provider", fake_create_provider)
        mgr = ProviderManager(
            model="claude-sonnet-4-6",
            credentials={"anthropic": "k"},
            retry_config=RetryConfig(max_retries=0, base_delay=0, jitter_factor=0),
        )

        with pytest.raises(RetryableError, match="primary temporary outage"):
            await mgr.complete(messages=[Message.user("hi")], system="")

    async def test_fallback_complete_failure_preserves_original_error(self, monkeypatch):
        from iac_code.providers.retry import RetryableError, RetryConfig

        class Status503Error(Exception):
            status_code = 503

        class PrimaryProvider:
            def get_model_name(self) -> str:
                return "claude-sonnet-4-6"

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                raise Status503Error("primary temporary outage")

        class FallbackProvider:
            def get_model_name(self) -> str:
                return "claude-haiku-4-5-20251001"

            async def complete(self, messages, system, tools=None, max_tokens=8192):
                raise RuntimeError("fallback complete failed")

        created_models: list[str] = []

        def fake_create_provider(model, credentials, *, base_url=None, provider_key_override=None):
            created_models.append(model)
            if model == "claude-sonnet-4-6":
                return PrimaryProvider()
            return FallbackProvider()

        monkeypatch.setattr("iac_code.providers.manager.create_provider", fake_create_provider)
        mgr = ProviderManager(
            model="claude-sonnet-4-6",
            credentials={"anthropic": "k"},
            retry_config=RetryConfig(max_retries=0, base_delay=0, jitter_factor=0),
        )

        with pytest.raises(RetryableError, match="primary temporary outage") as exc_info:
            await mgr.complete(messages=[Message.user("hi")], system="")

        assert "fallback complete failed" not in str(exc_info.value)
        assert created_models == ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"]


class TestModelPrefixAutoMapping:
    """_detect_provider_name falls back to model-name prefix heuristics."""

    @pytest.mark.parametrize(
        "model, expected_provider",
        [
            ("claude-sonnet-4-6", "anthropic"),
            ("claude-opus-4-7", "anthropic"),
            ("claude-haiku-4-5-20251001", "anthropic"),
            ("gpt-4o", "openai"),
            ("gpt-5.5", "openai"),
            ("o1-preview", "openai"),
            ("o3-mini", "openai"),
            ("qwen3.6-plus", "dashscope"),
            ("qwen-max", "dashscope"),
            ("deepseek-v4-pro", "deepseek"),
            ("deepseek-chat", "deepseek"),
        ],
    )
    def test_auto_maps_mainstream_models(self, monkeypatch, model, expected_provider):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
        assert _detect_provider_name(model) == expected_provider

    def test_saved_config_takes_precedence_over_prefix(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "openai")
        assert _detect_provider_name("claude-sonnet-4-6") == "openai"

    def test_unknown_model_still_raises(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
        with pytest.raises(ValueError, match="Cannot determine provider"):
            _detect_provider_name("totally-unknown-model")

    def test_auto_mapped_model_without_api_key_raises(self, monkeypatch):
        """Model prefix resolves the provider, but empty credential raises ValueError."""
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
        with pytest.raises(ValueError, match="No API key configured for provider"):
            create_provider("claude-sonnet-4-6", credentials={"anthropic": ""})
