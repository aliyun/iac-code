"""Pre-call hooks for AliyunApi with decorator registration and auto-discovery."""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Any, Callable

from iac_code.tools.base import ToolResult

HookFn = Callable[[str, str, dict[str, Any]], ToolResult | None]

_hooks: dict[tuple[str, str], list[HookFn]] = {}
_loaded = False


def before_call(product: str, action: str | list[str]):
    """Decorator to register a pre-call hook for (product, action).

    action can be a single string or a list of strings.
    """

    def decorator(fn: HookFn) -> HookFn:
        actions = action if isinstance(action, list) else [action]
        for a in actions:
            _hooks.setdefault((product, a), []).append(fn)
        return fn

    return decorator


def run_hooks(product: str, action: str, params: dict[str, Any]) -> ToolResult | None:
    """Execute registered hooks for (product, action). Returns first non-None result."""
    _ensure_loaded()
    for hook in _hooks.get((product, action), []):
        result = hook(product, action, params)
        if result is not None:
            return result
    return None


def _ensure_loaded() -> None:
    """Auto-import all modules under hooks/ directory once."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    hooks_dir = Path(__file__).parent / "hooks"
    if not hooks_dir.is_dir():
        return
    package = "iac_code.tools.cloud.aliyun.hooks"
    for info in pkgutil.iter_modules([str(hooks_dir)]):
        importlib.import_module(f"{package}.{info.name}")
