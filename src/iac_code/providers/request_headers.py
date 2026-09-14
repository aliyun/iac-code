"""Request-local HTTP headers for LLM provider calls."""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator, Mapping

_request_headers: contextvars.ContextVar[tuple[tuple[str, str], ...]] = contextvars.ContextVar(
    "iac_code_provider_request_headers",
    default=(),
)


def get_provider_request_headers() -> dict[str, str]:
    """Return a copy of the HTTP headers scoped to the current provider request."""
    return dict(_request_headers.get())


@contextlib.contextmanager
def use_provider_request_headers(headers: Mapping[str, str]) -> Iterator[None]:
    """Apply HTTP headers to provider calls made in the current async context."""
    token = _request_headers.set(tuple(headers.items()))
    try:
        yield
    finally:
        _request_headers.reset(token)


def merge_provider_request_headers(
    base: Mapping[str, str] | None,
    overrides: Mapping[str, str] | None,
) -> dict[str, str]:
    """Merge headers case-insensitively, with ``overrides`` taking precedence."""
    merged: dict[str, str] = {}
    names_by_lowercase: dict[str, str] = {}
    for source in (base, overrides):
        if not source:
            continue
        for name, value in source.items():
            normalized_name = name.lower()
            previous_name = names_by_lowercase.get(normalized_name)
            if previous_name is not None:
                merged.pop(previous_name, None)
            merged[name] = value
            names_by_lowercase[normalized_name] = name
    return merged
