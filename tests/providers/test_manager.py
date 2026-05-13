from unittest.mock import AsyncMock

import pytest

from iac_code.providers.base import Message, NonStreamingResponse
from iac_code.providers.manager import ProviderManager, create_provider
from iac_code.types.stream_events import MessageEndEvent, MessageStartEvent, TextDeltaEvent, Usage


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
        m = ProviderManager(model="claude-sonnet-4-6", credentials={})
        assert m._provider is None

    def test_ensure_provider_raises_when_still_unconfigured(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
        m = ProviderManager(model="claude-sonnet-4-6", credentials={})
        with pytest.raises(ValueError, match="Cannot determine provider"):
            m._ensure_provider()

    def test_ensure_provider_lazy_success(self, monkeypatch):
        # First call: no provider configured
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
        m = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})
        assert m._provider is None
        # Second call: user configured provider via /auth
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        provider = m._ensure_provider()
        assert provider.get_model_name() == "claude-sonnet-4-6"

    def test_unknown_model_no_fallback(self, monkeypatch):
        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "anthropic")
        m = ProviderManager(model="some-model-without-fallback", credentials={})
        assert m._get_fallback_model() is None

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
        m = ProviderManager(model="claude-sonnet-4-6", credentials={})
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
        assert "irrecoverable" in err.error


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
