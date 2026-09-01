"""Qwen model adapter for DashScope OpenAI-compatible endpoints."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from iac_code.providers.base import NonStreamingResponse, ToolDefinition
from iac_code.providers.dashscope_endpoints import official_dashscope_wire_provider_key
from iac_code.providers.dashscope_provider import _EXPLICIT_CACHE_MODEL_PREFIXES, DashScopeProvider
from iac_code.providers.model_family import normalized_model_name
from iac_code.providers.openai_provider import ChatRequestContext
from iac_code.providers.qwen_prompts import prepare_qwen_system_prompt
from iac_code.providers.qwen_tool_call_parser import parse_non_streaming_tool_calls, recover_xml_tool_calls
from iac_code.providers.schema_compat import relax_qwen_tool_schema
from iac_code.providers.streaming import QwenStreamResponseAdapter
from iac_code.providers.thinking import ThinkingFamily, get_thinking_spec, normalize_effort
from iac_code.providers.thinking_intent import ResolvedThinkingIntent
from iac_code.services.telemetry import add_metric, log_event
from iac_code.services.telemetry.names import Events, IacCodeAttr, Metrics
from iac_code.services.telemetry.sanitize import sanitize_error_message, sanitize_model_name
from iac_code.services.telemetry.scope import get_span_attributes
from iac_code.utils.public_errors import public_error, public_exception_summary


class QwenProvider(DashScopeProvider):
    """DashScope transport with Qwen-only request and response adaptation."""

    _ADAPTER_NAME = "qwen"

    def __init__(self, *args: Any, thinking_intent: ResolvedThinkingIntent | None = None, **kwargs: Any) -> None:
        self._thinking_intent = thinking_intent or ResolvedThinkingIntent()
        self._learned_mandatory_thinking = False
        super().__init__(*args, **kwargs)

    def prepare_system_prompt(self, system: str, tools: list[ToolDefinition] | None) -> str:
        return prepare_qwen_system_prompt(system, self._model, tools)

    def _supports_explicit_cache(self) -> bool:
        model = normalized_model_name(self._model)
        model_supported = model.startswith(_EXPLICIT_CACHE_MODEL_PREFIXES)
        return official_dashscope_wire_provider_key(str(self._base_url or "")) is not None and model_supported

    def _create_stream_response_adapter(self, tools: list[ToolDefinition] | None) -> QwenStreamResponseAdapter:
        return QwenStreamResponseAdapter(self, tools)

    def _parse_non_streaming_tool_calls(self, tool_calls: Any) -> list[dict[str, Any]]:
        return parse_non_streaming_tool_calls(tool_calls)

    def _postprocess_non_streaming_response(
        self,
        raw_message: Any,
        response: NonStreamingResponse,
        tools: list[ToolDefinition] | None,
    ) -> NonStreamingResponse:
        if response.tool_uses:
            return response
        recovered = recover_xml_tool_calls(response.text, tools)
        if recovered is None:
            return response
        return replace(
            response,
            text=recovered.remaining_text,
            tool_uses=recovered.calls,
            stop_reason="tool_use",
        )

    def _prepare_tool_schema_for_wire(self, schema: dict[str, Any]) -> dict[str, Any]:
        return relax_qwen_tool_schema(schema)

    def _request_headers(self, *, cache_policy: str = "default") -> dict[str, str]:
        if cache_policy == "no_explicit_cache" or not self._supports_explicit_cache():
            return {}
        return {"X-DashScope-CacheControl": "enable"}

    def _build_api_tools(
        self,
        tools: list[ToolDefinition],
        *,
        streaming: bool,
        cache_policy: str = "default",
    ) -> list[dict[str, Any]]:
        api_tools = super()._build_api_tools(tools, streaming=streaming, cache_policy=cache_policy)
        if streaming and cache_policy != "no_explicit_cache" and self._supports_explicit_cache() and api_tools:
            api_tools[-1]["cache_control"] = {"type": "ephemeral"}
        return api_tools

    def _build_thinking_kwargs(self) -> dict[str, Any]:
        return self._build_thinking_kwargs_with_mandatory(self._known_mandatory_thinking())

    def _known_mandatory_thinking(self) -> bool:
        normalized_model = normalized_model_name(self._model)
        token_plan_mandatory = self._PROVIDER_KEY == "dashscope_token_plan" and (
            normalized_model == "qwen3.8-max" or normalized_model.startswith("qwen3.8-max-preview")
        )
        return self._learned_mandatory_thinking or token_plan_mandatory

    def _build_thinking_kwargs_with_mandatory(self, mandatory: bool) -> dict[str, Any]:
        spec = get_thinking_spec(self._PROVIDER_KEY, self._model)
        if spec.family is not ThinkingFamily.DASHSCOPE:
            return {}
        normalized_model = normalized_model_name(self._model)
        is_qwen38 = normalized_model.startswith("qwen3.8-max")
        if not is_qwen38:
            return self._build_legacy_qwen_thinking_kwargs(spec, mandatory=mandatory)

        effort = normalize_effort(self._effort)
        if effort == "max":
            effort = "xhigh"
        allowed = {item.value for item in spec.allowed_efforts}
        extra_body: dict[str, Any] = {}
        if self._supports_preserve_thinking():
            extra_body["preserve_thinking"] = True

        dominant = self._thinking_intent.dominant_concrete_field()
        resolved_enabled = self._thinking_intent.enabled.value
        disabled = (
            not resolved_enabled
            if resolved_enabled is not None
            else self._thinking_disabled()
            or effort in {"none", "off", "disable", "disabled", "false", "0"}
        )
        effort_is_disable = effort in {"none", "off", "disable", "disabled", "false", "0"}
        concrete_priority = (
            self._thinking_intent.effort.priority
            if dominant == "effort"
            else self._thinking_intent.budget.priority
        )
        if (
            dominant in {"effort", "budget"}
            and not effort_is_disable
            and concrete_priority > self._thinking_intent.enabled.priority
        ):
            disabled = False
        elif dominant == "disabled":
            disabled = True
        if disabled and not mandatory and spec.supports_disable:
            kwargs: dict[str, Any] = {"reasoning_effort": "none"}
            if extra_body:
                kwargs["extra_body"] = extra_body
            return kwargs

        selected_budget = self._selected_thinking_budget(dominant, effort)
        if selected_budget is not None:
            budget_body = dict(extra_body)
            budget_body["enable_thinking"] = True
            budget_body["thinking_budget"] = selected_budget
            return {"extra_body": budget_body}

        if effort in allowed:
            resolved_effort = effort
        elif effort not in {None, "auto"} and spec.default_effort is not None:
            resolved_effort = spec.default_effort.value
        elif (
            resolved_enabled is True
            or self._thinking_forced()
            or (not disabled and dominant is None and spec.thinking_enabled_by_default)
        ) and spec.default_effort is not None:
            resolved_effort = spec.default_effort.value
        else:
            resolved_effort = None
        kwargs = {}
        if resolved_effort is not None:
            kwargs["reasoning_effort"] = resolved_effort
        if extra_body:
            kwargs["extra_body"] = extra_body
        if mandatory and resolved_effort is None:
            mandatory_body = dict(kwargs.get("extra_body") or {})
            mandatory_body["enable_thinking"] = True
            kwargs["extra_body"] = mandatory_body
        return kwargs

    def _build_legacy_qwen_thinking_kwargs(self, spec: Any, *, mandatory: bool) -> dict[str, Any]:
        effort = normalize_effort(self._effort)
        effort_is_disable = effort in {"none", "off", "disable", "disabled", "false", "0"}
        resolved_enabled = self._thinking_intent.enabled.value
        disabled = (
            not resolved_enabled
            if resolved_enabled is not None
            else self._thinking_disabled() or effort_is_disable
        )
        dominant = self._thinking_intent.dominant_concrete_field()
        concrete_priority = (
            self._thinking_intent.effort.priority
            if dominant == "effort"
            else self._thinking_intent.budget.priority
        )
        if (
            dominant in {"effort", "budget"}
            and not effort_is_disable
            and concrete_priority > self._thinking_intent.enabled.priority
        ):
            disabled = False
        elif dominant == "disabled":
            disabled = True
        if disabled and not mandatory:
            if not spec.supports_disable:
                return self._preserve_thinking_kwargs()
            return {"extra_body": {"enable_thinking": False}}
        extra_body: dict[str, Any] = {"enable_thinking": True}
        if self._supports_preserve_thinking():
            extra_body["preserve_thinking"] = True
        thinking_budget = self._selected_thinking_budget(dominant, effort)
        if thinking_budget is not None:
            extra_body["thinking_budget"] = thinking_budget
        return {"extra_body": extra_body}

    def _selected_thinking_budget(self, dominant: str | None, effort: str | None) -> int | None:
        budget = self._thinking_intent.budget.value
        if budget is None:
            budget = self._thinking_budget
        if budget is None:
            return None
        if dominant == "budget":
            return budget
        if dominant is None and effort in {None, "auto"}:
            return budget
        return None

    def _thinking_kwargs_for_context(self, context: ChatRequestContext) -> dict[str, Any]:
        if context.mandatory_thinking is None:
            context.mandatory_thinking = self._known_mandatory_thinking()
        kwargs = self._build_thinking_kwargs_with_mandatory(context.mandatory_thinking)
        if not context.mandatory_thinking_retry:
            return kwargs
        retry_kwargs = dict(kwargs)
        if retry_kwargs.get("reasoning_effort") == "none":
            retry_kwargs.pop("reasoning_effort", None)
        extra_body = dict(retry_kwargs.get("extra_body") or {})
        if extra_body.get("enable_thinking") is False:
            extra_body.pop("enable_thinking", None)
        has_positive_shape = bool(retry_kwargs.get("reasoning_effort")) or extra_body.get("enable_thinking") is True
        if not has_positive_shape:
            extra_body["enable_thinking"] = True
        if extra_body:
            retry_kwargs["extra_body"] = extra_body
        else:
            retry_kwargs.pop("extra_body", None)
        return retry_kwargs

    def _create_chat_request_context(
        self,
        *,
        streaming: bool,
        cache_policy: str = "default",
    ) -> ChatRequestContext:
        context = super()._create_chat_request_context(streaming=streaming, cache_policy=cache_policy)
        context.mandatory_thinking = self._known_mandatory_thinking()
        return context

    def _build_chat_completion_kwargs(
        self,
        messages,
        system,
        tools,
        max_tokens,
        context: ChatRequestContext,
    ) -> dict[str, Any]:
        kwargs = super()._build_chat_completion_kwargs(messages, system, tools, max_tokens, context)
        if kwargs.get("tool_choice") == "required" and _request_enables_thinking(kwargs):
            kwargs.pop("tool_choice", None)
        if not context.streaming:
            _remove_non_system_cache_markers(kwargs.get("messages"))
        return kwargs

    def _retry_request_context_after_error(
        self,
        error: Exception,
        sent_kwargs: dict[str, Any],
        context: ChatRequestContext,
    ) -> bool:
        if not _request_explicitly_disabled_thinking(sent_kwargs) or not _is_required_thinking_error(error):
            return False
        context.mandatory_thinking_retry = True
        context.mandatory_thinking = True
        self._learned_mandatory_thinking = True
        return True

    def _record_request_compatibility_retry(
        self,
        error: Exception,
        context: ChatRequestContext,
        *,
        operation_name: str,
        duration_ms: int,
    ) -> None:
        """Expose the consumed mandatory-thinking attempt to telemetry."""
        endpoint = getattr(self, "_base_url", None) or getattr(getattr(self, "_client", None), "base_url", None)
        identity_attrs: dict[str, str | bool] = {
            IacCodeAttr.PROVIDER_ADAPTER: self._ADAPTER_NAME,
            IacCodeAttr.OFFICIAL_ENDPOINT: official_dashscope_wire_provider_key(str(endpoint or "")) is not None,
        }
        model = sanitize_model_name(self._model)
        summary = public_exception_summary(error, max_chars=1000)
        failure = public_error(message=summary, error_type=type(error).__name__)
        scope_attrs = get_span_attributes()
        failure_attrs = {
            "provider": "dashscope",
            "model": model,
            "error_type": type(error).__name__,
            "duration_ms": duration_ms,
            "error_message": sanitize_error_message(failure.summary),
            "error_id": failure.error_id,
            "status": "compatibility_retry",
            "operation": operation_name,
            **identity_attrs,
            **scope_attrs,
        }
        retry_attrs = {
            "provider": "dashscope",
            "model": model,
            "attempt": 1,
            "error_type": type(error).__name__,
            "streaming": context.streaming,
            "reason": "mandatory_thinking_compatibility",
            "operation": operation_name,
            **identity_attrs,
            **scope_attrs,
        }
        metric_attrs = {
            "provider": "dashscope",
            "model": model,
            "status": "compatibility_retry",
            "error_type": type(error).__name__,
            **identity_attrs,
        }
        try:
            log_event(Events.API_REQUEST_FAILED, failure_attrs)
        except Exception:
            pass
        try:
            log_event(Events.API_REQUEST_RETRIED, retry_attrs)
        except Exception:
            pass
        try:
            add_metric(Metrics.API_REQUEST_COUNT, 1, metric_attrs)
        except Exception:
            pass

    def _extract_reasoning_text(self, message_or_delta: Any) -> str:
        value = message_or_delta
        sentinel = object()
        reasoning_content = getattr(value, "reasoning_content", sentinel)
        reasoning = getattr(value, "reasoning", sentinel)
        extra = getattr(value, "model_extra", None)
        if isinstance(extra, dict):
            if reasoning_content is sentinel:
                reasoning_content = extra.get("reasoning_content", sentinel)
            if reasoning is sentinel:
                reasoning = extra.get("reasoning", sentinel)
        if reasoning_content is sentinel or reasoning is sentinel:
            model_dump = getattr(value, "model_dump", None)
            if callable(model_dump):
                try:
                    dumped = model_dump(exclude_none=False)
                except TypeError:
                    dumped = model_dump()
                if isinstance(dumped, dict):
                    if reasoning_content is sentinel:
                        reasoning_content = dumped.get("reasoning_content", sentinel)
                    if reasoning is sentinel:
                        reasoning = dumped.get("reasoning", sentinel)
        if reasoning_content is sentinel or reasoning_content is None:
            selected = "" if reasoning is sentinel or reasoning is None else reasoning
        else:
            selected = reasoning_content
        return selected if isinstance(selected, str) else str(selected)


def _request_explicitly_disabled_thinking(kwargs: dict[str, Any]) -> bool:
    if kwargs.get("reasoning_effort") == "none":
        return True
    extra_body = kwargs.get("extra_body")
    return isinstance(extra_body, dict) and extra_body.get("enable_thinking") is False


def _request_enables_thinking(kwargs: dict[str, Any]) -> bool:
    effort = kwargs.get("reasoning_effort")
    if isinstance(effort, str) and effort not in {"", "none"}:
        return True
    extra_body = kwargs.get("extra_body")
    return isinstance(extra_body, dict) and extra_body.get("enable_thinking") is True


def _remove_non_system_cache_markers(messages: Any) -> None:
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict) or message.get("role") == "system":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict):
                part.pop("cache_control", None)


def _is_required_thinking_error(error: Exception) -> bool:
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status != 400:
        return False
    code = str(getattr(error, "code", "") or "").strip().lower()
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        code = str(body.get("code") or code).strip().lower()
    body_message = str(body.get("message") or "") if isinstance(body, dict) else ""
    message = f"{error} {body_message}".strip().lower()
    stable_code = code in {"invalidparameter", "invalid_parameter", "invalid_request_error"}
    stable_message = any(
        marker in message
        for marker in (
            "thinking must be enabled",
            "enable_thinking must be true",
            "does not support disabling thinking",
            "reasoning_effort cannot be none",
        )
    )
    stable_message = stable_message or (
        "enable_thinking" in message
        and re.search(r"(?:restricted to|must be)\s+true\b", message, re.IGNORECASE) is not None
    )
    return stable_message and (stable_code or not code)
