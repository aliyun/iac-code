"""Provider selection, streaming fallback with tombstone, and model degradation."""

from __future__ import annotations

import asyncio
import copy
import sys
import time
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from anthropic import APIConnectionError as AnthropicAPIConnectionError
from loguru import logger
from openai import APIConnectionError as OpenAIAPIConnectionError

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
    IacCodeAttr,
    Metrics,
    PipelineAttr,
    Spans,
)
from iac_code.services.telemetry.sanitize import sanitize_error_message, sanitize_model_name
from iac_code.services.telemetry.scope import get_span_attributes
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
    Usage,
)
from iac_code.utils.public_errors import public_error, public_exception_summary


class ProviderNotConfiguredError(ValueError):
    """Raised when the LLM provider cannot be determined or has no API key."""


class ProviderConfigurationError(RuntimeError):
    """Raised when provider configuration cannot be loaded during a request."""


class _ModelRefusalError(RuntimeError):
    """Raised when a model returns a successful response that is unusable due to refusal."""

    def __init__(self, model: str):
        super().__init__(f"Model '{model}' refused the request")
        self.model = model


_RETRYABLE_TRANSPORT_ERRORS = (
    ConnectionError,
    TimeoutError,
    OSError,
    OpenAIAPIConnectionError,
    AnthropicAPIConnectionError,
)

_TOKEN_METRIC_SCOPE_KEYS = frozenset(
    {
        IacCodeAttr.MODE,
        PipelineAttr.NAME,
        PipelineAttr.STEP_ID,
        PipelineAttr.PARENT_STEP_ID,
        PipelineAttr.SUB_PIPELINE_NAME,
        PipelineAttr.SUB_STEP_ID,
        PipelineAttr.CANDIDATE_INDEX,
    }
)


class _BestEffortSpan:
    def __init__(self, span: Any | None = None) -> None:
        self._span = span

    def set_attribute(self, key: str, value: Any) -> None:
        if self._span is None:
            return
        try:
            self._span.set_attribute(key, value)
        except Exception:
            logger.opt(exception=True).warning("Provider telemetry span attribute failed: key={}", key)


def _safe_log_event(event_name: str, attrs: dict[str, Any]) -> None:
    try:
        log_event(event_name, attrs)
    except Exception:
        logger.opt(exception=True).warning("Provider telemetry event failed: event={}", event_name)


def _safe_add_metric(name: str, value: int | float, attrs: dict[str, Any]) -> None:
    try:
        add_metric(name, value, attrs)
    except Exception:
        logger.opt(exception=True).warning("Provider telemetry metric failed: metric={}", name)


@contextmanager
def _safe_start_span(name: str, attrs: dict[str, Any]) -> Iterator[_BestEffortSpan]:
    try:
        span_context = start_span(name, attrs)
        span = span_context.__enter__()
    except Exception:
        logger.opt(exception=True).warning("Provider telemetry span failed to start: span={}", name)
        yield _BestEffortSpan()
        return

    try:
        yield _BestEffortSpan(span)
    except BaseException:
        exc_info = sys.exc_info()
        try:
            span_context.__exit__(*exc_info)
        except Exception:
            logger.opt(exception=True).warning("Provider telemetry span failed to close: span={}", name)
        raise
    else:
        try:
            span_context.__exit__(None, None, None)
        except Exception:
            logger.opt(exception=True).warning("Provider telemetry span failed to close: span={}", name)


def _safe_session_id() -> str:
    try:
        return get_session_id()
    except Exception:
        logger.opt(exception=True).warning("Provider telemetry session id lookup failed")
        return ""


def _safe_span_scope_attributes() -> dict[str, str | int]:
    try:
        return get_span_attributes()
    except Exception:
        logger.opt(exception=True).warning("Provider telemetry scope lookup failed")
        return {}


def _capture_request_content(
    attrs: dict[str, Any],
    messages: list[Message],
    system: str,
    tools: list[ToolDefinition] | None,
) -> None:
    try:
        if not should_capture_content_on_span():
            return
        attrs[GenAiAttr.INPUT_MESSAGES] = serialize_input_messages(messages)
        attrs[GenAiAttr.SYSTEM_INSTRUCTIONS] = serialize_system_instructions(system)
        if tools:
            attrs[GenAiAttr.TOOL_DEFINITIONS] = serialize_tool_definitions(tools)
    except Exception:
        logger.opt(exception=True).warning("Provider telemetry request content capture failed")


@dataclass(frozen=True)
class _CompletionResult:
    response: NonStreamingResponse
    model: str
    provider_name: str
    provider: Provider


def _error_event_from_exception(exc: BaseException) -> ErrorEvent:
    summary = public_exception_summary(exc, max_chars=1000)
    failure = public_error(message=summary, error_type=type(exc).__name__)
    return ErrorEvent(error=summary, is_retryable=False, error_id=failure.error_id)


MODEL_FALLBACK_MAP = {
    "claude-fable-5": "claude-opus-4-8",
    "claude-opus-4-8": "claude-sonnet-5",
    "claude-opus-4-7": "claude-haiku-4-5-20251001",
    "claude-opus-4-6": "claude-haiku-4-5-20251001",
    "claude-sonnet-5": "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6": "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6-1m": "claude-haiku-4-5-20251001",
    "gpt-5.6-sol": "gpt-5.6-terra",
    "gpt-5.6": "gpt-5.6-terra",
    "gpt-5.6-terra": "gpt-5.6-luna",
    "gpt-5.5": "gpt-5.4",
    "gpt-5.4": "gpt-5.4-mini",
    "qwen3.8-max-preview": "qwen3.7-plus",
    "qwen3.7-max": "qwen3.7-plus",
    "kimi/kimi-k3": "kimi-k2.7-code",
    "kimi-k3": "kimi-k2.7-code",
    "glm-5.2": "glm-5.1",
    "deepseek-v4-pro": "deepseek-v4-flash",
}

_MODEL_REFUSAL_FALLBACK_MAP = {
    "claude-fable-5": "claude-opus-4-8",
}

_PROVIDER_MODEL_FALLBACK_MAP = {
    "dashscope": {"qwen3.6-plus": "qwen3.6-flash"},
    "dashscope_token_plan": {"qwen3.6-plus": "qwen3.6-flash"},
    "aliyun_codingplan": {"qwen3.6-plus": "qwen3.5-plus"},
    "aliyun_codingplan_intl": {"qwen3.6-plus": "qwen3.5-plus"},
}

_LEGACY_DISABLE_EFFORTS = {"none", "off", "disable", "disabled", "false", "0"}


def _legacy_effort_disables_thinking(effort: str | None) -> bool:
    return isinstance(effort, str) and effort.strip().lower() in _LEGACY_DISABLE_EFFORTS


def _normalize_configured_effort(
    effort: str | None,
    thinking_enabled: bool | None,
    *,
    provider_key: str,
    model: str,
) -> tuple[str | None, bool | None]:
    """Interpret legacy disable aliases without consuming a supported ``none`` effort."""
    if not _legacy_effort_disables_thinking(effort):
        return effort, thinking_enabled
    assert isinstance(effort, str)
    normalized_effort = effort.strip().lower()
    if normalized_effort == "none":
        from iac_code.providers.thinking import get_thinking_spec

        spec = get_thinking_spec(provider_key, model)
        if any(item.value == normalized_effort for item in spec.allowed_efforts):
            return normalized_effort, thinking_enabled
    return None, False


def _detect_provider_name(model: str) -> str:
    """Detect provider from saved settings, falling back to model-name heuristics.

    Priority:
    1. Saved config in settings.yml (set by /auth or /model).
    2. Model-name prefix matching for mainstream models.
    """
    from iac_code.config import _KEY_NAME_TO_CRED_SLOT, _infer_provider_key_from_model, get_active_provider_key

    key_name = get_active_provider_key() or ""
    if key_name in _KEY_NAME_TO_CRED_SLOT:
        return _KEY_NAME_TO_CRED_SLOT[key_name]

    inferred_provider = _infer_provider_key_from_model(model)
    if inferred_provider is not None:
        return inferred_provider

    raise ProviderNotConfiguredError(
        _("Cannot determine provider for model: {model}. Run /auth to configure.").format(model=model)
    )


def create_provider(
    model: str,
    credentials: dict[str, str],
    *,
    base_url: str | None = None,
    provider_key_override: str | None = None,
    provider_config_override: dict[str, Any] | None = None,
    request_policy_override: ProviderRequestPolicy | None = None,
    effort_override: str | None = None,
) -> Provider:
    from iac_code.providers.registry import PROVIDER_REGISTRY

    request_policy_override = _request_policy_with_effort_override(request_policy_override, effort_override)
    provider_key = provider_key_override or _detect_provider_name(model)
    desc = PROVIDER_REGISTRY.get(provider_key)
    if desc is None:
        raise ProviderNotConfiguredError(
            _("Unknown provider key: '{key}'. Run /auth to configure.").format(key=provider_key)
        )
    if provider_config_override is None:
        from iac_code.config import get_provider_config

        provider_cfg = get_provider_config(provider_key)
    else:
        provider_cfg = copy.deepcopy(provider_config_override)
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
    if _legacy_effort_disables_thinking(effort_override):
        effort = None
        thinking_enabled = False
    else:
        effort, thinking_enabled = _normalize_configured_effort(
            effort,
            thinking_enabled,
            provider_key=wire_provider_key,
            model=model,
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

        if thinking_budget is not None and issubclass(provider_cls, AnthropicProvider):
            request_policy_kwargs["thinking_budget"] = thinking_budget
        if max_completion_tokens is not None and issubclass(provider_cls, AnthropicProvider):
            request_policy_kwargs["max_completion_tokens"] = max_completion_tokens
    provider = provider_cls(
        model=model,
        api_key=api_key or None,
        base_url=effective_base_url,
        effort=effort,
        provider_key=wire_provider_key,
        **request_policy_kwargs,
    )
    setattr(provider, "_logical_provider_key", provider_key)
    return provider


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


def _request_policy_with_effort_override(
    policy: ProviderRequestPolicy | None,
    effort_override: str | None,
) -> ProviderRequestPolicy | None:
    policy = _active_request_policy(policy)
    if effort_override is None:
        return policy
    effort = effort_override.strip()
    if not effort:
        return policy
    thinking_enabled = policy.thinking_enabled if policy is not None else None
    effective_effort: str | None = effort
    if _legacy_effort_disables_thinking(effort):
        thinking_enabled = False
        effective_effort = None
    return _active_request_policy(
        ProviderRequestPolicy(
            thinking_enabled=thinking_enabled,
            effort=effective_effort,
            thinking_budget=policy.thinking_budget if policy is not None else None,
            max_completion_tokens=policy.max_completion_tokens if policy is not None else None,
        )
    )


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
        effort_override: str | None = None,
        ignore_llm_source: bool = False,
        provider_config_override: dict[str, Any] | None = None,
    ):
        self._model = model
        self._credentials = credentials
        self._retry_config = retry_config or RetryConfig()
        self._stream_idle_timeout = stream_idle_timeout
        self._provider_key_override = provider_key_override
        self._base_url_override = base_url_override
        self._effort_override = effort_override
        self._provider_config_override = copy.deepcopy(provider_config_override)
        self._request_policy_override = _request_policy_with_effort_override(request_policy_override, effort_override)
        # 会话级显式 provider 覆盖时,忽略全局合作方源 llm_source(当前唯一实现为 QwenPaw)的每轮热切换:
        # 否则每次请求开头的 _check_qwenpaw_config_change 都会因全局 llm_source 仍指向合作方而把本会话
        # 选定的 provider/模型/base_url 改回(失效的)合作方端点,导致换 provider 后仍报同样的错。
        self._ignore_llm_source = ignore_llm_source
        # Lazy: first startup may have no active provider yet. Defer errors
        # until the user actually tries to send a message, so /auth is reachable.
        self._provider: Provider | None = None
        self._pinned_provider: Provider | None = None
        self._pinned_model: str | None = None
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
        if self._ignore_llm_source:
            # 本会话已用会话级 provider 覆盖,忽略全局 llm_source 合作方源的热切换(见构造函数注释)。
            return
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

    def _active_provider_and_model(self) -> tuple[Provider, str]:
        if self._pinned_provider is not None and self._pinned_model is not None:
            return self._pinned_provider, self._pinned_model
        return self._ensure_provider(), self._model

    def _provider_create_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "base_url": self._base_url_override,
            "provider_key_override": self._provider_key_override,
        }
        if self._provider_config_override is not None:
            kwargs["provider_config_override"] = self._provider_config_override
        if self._request_policy_override is not None:
            kwargs["request_policy_override"] = self._request_policy_override
        if self._effort_override is not None:
            kwargs["effort_override"] = self._effort_override
        return kwargs

    def reconfigure(
        self,
        model: str,
        credentials: dict[str, str],
        provider_key_override: str | None = None,
        base_url_override: str | None = None,
        request_policy_override: ProviderRequestPolicy | None = None,
        effort_override: str | None = None,
        provider_config_override: dict[str, Any] | None = None,
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
        self._effort_override = effort_override
        self._provider_config_override = copy.deepcopy(provider_config_override)
        self._request_policy_override = _request_policy_with_effort_override(request_policy_override, effort_override)
        self.reset_conversation_state()
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
        return self._pinned_model or self._model

    def reset_conversation_state(self) -> None:
        """Clear conversation-scoped model selection such as refusal fallback pinning."""
        self._pinned_provider = None
        self._pinned_model = None

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

    def _get_fallback_model(self, model: str | None = None, provider_key: str | None = None) -> str | None:
        current_model = model or self._model
        resolved_provider_key = provider_key or self.get_provider_key()
        provider_fallbacks = _PROVIDER_MODEL_FALLBACK_MAP.get(resolved_provider_key, {})
        provider_fallback = provider_fallbacks.get(current_model)
        if provider_fallback is not None:
            return provider_fallback

        fallback = MODEL_FALLBACK_MAP.get(current_model)
        if fallback is None:
            return None
        from iac_code.providers.registry import PROVIDER_REGISTRY

        descriptor = PROVIDER_REGISTRY.get(resolved_provider_key)
        if descriptor is None or not descriptor.models:
            return None
        model_ids = {entry.id for entry in descriptor.models}
        return fallback if current_model in model_ids and fallback in model_ids else None

    def _get_refusal_fallback_model(self, model: str, provider_key: str) -> str | None:
        fallback = _MODEL_REFUSAL_FALLBACK_MAP.get(model)
        if fallback is None:
            return None
        from iac_code.providers.registry import PROVIDER_REGISTRY

        descriptor = PROVIDER_REGISTRY.get(provider_key)
        if descriptor is None or not descriptor.models:
            return None
        model_ids = {entry.id for entry in descriptor.models}
        return fallback if model in model_ids and fallback in model_ids else None

    async def stream(
        self, messages: list[Message], system: str, tools: list[ToolDefinition] | None = None, max_tokens: int = 8192
    ) -> AsyncGenerator[StreamEvent, None]:
        try:
            self._check_qwenpaw_config_change()
        except ProviderConfigurationError as exc:
            yield _error_event_from_exception(exc)
            return
        provider, model = self._active_provider_and_model()
        provider_name = type(provider).__name__.replace("Provider", "").lower()
        sanitized_model = sanitize_model_name(model)

        _safe_log_event(
            Events.API_REQUEST_STARTED,
            {
                "provider": provider_name,
                "model": sanitized_model,
                "message_count": len(messages),
            },
        )
        started = time.monotonic()

        span_name = f"{Spans.LLM_CHAT} {model}"
        session_id = _safe_session_id()
        span_attrs = {
            GenAiAttr.SPAN_KIND: GenAiSpanKind.LLM,
            GenAiAttr.OPERATION_NAME: GenAiOperationName.CHAT,
            GenAiAttr.PROVIDER_NAME: provider_name,
            GenAiAttr.REQUEST_MODEL: model,
            GenAiAttr.REQUEST_MAX_TOKENS: max_tokens,
            GenAiAttr.SESSION_ID: session_id,
            GenAiAttr.CONVERSATION_ID: session_id,
            GenAiAttr.OUTPUT_TYPE: "text",
        }
        span_attrs.update(_safe_span_scope_attributes())
        _capture_request_content(span_attrs, messages, system, tools)

        orphaned_message_ids: list[str] = []
        orphaned_tool_use_ids: dict[str, list[str]] = {}
        current_message_id: str | None = None
        buffer_until_accepted = model in _MODEL_REFUSAL_FALLBACK_MAP
        buffered_events: list[StreamEvent] = []
        streaming_failed = False
        refusal_detected = False
        first_token_received = False
        watchdog: StreamWatchdog | None = None
        with _safe_start_span(span_name, span_attrs) as span:
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
                        current_message_id = event.message_id
                        orphaned_tool_use_ids.setdefault(event.message_id, [])
                        span.set_attribute(GenAiAttr.RESPONSE_ID, event.message_id)
                    elif isinstance(event, (ToolUseStartEvent, ToolUseEndEvent)) and current_message_id is not None:
                        tool_ids = orphaned_tool_use_ids.setdefault(current_message_id, [])
                        if event.tool_use_id not in tool_ids:
                            tool_ids.append(event.tool_use_id)
                    elif (
                        isinstance(event, (TextDeltaEvent, ThinkingDeltaEvent))
                        and event.text
                        and not first_token_received
                    ):
                        first_token_received = True
                        ttft_ns = int((time.monotonic() - started) * 1_000_000_000)
                        span.set_attribute(GenAiAttr.RESPONSE_TIME_TO_FIRST_TOKEN, ttft_ns)
                        _safe_log_event(
                            Events.API_RESPONSE_FIRST_TOKEN,
                            {
                                "provider": provider_name,
                                "model": sanitized_model,
                                GenAiAttr.RESPONSE_TIME_TO_FIRST_TOKEN: ttft_ns,
                                "first_token_source": event.type,
                                **_safe_span_scope_attributes(),
                            },
                        )
                    if isinstance(event, MessageEndEvent):
                        watchdog.stop()
                        if event.stop_reason == "refusal":
                            self._set_llm_response_span_attrs(span, event, model)
                            self._emit_success_telemetry(
                                provider_name,
                                sanitized_model,
                                started,
                                event.usage,
                                status="refusal",
                            )
                            refusal_detected = True
                            streaming_failed = True
                            logger.warning("Streaming response was refused, falling back to an approved model")
                            break
                        if buffer_until_accepted:
                            for buffered_event in buffered_events:
                                yield buffered_event
                        yield event
                        self._set_llm_response_span_attrs(span, event, model)
                        self._emit_success_telemetry(provider_name, sanitized_model, started, event.usage)
                        return
                    if buffer_until_accepted:
                        buffered_events.append(event)
                    else:
                        yield event
                watchdog.stop()
                if not refusal_detected:
                    streaming_failed = True
                    self._emit_failure_telemetry(
                        provider_name,
                        sanitized_model,
                        started,
                        RuntimeError("Streaming response ended before message completion"),
                    )
            except asyncio.CancelledError as exc:
                if watchdog is not None:
                    watchdog.stop()
                self._emit_failure_telemetry(
                    provider_name,
                    sanitized_model,
                    started,
                    exc,
                    status="cancelled",
                    record_duration=True,
                )
                raise
            except Exception as e:
                if watchdog is not None:
                    watchdog.stop()
                streaming_failed = True
                self._emit_failure_telemetry(provider_name, sanitized_model, started, e)
                logger.warning(f"Streaming failed, falling back to non-streaming: {e}")

        if streaming_failed:
            if not buffer_until_accepted:
                for msg_id in orphaned_message_ids:
                    yield TombstoneEvent(
                        message_id=msg_id,
                        affected_tool_use_ids=orphaned_tool_use_ids.get(msg_id, []),
                    )
            try:
                completion = await self._complete_with_retry_result(
                    messages,
                    system,
                    tools,
                    max_tokens,
                    refusal_detected=refusal_detected,
                )
            except Exception as e:
                yield _error_event_from_exception(e)
                return
            response = completion.response
            yield MessageStartEvent(message_id=response.message_id)
            if response.thinking_blocks:
                for block_index, block in enumerate(response.thinking_blocks):
                    yield ThinkingDeltaEvent(
                        text=str(block.get("text") or ""),
                        block_index=block_index,
                        block_type=block.get("type", "thinking"),
                        provider_metadata=(
                            dict(block["provider_metadata"])
                            if isinstance(block.get("provider_metadata"), dict)
                            else None
                        ),
                    )
            elif response.thinking:
                yield ThinkingDeltaEvent(text=response.thinking)
            if response.text:
                yield TextDeltaEvent(text=response.text)
            for tu in response.tool_uses:
                provider_metadata = tu.get("provider_metadata")
                yield ToolUseStartEvent(
                    tool_use_id=tu["id"],
                    name=tu["name"],
                    provider_metadata=provider_metadata,
                )
                yield ToolUseEndEvent(
                    tool_use_id=tu["id"],
                    name=tu["name"],
                    input=tu["input"],
                    provider_metadata=provider_metadata,
                )
            yield MessageEndEvent(stop_reason=response.stop_reason, usage=response.usage)

    @staticmethod
    def _set_llm_response_span_attrs(span, end_event: MessageEndEvent, model: str) -> None:
        usage = end_event.usage
        span.set_attribute(GenAiAttr.RESPONSE_MODEL, model)
        span.set_attribute(GenAiAttr.RESPONSE_FINISH_REASONS, [end_event.stop_reason])
        ProviderManager._set_usage_span_attrs(span, usage)

    @staticmethod
    def _set_llm_response_span_attrs_from_response(span, response: NonStreamingResponse, model: str) -> None:
        usage = response.usage
        span.set_attribute(GenAiAttr.RESPONSE_MODEL, model)
        span.set_attribute(GenAiAttr.RESPONSE_FINISH_REASONS, [response.stop_reason])
        ProviderManager._set_usage_span_attrs(span, usage)

    @staticmethod
    def _set_usage_span_attrs(span: Any, usage: Usage) -> None:
        span.set_attribute(GenAiAttr.USAGE_REPORTED, usage.usage_reported)
        if not usage.usage_reported:
            return
        span.set_attribute(GenAiAttr.USAGE_INPUT_TOKENS, usage.input_tokens)
        span.set_attribute(GenAiAttr.USAGE_TOTAL_INPUT_TOKENS, usage.total_input_tokens)
        span.set_attribute(GenAiAttr.USAGE_STANDARD_INPUT_TOKENS, usage.standard_input_tokens)
        span.set_attribute(GenAiAttr.USAGE_OUTPUT_TOKENS, usage.output_tokens)
        span.set_attribute(GenAiAttr.USAGE_TOTAL_TOKENS, usage.normalized_total_tokens)
        span.set_attribute(GenAiAttr.USAGE_CACHE_CREATION_INPUT_TOKENS, usage.cache_creation_input_tokens)
        span.set_attribute(GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS, usage.cache_read_input_tokens)
        span.set_attribute(GenAiAttr.USAGE_CACHE_HIT_RATE, usage.cache_hit_rate)

    @staticmethod
    def _emit_success_telemetry(
        provider_name: str,
        model: str,
        started: float,
        usage: Usage,
        *,
        status: str = "ok",
    ) -> None:
        duration_ms = int((time.monotonic() - started) * 1000)
        scope_attrs = _safe_span_scope_attributes()
        usage_attrs: dict[str, Any] = {"usage_reported": usage.usage_reported}
        if usage.usage_reported:
            usage_attrs.update(
                {
                    "input_tokens": usage.input_tokens,
                    "total_input_tokens": usage.total_input_tokens,
                    "standard_input_tokens": usage.standard_input_tokens,
                    "output_tokens": usage.output_tokens,
                    "total_tokens": usage.normalized_total_tokens,
                    "cache_read_tokens": usage.cache_read_input_tokens,
                    "cache_create_tokens": usage.cache_creation_input_tokens,
                    "cache_hit_rate": usage.cache_hit_rate,
                }
            )
        _safe_log_event(
            Events.API_REQUEST_SUCCEEDED,
            {
                "provider": provider_name,
                "model": model,
                "status": status,
                "duration_ms": duration_ms,
                **usage_attrs,
                **scope_attrs,
            },
        )
        request_metric_attrs = {"provider": provider_name, "model": model}
        _safe_add_metric(
            Metrics.API_REQUEST_COUNT,
            1,
            {**request_metric_attrs, "status": status},
        )
        _safe_add_metric(Metrics.API_REQUEST_DURATION, duration_ms, request_metric_attrs)
        token_metric_attrs = {
            **request_metric_attrs,
            **{key: value for key, value in scope_attrs.items() if key in _TOKEN_METRIC_SCOPE_KEYS},
        }
        _safe_add_metric(
            Metrics.TOKEN_USAGE_REPORT_COUNT,
            1,
            {**token_metric_attrs, "reported": usage.usage_reported},
        )
        if not usage.usage_reported:
            return
        if usage.normalized_total_tokens:
            _safe_add_metric(Metrics.TOKEN_TOTAL, usage.normalized_total_tokens, token_metric_attrs)
        for token_type, count in (
            ("input", usage.input_tokens),
            ("output", usage.output_tokens),
            ("cache_read", usage.cache_read_input_tokens or 0),
            ("cache_create", usage.cache_creation_input_tokens or 0),
        ):
            if count:
                _safe_add_metric(Metrics.TOKEN_USAGE, count, {**token_metric_attrs, "type": token_type})

    @staticmethod
    def _emit_failure_telemetry(
        provider_name: str,
        model: str,
        started: float,
        exc: BaseException,
        *,
        status: str = "error",
        record_duration: bool = False,
    ) -> None:
        duration_ms = int((time.monotonic() - started) * 1000)
        summary = public_exception_summary(exc, max_chars=1000)
        failure = public_error(message=summary, error_type=type(exc).__name__)
        event_attrs: dict[str, Any] = {
            "provider": provider_name,
            "model": model,
            "error_type": type(exc).__name__,
            "duration_ms": duration_ms,
            "error_message": sanitize_error_message(failure.summary),
            "error_id": failure.error_id,
        }
        if status != "error":
            event_attrs["status"] = status
        _safe_log_event(
            Events.API_REQUEST_FAILED,
            event_attrs,
        )
        request_metric_attrs = {"provider": provider_name, "model": model}
        _safe_add_metric(
            Metrics.API_REQUEST_COUNT,
            1,
            {**request_metric_attrs, "status": status, "error_type": type(exc).__name__},
        )
        if record_duration:
            _safe_add_metric(Metrics.API_REQUEST_DURATION, duration_ms, request_metric_attrs)

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
            cache_policy=cache_policy,
        )

    async def _complete_with_retry(
        self,
        messages,
        system,
        tools,
        max_tokens,
        provider_override: Provider | None = None,
        model_override: str | None = None,
        cache_policy: str = "default",
    ) -> NonStreamingResponse:
        result = await self._complete_with_retry_result(
            messages,
            system,
            tools,
            max_tokens,
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
        provider_override: Provider | None = None,
        model_override: str | None = None,
        cache_policy: str = "default",
        fallback_visited: frozenset[str] | None = None,
        refusal_detected: bool = False,
        allow_model_fallback: bool = True,
    ) -> _CompletionResult:
        if provider_override is None and model_override is None:
            provider, model = self._active_provider_and_model()
        else:
            provider = provider_override or self._ensure_provider()
            model = model_override or self._model
        visited = set(fallback_visited or ())
        visited.add(model)
        provider_name = type(provider).__name__.replace("Provider", "").lower()
        sanitized_model = sanitize_model_name(model)

        async def _on_retry(attempt, exc, delay):
            _safe_log_event(
                Events.API_REQUEST_RETRIED,
                {
                    "provider": provider_name,
                    "model": sanitized_model,
                    "attempt": attempt,
                    "error_type": type(exc).__name__,
                },
            )

        async def operation():
            if refusal_detected:
                raise _ModelRefusalError(model)

            session_id = _safe_session_id()
            span_attrs = {
                GenAiAttr.SPAN_KIND: GenAiSpanKind.LLM,
                GenAiAttr.OPERATION_NAME: GenAiOperationName.CHAT,
                GenAiAttr.PROVIDER_NAME: provider_name,
                GenAiAttr.REQUEST_MODEL: model,
                GenAiAttr.REQUEST_MAX_TOKENS: max_tokens,
                GenAiAttr.SESSION_ID: session_id,
                GenAiAttr.CONVERSATION_ID: session_id,
                GenAiAttr.OUTPUT_TYPE: "text",
                **_safe_span_scope_attributes(),
            }
            _capture_request_content(span_attrs, messages, system, tools)

            try:
                with _safe_start_span(f"{Spans.LLM_CHAT} {model}", span_attrs) as span:
                    try:
                        kwargs = {"cache_policy": cache_policy} if cache_policy != "default" else {}
                        request_started = time.monotonic()
                        _safe_log_event(
                            Events.API_REQUEST_STARTED,
                            {
                                "provider": provider_name,
                                "model": sanitized_model,
                                "message_count": len(messages),
                            },
                        )
                        request_started = time.monotonic()
                        response = await provider.complete(messages, system, tools, max_tokens, **kwargs)
                    except asyncio.CancelledError as exc:
                        self._emit_failure_telemetry(
                            provider_name,
                            sanitized_model,
                            request_started,
                            exc,
                            status="cancelled",
                            record_duration=True,
                        )
                        raise
                    except Exception as exc:
                        self._emit_failure_telemetry(provider_name, sanitized_model, request_started, exc)
                        raise

                    span.set_attribute(GenAiAttr.RESPONSE_ID, response.message_id)
                    self._set_llm_response_span_attrs_from_response(span, response, model)
                    response_status = "refusal" if response.stop_reason == "refusal" else "ok"
                    self._emit_success_telemetry(
                        provider_name,
                        sanitized_model,
                        request_started,
                        response.usage,
                        status=response_status,
                    )

                if response.stop_reason == "refusal":
                    raise _ModelRefusalError(model)
                return _CompletionResult(
                    response=response,
                    model=model,
                    provider_name=provider_name,
                    provider=provider,
                )
            except Exception as e:
                status = getattr(e, "status_code", None) or getattr(e, "status", None)
                retryable_status = status in {408, 409, 429} or (isinstance(status, int) and 500 <= status < 600)
                if retryable_status:
                    raise RetryableError(f"{type(e).__name__}: {e}", status_code=status) from e
                if isinstance(e, _RETRYABLE_TRANSPORT_ERRORS):
                    raise RetryableError(f"{type(e).__name__}: {e}") from e
                raise

        try:
            return await with_retry(operation, self._retry_config, on_retry=_on_retry)
        except Exception as original_exc:
            if not isinstance(original_exc, (RetryableError, _ModelRefusalError)):
                raise
            logical_provider_key = getattr(provider, "_logical_provider_key", None)
            if not isinstance(logical_provider_key, str) or not logical_provider_key:
                logical_provider_key = self._provider_key_override
            wire_provider_key = getattr(provider, "_PROVIDER_KEY", None)
            if not isinstance(wire_provider_key, str) or not wire_provider_key:
                wire_provider_key = None
            if not logical_provider_key or not wire_provider_key:
                try:
                    detected_provider_key = _detect_provider_name(model)
                except ValueError:
                    detected_provider_key = ""
                logical_provider_key = logical_provider_key or detected_provider_key
                wire_provider_key = wire_provider_key or logical_provider_key

            if not allow_model_fallback:
                fallback = None
                fallback_reason = "model_degradation"
            elif isinstance(original_exc, _ModelRefusalError):
                fallback = self._get_refusal_fallback_model(model, wire_provider_key)
                fallback_reason = "model_refusal"
            else:
                fallback = self._get_fallback_model(model, wire_provider_key)
                fallback_reason = "model_degradation"
            if fallback is not None and fallback not in visited:
                _safe_log_event(
                    Events.MODEL_FALLBACK_TRIGGERED,
                    {
                        "from_model": sanitized_model,
                        "to_model": sanitize_model_name(fallback),
                        "reason": fallback_reason,
                    },
                )
                try:
                    fallback_kwargs: dict[str, Any] = {
                        "base_url": self._base_url_override,
                        "provider_key_override": logical_provider_key,
                    }
                    if self._request_policy_override is not None:
                        fallback_kwargs["request_policy_override"] = self._request_policy_override
                    if self._effort_override is not None:
                        fallback_kwargs["effort_override"] = self._effort_override
                    fallback_provider = create_provider(
                        fallback,
                        self._credentials,
                        **fallback_kwargs,
                    )
                    result = await self._complete_with_retry_result(
                        messages,
                        system,
                        tools,
                        max_tokens,
                        provider_override=fallback_provider,
                        model_override=fallback,
                        cache_policy=cache_policy,
                        fallback_visited=frozenset(visited),
                        allow_model_fallback=not isinstance(original_exc, _ModelRefusalError),
                    )
                    if isinstance(original_exc, _ModelRefusalError):
                        self._pinned_provider = result.provider
                        self._pinned_model = result.model
                    return result
                except Exception:
                    raise original_exc from None
            raise
