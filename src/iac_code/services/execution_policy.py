"""Optional process-local execution policy supplied by a trusted embedding host.

Ordinary product entrypoints install nothing. This module does not load plugins,
read environment variables, or import repository test infrastructure. Exceptions
from an installed policy propagate: policy failures must not allow execution.
"""

from __future__ import annotations

from typing import Any, Protocol


class ExecutionPolicyDeniedError(ValueError):
    """A policy rejection with a public, non-secret message."""


class ExecutionPolicy(Protocol):
    def enabled(self) -> bool: ...

    def execution_instructions(self, session: str, cwd: str) -> str: ...

    def filter_tools(self, tools: list[Any]) -> list[Any]: ...

    def input_schema(self, tool: str, schema: dict[str, Any]) -> dict[str, Any]: ...

    def deferred_call(self, tool: str, index: int) -> str | None: ...

    def require_runtime(self, available: bool) -> str | None: ...

    def check(self, tool: str, value: dict[str, Any], session: str, *, cwd: str = "") -> str | None: ...

    async def reserve_when_ready(
        self,
        value: dict[str, Any],
        session: str,
        credential: Any,
        *,
        read_only: bool,
        cwd: str = "",
        tool_use_id: str = "",
    ) -> Any: ...

    def finish(
        self,
        ticket: Any,
        response: Any,
        succeeded: bool,
        *,
        status: int | None = None,
        headers: Any = None,
    ) -> None: ...

    def on_web_startup(self) -> None: ...


_policy: ExecutionPolicy | None = None


def install_policy(policy: ExecutionPolicy) -> None:
    """Install once before starting the runtime; replacement is not permitted."""
    global _policy
    if _policy is not None and _policy is not policy:
        raise RuntimeError("An execution policy is already installed")
    _policy = policy


def enabled() -> bool:
    return _policy is not None and _policy.enabled()


def execution_instructions(session: str, cwd: str) -> str:
    return _policy.execution_instructions(session, cwd) if _policy is not None else ""


def filter_tools(tools: list[Any]) -> list[Any]:
    return _policy.filter_tools(tools) if _policy is not None else tools


def input_schema(tool: str, schema: dict[str, Any]) -> dict[str, Any]:
    return _policy.input_schema(tool, schema) if _policy is not None else schema


def deferred_call(tool: str, index: int) -> str | None:
    return _policy.deferred_call(tool, index) if _policy is not None else None


def require_runtime(available: bool) -> str | None:
    return _policy.require_runtime(available) if _policy is not None else None


def check(tool: str, value: dict[str, Any], session: str, *, cwd: str = "") -> str | None:
    return _policy.check(tool, value, session, cwd=cwd) if _policy is not None else None


async def reserve_when_ready(
    value: dict[str, Any],
    session: str,
    credential: Any,
    *,
    read_only: bool,
    cwd: str = "",
    tool_use_id: str = "",
) -> Any:
    if _policy is None:
        return None
    return await _policy.reserve_when_ready(
        value,
        session,
        credential,
        read_only=read_only,
        cwd=cwd,
        tool_use_id=tool_use_id,
    )


def finish(
    ticket: Any,
    response: Any,
    succeeded: bool,
    *,
    status: int | None = None,
    headers: Any = None,
) -> None:
    if _policy is not None:
        _policy.finish(ticket, response, succeeded, status=status, headers=headers)


def on_web_startup() -> None:
    if _policy is not None:
        _policy.on_web_startup()
