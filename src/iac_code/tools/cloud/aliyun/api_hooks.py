"""Parameter normalization and pre-call hooks with registration and auto-discovery."""

from __future__ import annotations

import copy
import importlib
import inspect
import pkgutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from iac_code.tools.base import ToolContext, ToolResult
from iac_code.tools.cloud.aliyun.ros_validation.outcome import RosPreflightOutcome

if TYPE_CHECKING:
    from iac_code.tools.cloud.aliyun.api_contract import CanonicalWireContract

HookFn = Callable[..., ToolResult | RosPreflightOutcome | None]
ParameterNormalizer = Callable[..., None]

_hooks: dict[tuple[str, str], list[HookFn]] = {}
_parameter_normalizers: dict[tuple[str, str], list[ParameterNormalizer]] = {}
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


def parameter_normalizer(product: str, action: str | list[str]):
    """Register a pure parameter adapter, separate from blocking pre-call hooks."""

    def decorator(fn: ParameterNormalizer) -> ParameterNormalizer:
        actions = action if isinstance(action, list) else [action]
        for api_action in actions:
            _parameter_normalizers.setdefault((product.casefold(), api_action), []).append(fn)
        return fn

    return decorator


def normalize_parameters(
    product: str,
    action: str,
    params: dict[str, Any],
    *,
    contract: CanonicalWireContract | None = None,
) -> None:
    """Normalize copied params before permission shapes and again before encoding.

    Adapters must be deterministic and idempotent, with no external operations.
    Structural normalization must not depend on metadata. Contract-dependent changes
    are limited to wire-equivalent scalar coercion before schema validation.
    """
    _ensure_loaded()
    for normalizer in _parameter_normalizers.get((product.casefold(), action), []):
        if "contract" in inspect.signature(normalizer).parameters:
            normalizer(product, action, params, contract=contract)
        else:
            normalizer(product, action, params)


def run_hooks(
    product: str,
    action: str,
    params: dict[str, Any],
    *,
    context: ToolContext | None = None,
    read_only: bool = False,
) -> ToolResult | None:
    """Execute hooks and return the first blocking result.

    ``read_only`` gives the hook chain an isolated copy.  Stage-zero callers
    use it so no current or future hook can mutate the permission-bound input.
    """

    _ensure_loaded()
    hook_params = copy.deepcopy(params) if read_only else params
    for hook in _hooks.get((product, action), []):
        if "context" in inspect.signature(hook).parameters:
            result = hook(product, action, hook_params, context=context)
        else:
            result = hook(product, action, hook_params)
        if isinstance(result, RosPreflightOutcome):
            if context is not None:
                context.ros_preflight_outcome = result
            if result.blocking_result is not None:
                return result.blocking_result
            continue
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
