"""Request-local dimensions propagated to nested telemetry spans."""

from __future__ import annotations

import contextvars
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from iac_code.services.telemetry.names import IacCodeAttr, PipelineAttr

_ALLOWED_KEYS = frozenset(
    {
        IacCodeAttr.MODE,
        PipelineAttr.NAME,
        PipelineAttr.RUN_ID,
        PipelineAttr.STEP_ID,
        PipelineAttr.PARENT_STEP_ID,
        PipelineAttr.SUB_PIPELINE_NAME,
        PipelineAttr.SUB_PIPELINE_ID,
        PipelineAttr.SUB_STEP_ID,
        PipelineAttr.CANDIDATE_INDEX,
    }
)
_MAX_STRING_LENGTH = 256
_span_attributes: contextvars.ContextVar[dict[str, str | int]] = contextvars.ContextVar(
    "iac_code_telemetry_span_attributes",
    default={},
)


def normalize_span_attributes(attributes: Mapping[str, Any] | None) -> dict[str, str | int]:
    """Keep only bounded, non-content values accepted by nested spans."""
    normalized: dict[str, str | int] = {}
    for key, value in (attributes or {}).items():
        if key not in _ALLOWED_KEYS or isinstance(value, bool):
            continue
        if isinstance(value, int):
            normalized[key] = value
        elif isinstance(value, str) and value and len(value) <= _MAX_STRING_LENGTH:
            normalized[key] = value
    return normalized


def get_span_attributes() -> dict[str, str | int]:
    return dict(_span_attributes.get())


@contextmanager
def use_span_attributes(attributes: Mapping[str, Any] | None) -> Iterator[None]:
    merged = get_span_attributes()
    merged.update(normalize_span_attributes(attributes))
    token = _span_attributes.set(merged)
    try:
        yield
    finally:
        _span_attributes.reset(token)


@contextmanager
def replace_span_attributes(attributes: Mapping[str, Any] | None) -> Iterator[None]:
    """Temporarily replace, rather than merge, request-local dimensions."""
    token = _span_attributes.set(normalize_span_attributes(attributes))
    try:
        yield
    finally:
        _span_attributes.reset(token)
