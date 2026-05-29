from __future__ import annotations

from collections.abc import Iterable
from enum import Enum
from typing import Any


class A2AExposureType(str, Enum):
    RAW_THINKING = "raw_thinking"
    TOOL_TRACE = "tool_trace"


_EXPOSURE_TYPE_ORDER = (
    A2AExposureType.RAW_THINKING,
    A2AExposureType.TOOL_TRACE,
)
_SUPPORTED_TYPE_NAMES = ", ".join(item.value.replace("_", "-") for item in _EXPOSURE_TYPE_ORDER)
_DISABLED_ALIASES = frozenset({"", "none", "off", "false", "0"})
_ALL_ALIASES = frozenset({"all", "*"})
DEFAULT_A2A_EXPOSURE_TYPES = frozenset({A2AExposureType.TOOL_TRACE})


def normalize_a2a_exposure_types(value: Any) -> frozenset[A2AExposureType]:
    if value is None:
        return DEFAULT_A2A_EXPOSURE_TYPES

    tokens = list(_iter_exposure_tokens(value))
    if not tokens:
        return frozenset()
    if any(token in _ALL_ALIASES for token in tokens):
        return frozenset(_EXPOSURE_TYPE_ORDER)
    tokens = [token for token in tokens if token not in _DISABLED_ALIASES]
    return frozenset(_exposure_type_from_token(token) for token in tokens)


def format_a2a_exposure_types(value: Any) -> list[str]:
    enabled = normalize_a2a_exposure_types(value)
    return [item.value for item in _EXPOSURE_TYPE_ORDER if item in enabled]


def _iter_exposure_tokens(value: Any) -> Iterable[str]:
    if isinstance(value, A2AExposureType):
        yield value.value
        return
    if isinstance(value, str):
        for item in value.replace(";", ",").split(","):
            token = item.strip().lower().replace("-", "_")
            if token:
                yield token
        return
    if isinstance(value, Iterable) and not isinstance(value, dict):
        for item in value:
            yield from _iter_exposure_tokens(item)
        return
    raise ValueError(
        f"A2A thinking exposure must be a string or list of strings. Supported values: {_SUPPORTED_TYPE_NAMES}."
    )


def _exposure_type_from_token(token: str) -> A2AExposureType:
    try:
        return A2AExposureType(token)
    except ValueError as exc:
        raise ValueError(
            f"Unsupported A2A thinking exposure type {token!r}. Supported values: {_SUPPORTED_TYPE_NAMES}."
        ) from exc
