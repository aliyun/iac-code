"""Safe provider request logging helpers."""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

_LOG_PREFIX = "[provider-request-policy]"
_REQUEST_KEYS: tuple[str, ...] = ("stream", "max_tokens", "max_completion_tokens", "reasoning_effort")
_EXTRA_BODY_KEYS: tuple[str, ...] = ("enable_thinking", "thinking_budget", "thinking")
_THINKING_KEYS: tuple[str, ...] = ("type", "budget_tokens")


class ProviderRequestLogSanitizer:
    """Build a safe, low-cardinality log payload for provider SDK calls."""

    def __init__(self, provider_key: str, model: str, operation: str, kwargs: dict[str, Any]) -> None:
        self._provider_key = provider_key
        self._model = model
        self._operation = operation
        self._kwargs = kwargs

    def payload(self) -> dict[str, Any]:
        request: dict[str, Any] = {}
        for key in _REQUEST_KEYS:
            if key in self._kwargs:
                request[key] = self._json_scalar(self._kwargs[key])

        extra_body = self._sanitize_extra_body(self._kwargs.get("extra_body"))
        if extra_body:
            request["extra_body"] = extra_body

        thinking = self._sanitize_thinking(self._kwargs.get("thinking"))
        if thinking:
            request["thinking"] = thinking

        return {
            "provider": self._provider_key,
            "model": self._model,
            "operation": self._operation,
            "request": request,
        }

    @classmethod
    def _sanitize_extra_body(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        sanitized: dict[str, Any] = {}
        for key in _EXTRA_BODY_KEYS:
            if key not in value:
                continue
            if key == "thinking":
                thinking = cls._sanitize_thinking(value.get(key))
                if thinking:
                    sanitized[key] = thinking
                continue
            sanitized[key] = cls._json_scalar(value[key])
        return sanitized

    @classmethod
    def _sanitize_thinking(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        sanitized: dict[str, Any] = {}
        for key in _THINKING_KEYS:
            if key in value:
                sanitized[key] = cls._json_scalar(value[key])
        return sanitized

    @staticmethod
    def _json_scalar(value: Any) -> str | int | float | bool | None:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)


def build_provider_request_policy_log(
    provider_key: str,
    model: str,
    operation: str,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Return the safe provider request policy payload without writing logs."""
    return ProviderRequestLogSanitizer(provider_key, model, operation, kwargs).payload()


def log_provider_request_policy(
    provider_key: str,
    model: str,
    operation: str,
    kwargs: dict[str, Any],
) -> None:
    """Log a sanitized SDK call payload for request-policy diagnostics."""
    payload = build_provider_request_policy_log(provider_key, model, operation, kwargs)
    logger.info("{} {}", _LOG_PREFIX, json.dumps(payload, ensure_ascii=False, sort_keys=True))
