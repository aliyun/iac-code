"""Provider selection, streaming fallback with tombstone, and model degradation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from loguru import logger

from iac_code.i18n import _
from iac_code.providers.base import Message, NonStreamingResponse, Provider, ToolDefinition
from iac_code.providers.request_policy import ProviderRequestPolicy, bool_or_none
from iac_code.providers.retry import RetryableError, RetryConfig, with_retry
from iac_code.providers.stream_watchdog import StreamWatchdog
from iac_code.services.telemetry import add_metric, get_session_id, log_event, start_span
from iac_code.services.telemetry.config import should_capture_content_on_span
from iac_code.services.telemetry.content_serializer import (
    serialize_input_messages,
    serialize_system_instructions,
    serialize_tool_definitions,
)
from iac_code.services.telemetry.names import (
    Events,
    GenAiAttr,
    GenAiOperationName,
    GenAiSpanKind,
    Metrics,
    Spans,
)
from iac_code.services.telemetry.sanitize import sanitize_error_message, sanitize_model_name
from iac_code.types.stream_events import (
    ErrorEvent,
    MessageEndEvent,
    MessageStartEvent,
    StreamEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    TombstoneEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)
from iac_code.utils.public_errors import public_error, public_exception_summary


class ProviderNotConfiguredError(ValueError):
    """Raised when the LLM provider cannot be determined or has no API key."""


class ProviderConfigurationError(RuntimeError):
    """Raised when provider configuration cannot be loaded during a request."""


@dataclass(frozen=True)
class _CompletionResult:
    response: NonStreamingResponse
    model: str
    provider_name: str


def _error_event_from_exception(exc: BaseException) -> ErrorEvent:
    summary = public_exception_summary(exc, max_chars=1000)
    failure = public_error(message=summary, error_type=type(exc).__name__)
    return ErrorEvent(error=summary, is_retryable=False, error_id=failure.error_id)


MODEL_FALLBACK_MAP = {
    "claude-opus-4-7": "claude-haiku-4-5-20251001",
    "claude-opus-4-6": "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6": "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6-1m": "claude-haiku-4-5-20251001",
    "gpt-5.5": "gpt-5.4",
    "gpt-5.4": "gpt-5.4-mini",
    "qwen3.6-plus": "qwen3.5-plus",
    "deepseek-v4-pro": "deepseek-v4-flash",
}


def _detect_provider_name(model: str) -> str:
    """Detect provider from saved settings, falling back to model-name heuristics.

    Priority:
    1. Saved config in settings.yml (set by /auth or /model).
    2. Model-name prefix matching for mainstream models.
    """
    from iac_code.config import _KEY_NAME_TO_CRED_SLOT, _MODEL_PREFIX_TO_PROVIDER, get_active_provider_key

    key_name = get_active_provider_key() or ""
    if key_name in _KEY_NAME_TO_CRED_SLOT:
        return _KEY_NAME_TO_CRED_SLOT[key_name]

    model_lower = model.lower()
    for prefix, provider in _MODEL_PREFIX_TO_PROVIDER:
        if model_lower.startswith(prefix):
            return provider

    raise ProviderNotConfiguredError(
        _("Cannot determine provider for model: {model}. Run /auth to configure.").format(model=model)
    )


def create_provider(
    model: str,
    credentials: dict[str, str],
    *,
    base_url: str | None = None,
    provider_key_override: str | None = None,
    request_policy_override: ProviderRequestPolicy | None = None,
) -> Provider:
    from iac_code.providers.registry import PROVIDER_REGISTRY

    provider_key = provider_key_override or _detect_provider_name(model)
    desc = PROVIDER_REGISTRY.get(provider_key)
    if desc is None:
        raise ProviderNotConfiguredError(
            _("Unknown provider key: '{key}'. Run /auth to configure.").format(key=provider_key)
        )
    from iac_code.config import get_provider_config

    provider_cfg = get_provider_config(provider_key)
    effective_base_url = base_url or desc.base_url
    if not effective_base_url:
        saved_base = provider_cfg.get("apiBase")
        if isinstance(saved_base, str) and saved_base:
            effective_base_url = saved_base
    effort_value = _get_provider_config_value(provider_cfg, model, "effort")
    effort = effort_value if isinstance(effort_value, str) else None
    if request_policy_override is not None and request_policy_override.effort is not None:
        effort = request_policy_override.effort
    thinking_enabled = _get_bool_provider_config_value(provider_cfg, model, "thinkingEnabled")
    if request_policy_override is not None and request_policy_override.thinking_enabled is not None:
        thinking_enabled = request_policy_override.thinking_enabled
    thinking_budget = _get_positive_int_provider_config_value(provider_cfg, model, "thinkingBudget")
    max_completion_tokens = _get_positive_int_provider_config_value(provider_cfg, model, "maxCompletionTokens")
    if request_policy_override is not None and request_policy_override.thinking_budget is not None:
        thinking_budget = request_policy_override.thinking_budget
    if request_policy_override is not None and request_policy_override.max_completion_tokens is not None:
        max_completion_tokens = request_policy_override.max_completion_tokens
    wire_provider_key = _wire_provider_key_for_openai_compatible_base(
        provider_key,
        effective_base_url,
    )
    api_key = credentials.get(provider_key, "")
    if not api_key and wire_provider_key != provider_key:
        api_key = credentials.get(wire_provider_key, "")
    if desc.require_api_key and not api_key:
        raise ProviderNotConfiguredError(
            _("No API key configured for provider '{provider}' (model: {model}). Run /auth to configure.").format(
                provider=desc.display_name, model=model
            )
        )
    provider_class_path = desc.provider_class
    if wire_provider_key != provider_key:
        wire_desc = PROVIDER_REGISTRY.get(wire_provider_key)
        if wire_desc is not None:
            provider_class_path = wire_desc.provider_class
    provider_cls = _import_provider_class(provider_class_path)
    request_policy_kwargs: dict[str, Any] = {}
    if thinking_enabled is not None:
        request_policy_kwargs["thinking_enabled"] = thinking_enabled
    from iac_code.providers.openai_provider import OpenAIProvider

    if issubclass(provider_cls, OpenAIProvider):
        if thinking_budget is not None:
            request_policy_kwargs["thinking_budget"] = thinking_budget
        if max_completion_tokens is not None:
            request_policy_kwargs["max_completion_tokens"] = max_completion_tokens
    else:
        from iac_code.providers.anthropic_provider import AnthropicProvider

        if (
            request_policy_override is not None
            and request_policy_override.thinking_budget is not None
            and issubclass(provider_cls, AnthropicProvider)
        ):
            request_policy_kwargs["thinking_budget"] = request_policy_override.thinking_budget
    return provider_cls(
        model=model,
        api_key=api_key or None,
        base_url=effective_base_url,
        effort=effort,
        provider_key=wire_provider_key,
        **request_policy_kwargs,
    )


def _import_provider_class(dotted_path: str):
    """Lazily import a provider class from its dotted path."""
    module_path, class_name = dotted_path.rsplit(".", 1)
    import importlib

    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def _get_model_provider_config(provider_cfg: dict[str, Any], model: str) -> dict[str, Any]:
    models = provider_cfg.get("models")
    if not isinstance(models, dict):
        return {}
    raw = models.get(model)
    return raw if isinstance(raw, dict) else {}


def _get_provider_config_value(provider_cfg: dict[str, Any], model: str, key: str) -> Any:
    model_cfg = _get_model_provider_config(provider_cfg, model)
    if key in model_cfg:
        return model_cfg[key]
    return provider_cfg.get(key)


def _get_positive_int_provider_config_value(provider_cfg: dict[str, Any], model: str, key: str) -> int | None:
    model_cfg = _get_model_provider_config(provider_cfg, model)
    if key in model_cfg:
        model_value = _positive_int_or_none(model_cfg[key])
        if model_value is not None:
            return model_value
    return _positive_int_or_none(provider_cfg.get(key))


def _get_bool_provider_config_value(provider_cfg: dict[str, Any], model: str, key: str) -> bool | None:
    model_cfg = _get_model_provider_config(provider_cfg, model)
    if key in model_cfg:
        model_value = bool_or_none(model_cfg[key])
        if model_value is not None:
            return model_value
    return bool_or_none(provider_cfg.get(key))


def _wire_provider_key_for_openai_compatible_base(
    provider_key: str,
    base_url: str | None,
) -> str:
    if provider_key != "openai_compatible" or not isinstance(base_url, str):
        return provider_key
    lower_base_url = base_url.lower()
    if "token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode" in lower_base_url:
        return "dashscope_token_plan"
    if "dashscope.aliyuncs.com/compatible-mode" in lower_base_url:
        return "dashscope"
    return provider_key


def _positive_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped.isdigit():
        return None
    parsed = int(stripped)
    return parsed if parsed > 0 else None


def _active_request_policy(policy: ProviderRequestPolicy | None) -> ProviderRequestPolicy | None:
    if policy is None or not policy.has_values:
        return None
    return policy


class ProviderManager:
    """Manages provider lifecycle, streaming fallback, and model degradation.
    When streaming fails mid-way:
    1. Yield TombstoneEvents for orphaned partial messages
    2. Fall back to non-streaming complete() call
    3. Yield the complete response as events
    """

    def __init__(
        self,
        model: str,
        credentials: dict[str, str],
        retry_config: RetryConfig | None = None,
        stream_idle_timeout: float = 90.0,
        provider_key_override: str | None = None,
        base_url_override: str | None = None,
        request_policy_override: ProviderRequestPolicy | None = None,
    ):
        self._model = model
        self._credentials = credentials
        self._retry_config = retry_config or RetryConfig()
        self._stream_idle_timeout = stream_idle_timeout
        self._provider_key_override = provider_key_override
        self._base_url_override = base_url_override
        self._request_policy_override = _active_request_policy(request_policy_override)
        # Lazy: first startup may have no active provider yet. Defer errors
        # until the user actually tries to send a message, so /auth is reachable.
        self._provider: Provider | None = None
        try:
            self._provider = create_provider(
                model,
                credentials,
                **self._provider_create_kwargs(),
            )
        except ValueError as e:
            logger.warning(f"Provider not configured yet: {e}")

    def _check_qwenpaw_config_change(self) -> None:
        """Detect QwenPaw active_model.json changes and reconfigure if needed."""
        from iac_code.config import _get_env_overrides, get_llm_source

        env = _get_env_overrides()
        if env["api_key"]:
            return
        if get_llm_source() != "qwenpaw":
            return
        from iac_code.services.qwenpaw_source import QwenPawError, load_from_qwenpaw

        try:
            config = load_from_qwenpaw()
        except QwenPawError as exc:
            raise ProviderConfigurationError(str(exc)) from exc
        if config is None:
            return
        if config.model != self._model or config.provider_key != self._provider_key_override:
            creds = {config.provider_key: config.api_key or ""} if config.provider_key else {}
            self.reconfigure(config.model, creds, config.provider_key, config.base_url)

    def _ensure_provider(self) -> Provider:
        if self._provider is None:
            self._provider = create_provider(
                self._model,
                self._credentials,
                **self._provider_create_kwargs(),
            )
        return self._provider

    def _provider_create_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "base_url": self._base_url_override,
            "provider_key_override": self._provider_key_override,
        }
        if self._request_policy_override is not None:
            kwargs["request_policy_override"] = self._request_policy_override
        return kwargs

    def reconfigure(
        self,
        model: str,
        credentials: dict[str, str],
        provider_key_override: str | None = None,
        base_url_override: str | None = None,
        request_policy_override: ProviderRequestPolicy | None = None,
    ) -> None:
        """Switch model and credentials in place.

        Used by `/auth` and `/model` so every consumer holding this manager
        (REPL, AgentTool, SkillTool) picks up the change without re-wiring.
        The underlying provider is reset and lazily recreated on next use,
        so reconfiguring while no provider is active stays cheap.
        """
        self._model = model
        self._credentials = credentials
        self._provider_key_override = provider_key_override
        self._base_url_override = base_url_override
        self._request_policy_override = _active_request_policy(request_policy_override)
        self._provider = None
        try:
            self._provider = create_provider(
                model,
                credentials,
                **self._provider_create_kwargs(),
            )
        except ValueError as e:
            logger.warning(f"Provider not configured after reconfigure: {e}")

    def get_model_name(self) -> str:
        return self._model

    def get_provider_key(self) -> str:
        """Return the runtime provider key without forcing provider creation."""
        if self._provider_key_override:
            return self._provider_key_override
        if self._provider is not None:
            key = getattr(self._provider, "_PROVIDER_KEY", "")
            if isinstance(key, str) and key:
                return key
        try:
            return _detect_provider_name(self._model)
        except ValueError:
            return ""

    def get_provider_display(self) -> str:
        key = self.get_provider_key()
        if not key:
            return ""
        from iac_code.providers.registry import PROVIDER_REGISTRY

        descriptor = PROVIDER_REGISTRY.get(key)
        return descriptor.display_name if descriptor is not None else key

    def _get_fallback_model(self) -> str | None:
        return MODEL_FALLBACK_MAP.get(self._model)

    async def stream(
        self, messages: list[Message], system: str, tools: list[ToolDefinition] | None = None, max_tokens: int = 8192
    ) -> AsyncGenerator[StreamEvent, None]:
        try:
            self._check_qwenpaw_config_change()
        except ProviderConfigurationError as exc:
            yield _error_event_from_exception(exc)
            return
        provider = self._ensure_provider()
        provider_name = type(provider).__name__.replace("Provider", "").lower()
        sanitized_model = sanitize_model_name(self._model)

        log_event(
            Events.API_REQUEST_STARTED,
            {
                "provider": provider_name,
                "model": sanitized_model,
                "message_count": len(messages),
            },
        )
        started = time.monotonic()

        span_name = f"{Spans.LLM_CHAT} {self._model}"
        span_attrs = {
            GenAiAttr.SPAN_KIND: GenAiSpanKind.LLM,
            GenAiAttr.OPERATION_NAME: GenAiOperationName.CHAT,
            GenAiAttr.PROVIDER_NAME: provider_name,
            GenAiAttr.REQUEST_MODEL: self._model,
            GenAiAttr.REQUEST_MAX_TOKENS: max_tokens,
            GenAiAttr.CONVERSATION_ID: get_session_id(),
            GenAiAttr.OUTPUT_TYPE: "text",
        }
        if should_capture_content_on_span():
            span_attrs[GenAiAttr.INPUT_MESSAGES] = serialize_input_messages(messages)
            span_attrs[GenAiAttr.SYSTEM_INSTRUCTIONS] = serialize_system_instructions(system)
            if tools:
                span_attrs[GenAiAttr.TOOL_DEFINITIONS] = serialize_tool_definitions(tools)

        with start_span(span_name, span_attrs) as span:
            orphaned_message_ids: list[str] = []
            streaming_failed = False
            first_token_received = False
            try:
                watchdog = StreamWatchdog(idle_timeout=self._stream_idle_timeout)
                watchdog.start()
                stream_iter = provider.stream(messages, system, tools, max_tokens).__aiter__()
                while True:
                    try:
                        event = await asyncio.wait_for(stream_iter.__anext__(), timeout=self._stream_idle_timeout)
                    except StopAsyncIteration:
                        break
                    watchdog.ping()
                    if isinstance(event, MessageStartEvent):
                        orphaned_message_ids.append(event.message_id)
                        span.set_attribute(GenAiAttr.RESPONSE_ID, event.message_id)
                    elif isinstance(event, TextDeltaEvent) and not first_token_received:
                        first_token_received = True
                        ttft_ns = int((time.monotonic() - started) * 1_000_000_000)
                        span.set_attribute(GenAiAttr.RESPONSE_TIME_TO_FIRST_TOKEN, ttft_ns)
                    yield event
                    if isinstance(event, MessageEndEvent):
                        watchdog.stop()
                        self._set_llm_response_span_attrs(span, event, self._model)
                        self._emit_success_telemetry(provider_name, sanitized_model, started, event.usage)
                        return
                streaming_failed = True
            except asyncio.CancelledError:
                raise
            except Exception as e:
                streaming_failed = True
                logger.warning(f"Streaming failed, falling back to non-streaming: {e}")
            if streaming_failed:
                for msg_id in orphaned_message_ids:
                    yield TombstoneEvent(message_id=msg_id)
                try:
                    completion = await self._complete_with_retry_result(messages, system, tools, max_tokens)
                except Exception as e:
                    self._emit_failure_telemetry(provider_name, sanitized_model, started, e)
                    yield _error_event_from_exception(e)
                    return
                response = completion.response
                response_model = sanitize_model_name(completion.model)
                span.set_attribute(GenAiAttr.RESPONSE_ID, response.message_id)
                self._set_llm_response_span_attrs_from_response(span, response, completion.model)
                self._emit_success_telemetry(completion.provider_name, response_model, started, response.usage)
                yield MessageStartEvent(message_id=response.message_id)
                if response.thinking:
                    yield ThinkingDeltaEvent(text=response.thinking)
                if response.text:
                    yield TextDeltaEvent(text=response.text)
                for tu in response.tool_uses:
                    yield ToolUseStartEvent(tool_use_id=tu["id"], name=tu["name"])
                    yield ToolUseEndEvent(tool_use_id=tu["id"], name=tu["name"], input=tu["input"])
                yield MessageEndEvent(stop_reason=response.stop_reason, usage=response.usage)

    @staticmethod
    def _set_llm_response_span_attrs(span, end_event: MessageEndEvent, model: str) -> None:
        usage = end_event.usage
        span.set_attribute(GenAiAttr.RESPONSE_MODEL, model)
        span.set_attribute(GenAiAttr.RESPONSE_FINISH_REASONS, [end_event.stop_reason])
        span.set_attribute(GenAiAttr.USAGE_INPUT_TOKENS, usage.input_tokens)
        span.set_attribute(GenAiAttr.USAGE_OUTPUT_TOKENS, usage.output_tokens)
        total = usage.input_tokens + usage.output_tokens
        span.set_attribute(GenAiAttr.USAGE_TOTAL_TOKENS, total)
        if usage.cache_creation_input_tokens:
            span.set_attribute(GenAiAttr.USAGE_CACHE_CREATION_INPUT_TOKENS, usage.cache_creation_input_tokens)
        if usage.cache_read_input_tokens:
            span.set_attribute(GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS, usage.cache_read_input_tokens)

    @staticmethod
    def _set_llm_response_span_attrs_from_response(span, response: NonStreamingResponse, model: str) -> None:
        usage = response.usage
        span.set_attribute(GenAiAttr.RESPONSE_MODEL, model)
        span.set_attribute(GenAiAttr.RESPONSE_FINISH_REASONS, [response.stop_reason])
        span.set_attribute(GenAiAttr.USAGE_INPUT_TOKENS, usage.input_tokens)
        span.set_attribute(GenAiAttr.USAGE_OUTPUT_TOKENS, usage.output_tokens)
        total = usage.input_tokens + usage.output_tokens
        span.set_attribute(GenAiAttr.USAGE_TOTAL_TOKENS, total)
        if usage.cache_creation_input_tokens:
            span.set_attribute(GenAiAttr.USAGE_CACHE_CREATION_INPUT_TOKENS, usage.cache_creation_input_tokens)
        if usage.cache_read_input_tokens:
            span.set_attribute(GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS, usage.cache_read_input_tokens)

    @staticmethod
    def _emit_success_telemetry(provider_name: str, model: str, started: float, usage) -> None:
        duration_ms = int((time.monotonic() - started) * 1000)
        log_event(
            Events.API_REQUEST_SUCCEEDED,
            {
                "provider": provider_name,
                "model": model,
                "duration_ms": duration_ms,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_input_tokens,
                "cache_create_tokens": usage.cache_creation_input_tokens,
            },
        )
        add_metric(Metrics.API_REQUEST_COUNT, 1, {"provider": provider_name, "model": model, "status": "ok"})
        add_metric(Metrics.API_REQUEST_DURATION, duration_ms, {"provider": provider_name, "model": model})
        for token_type, count in (
            ("input", usage.input_tokens),
            ("output", usage.output_tokens),
            ("cache_read", usage.cache_read_input_tokens or 0),
            ("cache_create", usage.cache_creation_input_tokens or 0),
        ):
            if count:
                add_metric(Metrics.TOKEN_USAGE, count, {"type": token_type, "provider": provider_name, "model": model})

    @staticmethod
    def _emit_failure_telemetry(provider_name: str, model: str, started: float, exc: Exception) -> None:
        duration_ms = int((time.monotonic() - started) * 1000)
        summary = public_exception_summary(exc, max_chars=1000)
        failure = public_error(message=summary, error_type=type(exc).__name__)
        log_event(
            Events.API_REQUEST_FAILED,
            {
                "provider": provider_name,
                "model": model,
                "error_type": type(exc).__name__,
                "duration_ms": duration_ms,
                "error_message": sanitize_error_message(failure.summary),
                "error_id": failure.error_id,
            },
        )
        add_metric(
            Metrics.API_REQUEST_COUNT,
            1,
            {"provider": provider_name, "model": model, "status": "error", "error_type": type(exc).__name__},
        )

    async def complete(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 8192,
        cache_policy: str = "default",
    ) -> NonStreamingResponse:
        return await self._complete_with_retry(
            messages,
            system,
            tools,
            max_tokens,
            is_fallback=False,
            cache_policy=cache_policy,
        )

    async def _complete_with_retry(
        self,
        messages,
        system,
        tools,
        max_tokens,
        is_fallback=False,
        provider_override: Provider | None = None,
        model_override: str | None = None,
        cache_policy: str = "default",
    ) -> NonStreamingResponse:
        result = await self._complete_with_retry_result(
            messages,
            system,
            tools,
            max_tokens,
            is_fallback=is_fallback,
            provider_override=provider_override,
            model_override=model_override,
            cache_policy=cache_policy,
        )
        return result.response

    async def _complete_with_retry_result(
        self,
        messages,
        system,
        tools,
        max_tokens,
        is_fallback=False,
        provider_override: Provider | None = None,
        model_override: str | None = None,
        cache_policy: str = "default",
    ) -> _CompletionResult:
        provider = provider_override or self._ensure_provider()
        model = model_override or self._model
        provider_name = type(provider).__name__.replace("Provider", "").lower()
        sanitized_model = sanitize_model_name(model)

        async def _on_retry(attempt, exc, delay):
            log_event(
                Events.API_REQUEST_RETRIED,
                {
                    "provider": provider_name,
                    "model": sanitized_model,
                    "attempt": attempt,
                    "error_type": type(exc).__name__,
                },
            )

        async def operation():
            try:
                kwargs = {"cache_policy": cache_policy} if cache_policy != "default" else {}
                response = await provider.complete(messages, system, tools, max_tokens, **kwargs)
                return _CompletionResult(response=response, model=model, provider_name=provider_name)
            except Exception as e:
                status = getattr(e, "status_code", None) or getattr(e, "status", None)
                if status and status in {408, 409, 429, 500, 502, 503, 529}:
                    raise RetryableError(f"{type(e).__name__}: {e}", status_code=status) from e
                if isinstance(e, (ConnectionError, TimeoutError, OSError)):
                    raise RetryableError(f"{type(e).__name__}: {e}") from e
                raise

        try:
            return await with_retry(operation, self._retry_config, on_retry=_on_retry)
        except Exception as original_exc:
            if not is_fallback:
                fallback = self._get_fallback_model()
                if fallback is not None:
                    log_event(
                        Events.MODEL_FALLBACK_TRIGGERED,
                        {
                            "from_model": sanitized_model,
                            "to_model": sanitize_model_name(fallback),
                            "reason": "model_degradation",
                        },
                    )
                    try:
                        fallback_kwargs: dict[str, Any] = {
                            "base_url": self._base_url_override,
                            "provider_key_override": self._provider_key_override,
                        }
                        if self._request_policy_override is not None:
                            fallback_kwargs["request_policy_override"] = self._request_policy_override
                        fallback_provider = create_provider(
                            fallback,
                            self._credentials,
                            **fallback_kwargs,
                        )
                        return await self._complete_with_retry_result(
                            messages,
                            system,
                            tools,
                            max_tokens,
                            is_fallback=True,
                            provider_override=fallback_provider,
                            model_override=fallback,
                            cache_policy=cache_policy,
                        )
                    except Exception:
                        raise original_exc from None
            raise
