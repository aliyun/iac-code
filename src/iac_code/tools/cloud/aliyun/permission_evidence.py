"""Allowlisted, non-secret permission evidence returned by Alibaba Cloud."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import cast

_RAM_ACTION = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:[A-Za-z][A-Za-z0-9]*$")
_DETAIL_VALUE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")


def access_denied_detail(body: object) -> dict[str, str]:
    """Keep explicit authorization evidence without principal IDs or encoded diagnostics."""
    if not isinstance(body, Mapping):
        return {}
    mapping = cast(Mapping[str, object], body)
    detail = mapping.get("AccessDeniedDetail") or mapping.get("accessDeniedDetail")
    if not isinstance(detail, Mapping):
        return {}
    result = {}
    for key in ("AuthAction", "NoPermissionType", "PolicyType"):
        value = cast(Mapping[str, object], detail).get(key)
        pattern = _RAM_ACTION if key == "AuthAction" else _DETAIL_VALUE
        if isinstance(value, str) and len(value) <= 128 and pattern.fullmatch(value):
            result[key] = value
    return result
