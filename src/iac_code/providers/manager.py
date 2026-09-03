"""Provider selection, streaming fallback with tombstone, and model degradation."""

from __future__ import annotations

import asyncio
import copy
import sys
import time
import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any

from anthropic import APIConnectionError as AnthropicAPIConnectionError
from loguru import logger
from openai import APIConnectionError as OpenAIAPIConnectionError
from opentelemetry.trace import Status, StatusCode

from iac_code.i18n import _
from iac_code.providers.base import Message, NonStreamingResponse, Provider, ToolDefinition
from iac_code.providers.dashscope_endpoints import (
    DASHSCOPE_WIRE_PROVIDER_KEYS,
    is_bailian_compatible_endpoint,
    official_dashscope_wire_provider_key,
)
from iac_code.providers.model_family import is_qwen_model
from iac_code.providers.request_lease import LeaseToken, ProviderRequestLease
from iac_code.providers.request_policy import ProviderRequestPolicy, bool_or_none, positive_int_or_none
from iac_code.providers.retry import RetryableError, RetryConfig, with_retry
from iac_code.providers.stream_watchdog import StreamWatchdog
from iac_code.providers.streaming import UnsafeStreamProtocolError
from iac_code.services.session_logging import is_custom_endpoint, sanitize_endpoint_origin
from iac_code.services.telemetry import (
    add_metric,
    attach_context,
    detach_context,
    get_current_context,
    get_session_id,
    log_event,
    start_detached_span,
    start_span,
    use_span,
)
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
from iac_code.services.telemetry.scope import get_span_attributes, replace_span_attributes
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
from iac_code.types.usage_attribution import UsageAttribution
from iac_code.utils.public_errors import public_error, public_exception_summary


class ProviderNotConfiguredError(ValueError):
    """Raised when the LLM provider cannot be determined or has no API key."""


class ProviderConfigurationError(RuntimeError):
    """Raised when provider configuration cannot be loaded during a request."""


class ProviderRequestLeaseError(ValueError):
    """A request lease invariant failed with a localizable public message."""

    def __init__(self, message_id: str, **message_args: Any) -> None:
        self.i18n_message_id = message_id
        self.i18n_message_args = message_args or None
        super().__init__(_(message_id).format(**message_args))


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


def _retryable_provider_status(exc: BaseException) -> int | None:
    """Provider HTTP status of *exc* when the same request is worth repeating, else ``None``."""
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status in {408, 409, 429} or (isinstance(status, int) and 500 <= status < 600):
        return status
    return None


def _is_retryable_provider_error(exc: BaseException) -> bool:
    """Whether *exc* is a transient provider failure rather than a rejected request."""
    return _retryable_provider_status(exc) is not None or isinstance(exc, _RETRYABLE_TRANSPORT_ERRORS)


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
        self._exception_recorded = False
        self._error_status_set = False

    @property
    def raw(self) -> Any | None:
        return self._span

    def set_attribute(self, key: str, value: Any) -> None:
        if self._span is None:
            return
        try:
            self._span.set_attribute(key, value)
        except Exception:
            logger.opt(exception=True).warning("Provider telemetry span attribute failed: key={}", key)

    def record_exception_once(self, exc: BaseException) -> None:
        if self._span is None or self._exception_recorded:
            return
        self._exception_recorded = True
        try:
            self._span.record_exception(exc)
        except Exception:
            logger.opt(exception=True).warning("Provider telemetry span exception recording failed")

    def set_error_status_once(self, description: str) -> None:
        if self._span is None or self._error_status_set:
            return
        self._error_status_set = True
        try:
            self._span.set_status(Status(StatusCode.ERROR, description=description))
        except Exception:
            logger.opt(exception=True).warning("Provider telemetry span status update failed")

    def end(self) -> None:
        if self._span is None:
            return
        self._span.end()


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


def _safe_start_detached_span(name: str, attrs: dict[str, Any], parent_context: Any) -> _BestEffortSpan:
    try:
        return _BestEffortSpan(start_detached_span(name, attrs, parent_context=parent_context))
    except Exception:
        logger.opt(exception=True).warning("Provider detached telemetry span failed to start: span={}", name)
        return _BestEffortSpan()


@contextmanager
def _safe_use_span(span: _BestEffortSpan) -> Iterator[None]:
    raw_span = span.raw
    if raw_span is None:
        yield
        return
    try:
        span_context = use_span(
            raw_span,
            record_exception=False,
            set_status_on_exception=False,
            end_on_exit=False,
        )
        span_context.__enter__()
    except Exception:
        logger.opt(exception=True).warning("Provider telemetry span activation failed")
        yield
        return
    try:
        yield
    except BaseException:
        exc_info = sys.exc_info()
        try:
            span_context.__exit__(*exc_info)
        except Exception:
            logger.opt(exception=True).warning("Provider telemetry span deactivation failed")
        raise
    else:
        try:
            span_context.__exit__(None, None, None)
        except Exception:
            logger.opt(exception=True).warning("Provider telemetry span deactivation failed")


def _safe_current_context() -> Any:
    try:
        return get_current_context()
    except Exception:
        logger.opt(exception=True).warning("Provider telemetry context capture failed")
        return None


@contextmanager
def _safe_attach_parent_context(parent_context: Any) -> Iterator[None]:
    if parent_context is None:
        yield
        return
    try:
        token = attach_context(parent_context)
    except Exception:
        logger.opt(exception=True).warning("Provider telemetry parent context attach failed")
        yield
        return
    try:
        yield
    finally:
        try:
            detach_context(token)
        except Exception:
            logger.opt(exception=True).warning("Provider telemetry parent context detach failed")


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
    telemetry_messages: list[Any] | None = None,
) -> None:
    try:
        if not should_capture_content_on_span():
            return
        attrs[GenAiAttr.INPUT_MESSAGES] = serialize_input_messages(telemetry_messages or messages)
        attrs[GenAiAttr.SYSTEM_INSTRUCTIONS] = serialize_system_instructions(system)
        if tools:
            attrs[GenAiAttr.TOOL_DEFINITIONS] = serialize_tool_definitions(tools)
    except Exception:
        logger.opt(exception=True).warning("Provider telemetry request content capture failed")


@dataclass
class _StreamAttemptOutcome:
    """What one streaming attempt left behind, for the retry / downgrade decision.

    ``retryable_stream_error`` is set only when the stream died on a transient
    provider failure — the one shape of failure that repeating the same stream
    can survive.
    """

    streaming_failed: bool = False
    refusal_detected: bool = False
    buffer_until_accepted: bool = False
    retryable_stream_error: BaseException | None = None
    stream_failure_exception: BaseException | None = None
    provider_name: str = ""
    sanitized_model: str = ""
    orphaned_message_ids: list[str] = field(default_factory=list)
    orphaned_tool_use_ids: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class _CompletionResult:
    response: NonStreamingResponse
    model: str
    provider_name: str
    provider: Provider


def _error_event_from_exception(exc: BaseException) -> ErrorEvent:
    summary = public_exception_summary(exc, max_chars=1000)
    failure = public_error(message=summary, error_type=type(exc).__name__)
    message_id = getattr(exc, "i18n_message_id", None)
    message_args = getattr(exc, "i18n_message_args", None)
    return ErrorEvent(
        error=summary,
        is_retryable=False,
        error_id=failure.error_id,
        i18n_message_id=message_id if isinstance(message_id, str) else None,
        i18n_message_args=dict(message_args) if isinstance(message_args, dict) else None,
    )


MODEL_FALLBACK_MAP = {
    "claude-fable-5": "claude-opus-4-8",
    "claude-opus-5": "claude-opus-4-8",
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
    "qwen3.8-max": "qwen3.7-plus",
    "qwen3.8-max-prime": "qwen3.8-max",
    "qwen3.8-2.4t-a95b": "qwen3.8-max",
    "qwen3.8-27b": "qwen3.8-flash",
    "qwen3.8-max-preview": "qwen3.8-max",
    "qwen3.7-max": "qwen3.7-plus",
    "qwen3.7-flash": "qwen3.6-flash",
    "qwen3.6-35b-a3b": "qwen3.6-flash",
    "qwen3.6-27b": "qwen3.6-flash",
    "kimi/kimi-k3": "kimi-k2.7-code",
    "kimi-k3": "kimi-k2.7-code",
    "glm-5.3": "glm-5.3-flash",
    "ZHIPU/GLM-5.3": "glm-5.2",
    "glm-5.2-fast-preview": "glm-5.2",
    "glm-5.2": "glm-5.1",
    "deepseek-v4-pro": "deepseek-v4-flash",
    "deepseek-v4-pro-0813": "deepseek-v4-pro",
    "deepseek-v4-flash-0731": "deepseek-v4-flash",
    "xiaomi/mimo-v2.5-pro": "qwen3.8-flash",
    "stepfun/step-3.7-flash": "qwen3.8-flash",
}

_MODEL_REFUSAL_FALLBACK_MAP = {
    "claude-fable-5": "claude-opus-4-8",
    "claude-opus-5": "claude-opus-4-8",
}

_PROVIDER_MODEL_FALLBACK_MAP = {
    "dashscope": {
        "qwen3.8-flash": "qwen3.7-flash",
        "qwen3.6-plus": "qwen3.6-flash",
    },
    "dashscope_token_plan": {
        "qwen3.8-flash": "qwen3.6-flash",
        "qwen3.6-plus": "qwen3.6-flash",
    },
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
    saved_base = provider_cfg.get("apiBase")
    configured_base_url = saved_base if isinstance(saved_base, str) and saved_base else None
    effective_base_url = base_url or configured_base_url or desc.base_url
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
        model=model,
    )
    thinking_intent = _resolve_thinking_intent(
        provider_cfg,
        model,
        request_policy_override,
        provider_key=wire_provider_key,
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
    if _should_use_qwen_provider(provider_cls, model):
        from iac_code.providers.qwen_provider import QwenProvider

        provider_cls = QwenProvider
    request_policy_kwargs: dict[str, Any] = {}
    if thinking_enabled is not None:
        request_policy_kwargs["thinking_enabled"] = thinking_enabled
    from iac_code.providers.openai_provider import OpenAIProvider

    if issubclass(provider_cls, OpenAIProvider):
        if thinking_budget is not None:
            request_policy_kwargs["thinking_budget"] = thinking_budget
        if max_completion_tokens is not None:
            request_policy_kwargs["max_completion_tokens"] = max_completion_tokens
        from iac_code.providers.qwen_provider import QwenProvider

        if issubclass(provider_cls, QwenProvider):
            request_policy_kwargs["thinking_intent"] = thinking_intent
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
    endpoint_url = _provider_endpoint_url(provider) or effective_base_url
    setattr(provider, "_session_endpoint_origin", sanitize_endpoint_origin(endpoint_url))
    setattr(
        provider,
        "_session_endpoint_custom",
        is_custom_endpoint(base_url or configured_base_url, desc.base_url),
    )
    return provider


def _import_provider_class(dotted_path: str):
    """Lazily import a provider class from its dotted path."""
    module_path, class_name = dotted_path.rsplit(".", 1)
    import importlib

    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def _should_use_qwen_provider(provider_cls: type, model: str) -> bool:
    """Select the Qwen adapter only on an existing DashScope transport."""
    from iac_code.providers.dashscope_provider import DashScopeProvider

    return issubclass(provider_cls, DashScopeProvider) and is_qwen_model(model)


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
    *,
    model: str = "",
) -> str:
    if provider_key != "openai_compatible" or not isinstance(base_url, str):
        return provider_key
    wire_key = official_dashscope_wire_provider_key(base_url)
    if wire_key is None:
        return provider_key
    # Preserve the feature's pre-existing non-Qwen route set. New regional and
    # Coding Plan recognition is enabled only for Qwen requests.
    if wire_key == "dashscope" and _is_legacy_standard_dashscope_url(base_url):
        return wire_key
    if wire_key == "dashscope_token_plan" and _is_legacy_token_plan_url(base_url):
        return wire_key
    if is_qwen_model(model):
        return wire_key
    return provider_key


def _is_legacy_standard_dashscope_url(base_url: str) -> bool:
    try:
        from urllib.parse import urlsplit

        host = (urlsplit(base_url.strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host in {"dashscope.aliyuncs.com", "cn-hongkong.dashscope.aliyuncs.com"}


def _is_legacy_token_plan_url(base_url: str) -> bool:
    try:
        from urllib.parse import urlsplit

        host = (urlsplit(base_url.strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host == "token-plan.cn-beijing.maas.aliyuncs.com"


def _resolve_thinking_intent(
    provider_cfg: dict[str, Any],
    model: str,
    request_policy: ProviderRequestPolicy | None,
    *,
    provider_key: str,
):
    from iac_code.providers.thinking_intent import ResolvedThinkingIntent, SourcedValue

    model_cfg = _get_model_provider_config(provider_cfg, model)

    def configured(key: str, converter):
        model_value = converter(model_cfg.get(key)) if key in model_cfg else None
        if model_value is not None:
            return SourcedValue(model_value, "model")
        provider_value = converter(provider_cfg.get(key))
        if provider_value is not None:
            return SourcedValue(provider_value, "provider")
        return SourcedValue()

    def effort_value(value: Any) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    enabled = configured("thinkingEnabled", bool_or_none)
    effort = configured("effort", effort_value)
    budget = configured("thinkingBudget", positive_int_or_none)
    if request_policy is not None:
        if request_policy.thinking_enabled is not None:
            enabled = SourcedValue(request_policy.thinking_enabled, "request")
        if request_policy.effort is not None:
            effort = SourcedValue(request_policy.effort, "request")
        if request_policy.thinking_budget is not None:
            budget = SourcedValue(request_policy.thinking_budget, "request")
    if _legacy_effort_disables_thinking(effort.value):
        if enabled.priority <= effort.priority:
            enabled = SourcedValue(False, effort.source)
        effort = SourcedValue()
    return ResolvedThinkingIntent(enabled=enabled, effort=effort, budget=budget)


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


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _string_provider_attr(provider: Any, name: str) -> str | None:
    value = getattr(provider, name, None)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _bool_provider_attr(provider: Any, name: str) -> bool | None:
    value = getattr(provider, name, None)
    return value if isinstance(value, bool) else None


def _positive_int_provider_attr(provider: Any, name: str) -> int | None:
    return positive_int_or_none(getattr(provider, name, None))


def _provider_endpoint_url(provider: Any) -> str | None:
    base_url = getattr(provider, "_base_url", None)
    if base_url:
        return str(base_url)
    client = getattr(provider, "_client", None)
    client_base_url = getattr(client, "base_url", None)
    return str(client_base_url) if client_base_url else None


def _is_bailian_compatible_endpoint(base_url: str | None) -> bool:
    """Return whether a compatible API URL is an official Bailian endpoint."""
    return is_bailian_compatible_endpoint(base_url)


def _telemetry_provider_name(provider: Any) -> str:
    """Resolve the service provider reported by telemetry without changing the request adapter."""
    wire_provider_key = _string_provider_attr(provider, "_PROVIDER_KEY")
    if wire_provider_key in DASHSCOPE_WIRE_PROVIDER_KEYS:
        return "dashscope"
    if _is_bailian_compatible_endpoint(_provider_endpoint_url(provider)):
        return "dashscope"
    return type(provider).__name__.replace("Provider", "").lower()


def _provider_telemetry_attrs(provider: Any) -> dict[str, str | bool]:
    attrs: dict[str, str | bool] = {
        IacCodeAttr.OFFICIAL_ENDPOINT: official_dashscope_wire_provider_key(_provider_endpoint_url(provider))
        is not None,
    }
    adapter_name = _string_provider_attr(provider, "_ADAPTER_NAME")
    if adapter_name is not None:
        attrs[IacCodeAttr.PROVIDER_ADAPTER] = adapter_name
    return attrs


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
        self._lease_owner_identity = object()
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

    def begin_request(
        self,
        system: str,
        tools: list[ToolDefinition] | None = None,
    ) -> ProviderRequestLease:
        """Atomically bind one request to its provider, model, and prompt."""
        self._check_qwenpaw_config_change()
        provider, actual_model = self._active_provider_and_model()
        prompt_preparer = getattr(type(provider), "prepare_system_prompt", None)
        effective_system = prompt_preparer(provider, system, tools) if callable(prompt_preparer) else system
        logical_key = _string_provider_attr(provider, "_logical_provider_key") or self.get_provider_key()
        wire_key = _string_provider_attr(provider, "_PROVIDER_KEY") or logical_key
        adapter_name = _string_provider_attr(provider, "_ADAPTER_NAME")
        return ProviderRequestLease(
            request_id=f"req_{uuid.uuid4().hex}",
            provider=provider,
            system_prompt=effective_system,
            requested_model=self._model,
            logical_provider_key=logical_key,
            wire_provider_key=wire_key,
            telemetry_provider_name=_telemetry_provider_name(provider),
            adapter_name=adapter_name,
            context_window_model=actual_model,
            _owner_identity=self._lease_owner_identity,
            _lease_token=LeaseToken(),
        )

    def _consume_request_lease(self, lease: ProviderRequestLease) -> None:
        self._validate_request_lease(lease)
        token = lease._lease_token
        if token.state != "active":
            raise ProviderRequestLeaseError(
                "Provider request lease cannot be consumed from state {state}.",
                state=repr(token.state),
            )
        token.state = "consumed"

    def release_request(self, lease: ProviderRequestLease) -> None:
        self._validate_request_lease(lease)
        token = lease._lease_token
        if token.state == "released":
            raise ProviderRequestLeaseError("Provider request lease was already released.")
        token.state = "released"

    def _validate_request_lease(self, lease: ProviderRequestLease) -> None:
        if not isinstance(lease, ProviderRequestLease) or lease._owner_identity is not self._lease_owner_identity:
            raise ProviderRequestLeaseError("Provider request lease belongs to a different manager.")

    def _usage_attribution(
        self,
        lease: ProviderRequestLease,
        *,
        provider: Provider,
        actual_model: str,
        telemetry_provider_name: str | None = None,
    ) -> UsageAttribution:
        return UsageAttribution(
            logical_provider_key=lease.logical_provider_key,
            wire_provider_key=_string_provider_attr(provider, "_PROVIDER_KEY") or lease.wire_provider_key,
            telemetry_provider_name=telemetry_provider_name or _telemetry_provider_name(provider),
            adapter_name=_string_provider_attr(provider, "_ADAPTER_NAME"),
            requested_model=lease.requested_model,
            actual_model=actual_model,
        )

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

    def session_start_settings(self) -> dict[str, Any]:
        """Return non-sensitive provider settings that affect session performance."""
        provider = self._provider
        policy = self._request_policy_override
        max_completion_tokens = _first_not_none(
            _positive_int_provider_attr(provider, "_max_completion_tokens"),
            _positive_int_provider_attr(provider, "_max_output_tokens"),
            policy.max_completion_tokens if policy is not None else None,
        )
        return {
            "provider": self.get_provider_key() or None,
            "provider_display": self.get_provider_display() or None,
            "model": self.get_model_name(),
            "effort": _first_not_none(
                _string_provider_attr(provider, "_effort"),
                self._effort_override,
                policy.effort if policy is not None else None,
            ),
            "thinking_enabled": _first_not_none(
                _bool_provider_attr(provider, "_thinking_enabled"),
                policy.thinking_enabled if policy is not None else None,
            ),
            "thinking_budget": _first_not_none(
                _positive_int_provider_attr(provider, "_thinking_budget"),
                policy.thinking_budget if policy is not None else None,
            ),
            "max_completion_tokens": max_completion_tokens,
            "stream_idle_timeout": self._stream_idle_timeout,
            "endpoint_origin": _string_provider_attr(provider, "_session_endpoint_origin"),
            "endpoint_custom": _bool_provider_attr(provider, "_session_endpoint_custom"),
        }

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
        # The source model may be a legacy ID that left the selectable
        # catalog but is still callable (e.g. qwen3.8-max-preview after the
        # preview ended), so only the fallback target must stay in-catalog.
        return fallback if fallback in model_ids else None

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

    def stream(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 8192,
        telemetry_messages: list[Any] | None = None,
        *,
        lease: ProviderRequestLease | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        captured_scope = _safe_span_scope_attributes()
        captured_parent = _safe_current_context()
        return self._stream_with_request_lease(
            messages,
            system,
            tools,
            max_tokens,
            lease=lease,
            telemetry_messages=telemetry_messages,
            captured_scope=captured_scope,
            captured_parent=captured_parent,
        )

    async def _stream_with_request_lease(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None,
        max_tokens: int,
        *,
        lease: ProviderRequestLease | None,
        telemetry_messages: list[Any] | None,
        captured_scope: dict[str, str | int],
        captured_parent: Any,
    ) -> AsyncGenerator[StreamEvent, None]:
        implicit_lease = lease is None
        try:
            active_lease = lease or self.begin_request(system, tools)
            self._consume_request_lease(active_lease)
            if system != active_lease.system_prompt:
                if implicit_lease:
                    system = active_lease.system_prompt
                else:
                    raise ProviderRequestLeaseError(
                        "Explicit request lease system prompt does not match the streamed prompt."
                    )
            request_stream = self._stream_impl(
                messages,
                system,
                tools,
                max_tokens,
                lease=active_lease,
                telemetry_messages=telemetry_messages,
                captured_scope=captured_scope,
                captured_parent=captured_parent,
            )
            try:
                while True:
                    try:
                        event = await anext(request_stream)
                    except StopAsyncIteration:
                        break
                    yield event
            finally:
                await request_stream.aclose()
        except ProviderConfigurationError as exc:
            yield _error_event_from_exception(exc)
        finally:
            if implicit_lease and "active_lease" in locals() and active_lease._lease_token.state != "released":
                self.release_request(active_lease)

    async def _stream_attempt(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None,
        max_tokens: int,
        *,
        lease: ProviderRequestLease,
        telemetry_messages: list[Any] | None,
        captured_scope: dict[str, str | int],
        captured_parent: Any,
        outcome: _StreamAttemptOutcome,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Run one streaming request, reporting how it ended through *outcome*.

        Yields only what the stream itself produced; deciding whether a failed
        attempt is retried or downgraded to a non-streaming request belongs to
        ``_stream_impl``.
        """
        provider = lease.provider
        model = lease.context_window_model
        provider_name = _telemetry_provider_name(provider)
        provider_identity_attrs = _provider_telemetry_attrs(provider)
        sanitized_model = sanitize_model_name(model)
        outcome.provider_name = provider_name
        outcome.sanitized_model = sanitized_model

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
            **provider_identity_attrs,
        }
        span_attrs.update(captured_scope)
        _capture_request_content(span_attrs, messages, system, tools, telemetry_messages)

        orphaned_message_ids: list[str] = []
        orphaned_tool_use_ids: dict[str, list[str]] = {}
        current_message_id: str | None = None
        buffer_until_accepted = model in _MODEL_REFUSAL_FALLBACK_MAP
        buffered_events: list[StreamEvent] = []
        streaming_failed = False
        stream_failure_exception: BaseException | None = None
        refusal_detected = False
        first_token_received = False
        watchdog: StreamWatchdog | None = None
        stream_iter: AsyncGenerator[StreamEvent, None] | None = None
        terminal_status: str | None = None
        close_attempted = False
        close_completed = False
        end_attempted = False
        idle_timeout_hit = False

        with replace_span_attributes(captured_scope):
            span = _safe_start_detached_span(span_name, span_attrs, captured_parent)

        @contextmanager
        def activate_span() -> Iterator[None]:
            with replace_span_attributes(captured_scope), _safe_use_span(span):
                yield

        def end_span_once() -> None:
            nonlocal end_attempted
            if end_attempted:
                return
            end_attempted = True
            try:
                with replace_span_attributes(captured_scope):
                    span.end()
            except Exception:
                logger.opt(exception=True).warning("Provider detached telemetry span failed to end: span={}", span_name)

        def has_propagating_primary(primary: BaseException | None) -> bool:
            return isinstance(primary, (asyncio.CancelledError, GeneratorExit)) or (
                primary is not None and not isinstance(primary, Exception)
            )

        async def close_stream_iter_once(primary: BaseException | None = None) -> None:
            nonlocal close_attempted, close_completed
            if stream_iter is None or close_attempted:
                return
            close_attempted = True
            try:
                with activate_span():
                    await stream_iter.aclose()
            except asyncio.CancelledError:
                task = asyncio.current_task()
                cancelling = getattr(task, "cancelling", None)
                if callable(cancelling) and cancelling() > 0:
                    raise
                if has_propagating_primary(primary):
                    logger.opt(exception=True).warning(
                        "Provider stream close cancellation suppressed behind primary outcome"
                    )
                    return
                raise
            except Exception:
                logger.opt(exception=True).warning("Provider stream close failed")
            except BaseException:
                if has_propagating_primary(primary):
                    logger.opt(exception=True).warning(
                        "Provider stream fatal close failure suppressed behind primary outcome"
                    )
                    return
                raise
            else:
                close_completed = True

        def commit_success(event: MessageEndEvent, *, status: str = "ok") -> bool:
            nonlocal terminal_status
            if terminal_status is not None:
                return False
            terminal_status = status
            event.usage.provider = provider_name
            event.usage.model = sanitized_model
            with activate_span():
                self._set_llm_response_span_attrs(span, event, model)
                self._emit_success_telemetry(
                    provider_name,
                    sanitized_model,
                    started,
                    event.usage,
                    status=status,
                    identity_attrs=provider_identity_attrs,
                )
            return True

        def commit_failure(
            exc: BaseException,
            *,
            status: str,
            mark_span_error: bool,
            record_exception: bool,
            error_description: str | None = None,
        ) -> bool:
            nonlocal terminal_status
            if terminal_status is not None:
                return False
            terminal_status = status
            with activate_span():
                description = error_description or public_exception_summary(exc, max_chars=1000)
                if record_exception:
                    span.record_exception_once(exc)
                if mark_span_error:
                    span.set_error_status_once(description)
                self._emit_failure_telemetry(
                    provider_name,
                    sanitized_model,
                    started,
                    exc,
                    status=status,
                    record_duration=True,
                    scope_attrs=captured_scope,
                    identity_attrs=provider_identity_attrs,
                )
            return True

        try:
            with activate_span():
                _safe_log_event(
                    Events.API_REQUEST_STARTED,
                    {
                        "provider": provider_name,
                        "model": sanitized_model,
                        "message_count": len(messages),
                        **provider_identity_attrs,
                    },
                )
            watchdog = StreamWatchdog(idle_timeout=self._stream_idle_timeout)
            watchdog.start()
            stream_iter = provider.stream(messages, system, tools, max_tokens)
            while True:
                try:
                    with activate_span():
                        event = await asyncio.wait_for(stream_iter.__anext__(), timeout=self._stream_idle_timeout)
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
                                    **provider_identity_attrs,
                                    **captured_scope,
                                },
                            )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    # Stream idle watchdog fired: no event arrived within the idle
                    # window. Emit a rich diagnostic before re-raising into the generic
                    # handler (whose asyncio.TimeoutError carries an empty message, so
                    # the generic handler's message alone is useless). message_started
                    # disambiguates the two failure shapes:
                    #   message_started=False → nothing arrived at all (request never got
                    #     a response: connection-level / upstream-queue stall);
                    #   message_started=True, first_token_received=False → response opened
                    #     then went silent before any content (mid-stream / slow generation).
                    # scope carries the pipeline candidate, so a parallel-candidate stall
                    # can be attributed to the exact candidate that starved.
                    # An exhausted idle window is not worth another stream: retrying
                    # would stall for the same window again before downgrading.
                    idle_timeout_hit = True
                    idle_elapsed = time.monotonic() - started
                    logger.warning(
                        "Provider stream idle timeout: waited {:.1f}s (idle_limit={:.0f}s) "
                        "with no further stream event; message_started={}, "
                        "first_token_received={}, provider={}, model={}, scope={}",
                        idle_elapsed,
                        self._stream_idle_timeout,
                        current_message_id is not None,
                        first_token_received,
                        provider_name,
                        sanitized_model,
                        captured_scope,
                    )
                    raise

                if isinstance(event, MessageEndEvent):
                    event = replace(
                        event,
                        usage_attribution=self._usage_attribution(
                            lease,
                            provider=provider,
                            actual_model=model,
                            telemetry_provider_name=provider_name,
                        ),
                    )
                    watchdog.stop()
                    if event.stop_reason == "refusal":
                        commit_success(event, status="refusal")
                        refusal_detected = True
                        streaming_failed = True
                        try:
                            await close_stream_iter_once()
                        finally:
                            end_span_once()
                        logger.warning("Streaming response was refused, falling back to an approved model")
                        break
                    commit_success(event)
                    try:
                        await close_stream_iter_once()
                    finally:
                        end_span_once()
                    if buffer_until_accepted:
                        for buffered_event in buffered_events:
                            yield buffered_event
                    yield event
                    return
                if buffer_until_accepted:
                    buffered_events.append(event)
                else:
                    yield event

            if not refusal_detected:
                failure_message = "Streaming response ended before message completion"
                streaming_failed = True
                commit_failure(
                    RuntimeError(failure_message),
                    status="error",
                    mark_span_error=True,
                    record_exception=False,
                    error_description=failure_message,
                )
                try:
                    await close_stream_iter_once()
                finally:
                    end_span_once()
        except asyncio.CancelledError as exc:
            commit_failure(
                exc,
                status="cancelled",
                mark_span_error=False,
                record_exception=False,
            )
            try:
                await close_stream_iter_once(exc)
            finally:
                end_span_once()
            raise
        except GeneratorExit as exc:
            commit_failure(
                exc,
                status="cancelled",
                mark_span_error=False,
                record_exception=False,
            )
            try:
                await close_stream_iter_once(exc)
            finally:
                end_span_once()
            raise
        except Exception as exc:
            stream_failure_exception = exc
            already_terminal = terminal_status is not None
            commit_failure(
                exc,
                status="error",
                mark_span_error=True,
                record_exception=True,
            )
            try:
                await close_stream_iter_once()
            finally:
                end_span_once()
            if already_terminal:
                raise
            streaming_failed = True
            if isinstance(exc, UnsafeStreamProtocolError):
                logger.warning("Unsafe Qwen stream detected; replaying with streaming protocol validation")
            else:
                logger.warning(f"Streaming failed, falling back to non-streaming: {exc}")
        except BaseException as exc:
            commit_failure(
                exc,
                status="error",
                mark_span_error=True,
                record_exception=True,
            )
            try:
                await close_stream_iter_once(exc)
            finally:
                end_span_once()
            raise
        finally:
            if watchdog is not None:
                watchdog.stop()

        outcome.streaming_failed = streaming_failed
        outcome.refusal_detected = refusal_detected
        outcome.buffer_until_accepted = buffer_until_accepted
        outcome.orphaned_message_ids = orphaned_message_ids
        outcome.orphaned_tool_use_ids = orphaned_tool_use_ids
        outcome.stream_failure_exception = stream_failure_exception
        if (
            streaming_failed
            and stream_failure_exception is not None
            and not idle_timeout_hit
            and _is_retryable_provider_error(stream_failure_exception)
        ):
            outcome.retryable_stream_error = stream_failure_exception

    async def _stream_impl(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None,
        max_tokens: int,
        *,
        lease: ProviderRequestLease,
        telemetry_messages: list[Any] | None,
        captured_scope: dict[str, str | int],
        captured_parent: Any,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream one turn, retrying the stream itself before downgrading it.

        A transient provider failure (429, 5xx, dropped connection) used to
        downgrade the whole turn to a single non-streaming request. That
        downgrade is invisible to the caller and emits nothing until the model
        has finished the *entire* answer — 75s of silence on one observed
        pipeline step, which the UI can only render as a step frozen mid-run.
        Repeating the stream with the same backoff the non-streaming path uses
        keeps incremental output instead. A retry is only safe while nothing has
        reached the caller: once events are out, a second attempt would
        duplicate them, so that case still downgrades (behind a tombstone).
        """
        provider = lease.provider
        model = lease.context_window_model
        provider_name = _telemetry_provider_name(provider)
        provider_identity_attrs = _provider_telemetry_attrs(provider)
        sanitized_model = sanitize_model_name(model)
        attempt = 0
        # Bound before the loop as well so the post-loop read is unambiguous.
        outcome = _StreamAttemptOutcome()
        while True:
            outcome = _StreamAttemptOutcome()
            emitted = False
            attempt_stream = self._stream_attempt(
                messages,
                system,
                tools,
                max_tokens,
                lease=lease,
                telemetry_messages=telemetry_messages,
                captured_scope=captured_scope,
                captured_parent=captured_parent,
                outcome=outcome,
            )
            try:
                async for event in attempt_stream:
                    emitted = True
                    yield event
            finally:
                await attempt_stream.aclose()
            retryable_error = outcome.retryable_stream_error
            if retryable_error is None or emitted or attempt >= self._retry_config.max_retries:
                break
            delay = self._retry_config.calculate_delay(attempt)
            attempt += 1
            logger.warning(
                "Streaming failed before any event reached the caller; retrying the stream "
                "in {:.1f}s (attempt {}/{}): {}",
                delay,
                attempt,
                self._retry_config.max_retries,
                retryable_error,
            )
            _safe_log_event(
                Events.API_REQUEST_RETRIED,
                {
                    "provider": outcome.provider_name,
                    "model": outcome.sanitized_model,
                    "attempt": attempt,
                    "error_type": type(retryable_error).__name__,
                    "streaming": True,
                    **provider_identity_attrs,
                },
            )
            await asyncio.sleep(delay)

        stream_failure_exception = outcome.stream_failure_exception
        if outcome.streaming_failed:
            logger.warning("Falling back to non-streaming after the stream failed")
            if not outcome.buffer_until_accepted:
                for msg_id in outcome.orphaned_message_ids:
                    yield TombstoneEvent(
                        message_id=msg_id,
                        affected_tool_use_ids=outcome.orphaned_tool_use_ids.get(msg_id, []),
                    )
            if isinstance(stream_failure_exception, UnsafeStreamProtocolError):
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
                    **provider_identity_attrs,
                    **captured_scope,
                }
                _capture_request_content(span_attrs, messages, system, tools, telemetry_messages)
                last_unsafe_error: BaseException = stream_failure_exception
                for _replay_attempt in range(2):
                    replay_message_ids: list[str] = []
                    replay_tool_ids: dict[str, list[str]] = {}
                    replay_current_message: str | None = None
                    replay_iter: AsyncGenerator[StreamEvent, None] | None = None
                    replay_started = time.monotonic()
                    replay_span = _safe_start_detached_span(span_name, span_attrs, captured_parent)
                    replay_span_ended = False

                    @contextmanager
                    def activate_replay_span() -> Iterator[None]:
                        with replace_span_attributes(captured_scope), _safe_use_span(replay_span):
                            yield

                    def end_replay_span_once() -> None:
                        nonlocal replay_span_ended
                        if replay_span_ended:
                            return
                        replay_span_ended = True
                        try:
                            with replace_span_attributes(captured_scope):
                                replay_span.end()
                        except Exception:
                            logger.opt(exception=True).warning(
                                "Provider replay telemetry span failed to end: span={}", span_name
                            )

                    def commit_replay_failure(
                        exc: BaseException,
                        *,
                        status: str = "error",
                        mark_span_error: bool = True,
                        record_exception: bool = True,
                    ) -> None:
                        with activate_replay_span():
                            description = public_exception_summary(exc, max_chars=1000)
                            if record_exception:
                                replay_span.record_exception_once(exc)
                            if mark_span_error:
                                replay_span.set_error_status_once(description)
                            self._emit_failure_telemetry(
                                provider_name,
                                sanitized_model,
                                replay_started,
                                exc,
                                status=status,
                                record_duration=True,
                                scope_attrs=captured_scope,
                                identity_attrs=provider_identity_attrs,
                            )
                        end_replay_span_once()

                    with activate_replay_span():
                        _safe_log_event(
                            Events.API_REQUEST_STARTED,
                            {
                                "provider": provider_name,
                                "model": sanitized_model,
                                "message_count": len(messages),
                                "replay_attempt": _replay_attempt + 1,
                                **provider_identity_attrs,
                            },
                        )
                    replay_first_token_received = False
                    try:
                        replay_iter = provider.stream(messages, system, tools, max_tokens)
                        async for replay_event in replay_iter:
                            if isinstance(replay_event, MessageStartEvent):
                                replay_message_ids.append(replay_event.message_id)
                                replay_current_message = replay_event.message_id
                                replay_tool_ids.setdefault(replay_event.message_id, [])
                                replay_span.set_attribute(GenAiAttr.RESPONSE_ID, replay_event.message_id)
                            elif (
                                isinstance(replay_event, (ToolUseStartEvent, ToolUseEndEvent))
                                and replay_current_message is not None
                            ):
                                ids = replay_tool_ids.setdefault(replay_current_message, [])
                                if replay_event.tool_use_id not in ids:
                                    ids.append(replay_event.tool_use_id)
                            elif (
                                isinstance(replay_event, (TextDeltaEvent, ThinkingDeltaEvent))
                                and replay_event.text
                                and not replay_first_token_received
                            ):
                                replay_first_token_received = True
                                ttft_ns = int((time.monotonic() - replay_started) * 1_000_000_000)
                                replay_span.set_attribute(GenAiAttr.RESPONSE_TIME_TO_FIRST_TOKEN, ttft_ns)
                                with activate_replay_span():
                                    _safe_log_event(
                                        Events.API_RESPONSE_FIRST_TOKEN,
                                        {
                                            "provider": provider_name,
                                            "model": sanitized_model,
                                            GenAiAttr.RESPONSE_TIME_TO_FIRST_TOKEN: ttft_ns,
                                            "first_token_source": replay_event.type,
                                            "replay_attempt": _replay_attempt + 1,
                                            **provider_identity_attrs,
                                            **captured_scope,
                                        },
                                    )
                            if isinstance(replay_event, MessageEndEvent):
                                replay_event.usage.provider = provider_name
                                replay_event.usage.model = sanitized_model
                                terminal_event = replace(
                                    replay_event,
                                    usage_attribution=self._usage_attribution(
                                        lease,
                                        provider=provider,
                                        actual_model=model,
                                        telemetry_provider_name=provider_name,
                                    ),
                                )
                                with activate_replay_span():
                                    self._set_llm_response_span_attrs(replay_span, terminal_event, model)
                                    self._emit_success_telemetry(
                                        provider_name,
                                        sanitized_model,
                                        replay_started,
                                        terminal_event.usage,
                                        identity_attrs=provider_identity_attrs,
                                    )
                                end_replay_span_once()
                                yield terminal_event
                                return
                            yield replay_event
                        raise UnsafeStreamProtocolError("Qwen replay ended before message completion.")
                    except UnsafeStreamProtocolError as exc:
                        last_unsafe_error = exc
                        commit_replay_failure(exc)
                    except asyncio.CancelledError as exc:
                        commit_replay_failure(
                            exc,
                            status="cancelled",
                            mark_span_error=False,
                            record_exception=False,
                        )
                        raise
                    except GeneratorExit as exc:
                        commit_replay_failure(
                            exc,
                            status="cancelled",
                            mark_span_error=False,
                            record_exception=False,
                        )
                        raise
                    except Exception as exc:
                        commit_replay_failure(exc)
                        for msg_id in replay_message_ids:
                            yield TombstoneEvent(
                                message_id=msg_id,
                                affected_tool_use_ids=replay_tool_ids.get(msg_id, []),
                            )
                        yield _error_event_from_exception(exc)
                        return
                    except BaseException as exc:
                        commit_replay_failure(exc)
                        raise
                    finally:
                        if replay_iter is not None:
                            try:
                                await replay_iter.aclose()
                            except Exception:
                                logger.opt(exception=True).warning("Qwen replay stream close failed")
                        end_replay_span_once()
                    for msg_id in replay_message_ids:
                        yield TombstoneEvent(
                            message_id=msg_id,
                            affected_tool_use_ids=replay_tool_ids.get(msg_id, []),
                        )
                yield _error_event_from_exception(last_unsafe_error)
                return
            try:
                with replace_span_attributes(captured_scope), _safe_attach_parent_context(captured_parent):
                    completion = await self._complete_with_retry_result(
                        messages,
                        system,
                        tools,
                        max_tokens,
                        provider_override=provider,
                        model_override=model,
                        telemetry_messages=telemetry_messages,
                        refusal_detected=outcome.refusal_detected,
                    )
            except Exception as e:
                yield _error_event_from_exception(e)
                return
            response = completion.response
            response.usage.provider = completion.provider_name
            response.usage.model = sanitize_model_name(completion.model)
            completion_provider = getattr(completion, "provider", provider)
            completion_model = getattr(completion, "model", model)
            completion_provider_name = getattr(completion, "provider_name", provider_name)
            attribution = self._usage_attribution(
                lease,
                provider=completion_provider,
                actual_model=completion_model,
                telemetry_provider_name=completion_provider_name,
            )
            response = replace(response, usage_attribution=attribution)
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
                    input_error=tu.get("input_error"),
                )
            yield MessageEndEvent(
                stop_reason=response.stop_reason,
                usage=response.usage,
                usage_attribution=attribution,
            )

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
        identity_attrs: dict[str, str | bool] | None = None,
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
                **(identity_attrs or {}),
                **scope_attrs,
            },
        )
        request_metric_attrs = {
            "provider": provider_name,
            "model": model,
            **(identity_attrs or {}),
        }
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
        scope_attrs: dict[str, str | int] | None = None,
        identity_attrs: dict[str, str | bool] | None = None,
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
            "status": status,
            **(identity_attrs or {}),
            **(scope_attrs or {}),
        }
        _safe_log_event(
            Events.API_REQUEST_FAILED,
            event_attrs,
        )
        request_metric_attrs = {
            "provider": provider_name,
            "model": model,
            **(identity_attrs or {}),
        }
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
        telemetry_messages: list[Any] | None = None,
        *,
        lease: ProviderRequestLease | None = None,
    ) -> NonStreamingResponse:
        implicit_lease = lease is None
        try:
            active_lease = lease or self.begin_request(system, tools)
            self._consume_request_lease(active_lease)
            if system != active_lease.system_prompt:
                if implicit_lease:
                    system = active_lease.system_prompt
                else:
                    raise ProviderRequestLeaseError(
                        "Explicit request lease system prompt does not match the completion prompt."
                    )
            result = await self._complete_with_retry_result(
                messages,
                system,
                tools,
                max_tokens,
                provider_override=active_lease.provider,
                model_override=active_lease.context_window_model,
                cache_policy=cache_policy,
                telemetry_messages=telemetry_messages,
            )
            attribution = self._usage_attribution(
                active_lease,
                provider=result.provider,
                actual_model=result.model,
                telemetry_provider_name=result.provider_name,
            )
            return replace(result.response, usage_attribution=attribution)
        finally:
            if implicit_lease and "active_lease" in locals() and active_lease._lease_token.state != "released":
                self.release_request(active_lease)

    async def _complete_with_retry(
        self,
        messages,
        system,
        tools,
        max_tokens,
        provider_override: Provider | None = None,
        model_override: str | None = None,
        cache_policy: str = "default",
        telemetry_messages: list[Any] | None = None,
    ) -> NonStreamingResponse:
        result = await self._complete_with_retry_result(
            messages,
            system,
            tools,
            max_tokens,
            provider_override=provider_override,
            model_override=model_override,
            cache_policy=cache_policy,
            telemetry_messages=telemetry_messages,
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
        telemetry_messages: list[Any] | None = None,
    ) -> _CompletionResult:
        if provider_override is None and model_override is None:
            provider, model = self._active_provider_and_model()
        else:
            provider = provider_override or self._ensure_provider()
            model = model_override or self._model
        visited = set(fallback_visited or ())
        visited.add(model)
        provider_name = _telemetry_provider_name(provider)
        provider_identity_attrs = _provider_telemetry_attrs(provider)
        sanitized_model = sanitize_model_name(model)

        async def _on_retry(attempt, exc, delay):
            _safe_log_event(
                Events.API_REQUEST_RETRIED,
                {
                    "provider": provider_name,
                    "model": sanitized_model,
                    "attempt": attempt,
                    "error_type": type(exc).__name__,
                    **provider_identity_attrs,
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
                **provider_identity_attrs,
                **_safe_span_scope_attributes(),
            }
            _capture_request_content(span_attrs, messages, system, tools, telemetry_messages)

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
                                **provider_identity_attrs,
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
                            identity_attrs=provider_identity_attrs,
                        )
                        raise
                    except Exception as exc:
                        self._emit_failure_telemetry(
                            provider_name,
                            sanitized_model,
                            request_started,
                            exc,
                            identity_attrs=provider_identity_attrs,
                        )
                        raise
                    except BaseException as exc:
                        self._emit_failure_telemetry(
                            provider_name,
                            sanitized_model,
                            request_started,
                            exc,
                            status="error",
                            record_duration=True,
                            scope_attrs=_safe_span_scope_attributes(),
                            identity_attrs=provider_identity_attrs,
                        )
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
                        identity_attrs=provider_identity_attrs,
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
                retryable_status = _retryable_provider_status(e)
                if retryable_status is not None:
                    raise RetryableError(f"{type(e).__name__}: {e}", status_code=retryable_status) from e
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
                        **provider_identity_attrs,
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
                    fallback_prompt_preparer = getattr(type(fallback_provider), "prepare_system_prompt", None)
                    fallback_system = (
                        fallback_prompt_preparer(fallback_provider, system, tools)
                        if callable(fallback_prompt_preparer)
                        else system
                    )
                    result = await self._complete_with_retry_result(
                        messages,
                        fallback_system,
                        tools,
                        max_tokens,
                        provider_override=fallback_provider,
                        model_override=fallback,
                        cache_policy=cache_policy,
                        fallback_visited=frozenset(visited),
                        allow_model_fallback=not isinstance(original_exc, _ModelRefusalError),
                        telemetry_messages=telemetry_messages,
                    )
                    if isinstance(original_exc, _ModelRefusalError):
                        self._pinned_provider = result.provider
                        self._pinned_model = result.model
                    return result
                except Exception:
                    raise original_exc from None
            raise
