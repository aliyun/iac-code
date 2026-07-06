from __future__ import annotations

import os

A2A_EXTREME_PERFORMANCE_ENV = "IAC_CODE_A2A_EXTREME_PERFORMANCE"

_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


def a2a_extreme_performance_enabled() -> bool:
    raw = os.environ.get(A2A_EXTREME_PERFORMANCE_ENV)
    if raw is None or raw.strip() == "":
        return True
    return raw.strip().lower() not in _FALSE_VALUES
