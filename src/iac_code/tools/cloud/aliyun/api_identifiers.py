"""Shared validation rules for Alibaba Cloud OpenAPI identifiers."""

from __future__ import annotations

import re

SAFE_API_VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
SAFE_API_VERSION = re.compile(SAFE_API_VERSION_PATTERN)


def is_safe_api_version(value: object) -> bool:
    """Return whether value is a safe, opaque OpenAPI version path segment."""

    return isinstance(value, str) and SAFE_API_VERSION.fullmatch(value) is not None
