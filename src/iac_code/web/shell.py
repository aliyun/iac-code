"""Local shell escape execution for the Web workbench."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable
from typing import Any, Protocol, cast

from iac_code.i18n import _
from iac_code.services.permissions.pipeline import check_tool_permission
from iac_code.tools.base import ToolContext, ToolRegistry, ToolResult
from iac_code.tools.tool_executor import ToolCallRequest, ToolExecutor
from iac_code.types.permissions import PermissionResult, ToolPermissionContext
from iac_code.types.stream_events import PermissionRequestEvent
from iac_code.web.session_manager import WebSession, WebSessionManager

SHELL_ESCAPE_TOOL_USE_ID = "shell-escape"


class ToolRegistryLike(Protocol):
    def get(self, name: str) -> Any | None: ...


ExecutorFactory = Callable[[ToolRegistryLike], Any]
PermissionContextFactory = Callable[[WebSession], ToolPermissionContext]


def _default_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_default_tools()
    return registry


def _default_executor(registry: ToolRegistryLike) -> ToolExecutor:
    return ToolExecutor(cast(ToolRegistry, registry))


def _default_permission_context(session: WebSession) -> ToolPermissionContext:
    return session.permission_context or ToolPermissionContext(cwd=session.cwd)


def _local_shell_start_payload(command: str, *, shell_use_id: str) -> dict[str, Any]:
    return {
        "shellUseId": shell_use_id,
        "toolUseId": shell_use_id,
        "command": command,
        "local": True,
        "entersAgentContext": False,
    }


def _local_shell_end_payload(
    command: str,
    *,
    shell_use_id: str,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
) -> dict[str, Any]:
    return {
        "shellUseId": shell_use_id,
        "toolUseId": shell_use_id,
        "command": command,
        "exitCode": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "local": True,
        "entersAgentContext": False,
    }


def _permission_request_payload(
    permission: PermissionResult,
    *,
    tool: Any,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    return {
        "toolName": "bash",
        "toolUseId": SHELL_ESCAPE_TOOL_USE_ID,
        "toolInput": tool_input,
        "message": permission.message or _("Allow Bash?"),
        "suggestions": permission.suggestions or [],
        "allowAlways": bool(getattr(tool, "supports_blanket_allow", False)),
    }


def _parse_tool_result(result: ToolResult) -> tuple[int, str, str]:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    exit_code = _metadata_int(metadata, "exitCode", "exit_code")
    stdout = _metadata_str(metadata, "stdout")
    stderr = _metadata_str(metadata, "stderr")
    if exit_code is not None or stdout is not None or stderr is not None:
        return (
            exit_code if exit_code is not None else (1 if result.is_error else 0),
            stdout or "",
            stderr or "",
        )

    content = result.content or ""
    inferred_exit_code = 1 if result.is_error else 0
    exit_match = re.search(r"(?:^|\n)Exit code: (-?\d+)\s*$", content)
    if exit_match is not None:
        inferred_exit_code = int(exit_match.group(1))
        body = content[: exit_match.start()].rstrip("\n")
    else:
        body = content.strip("\n")

    parsed_stdout = ""
    parsed_stderr = ""
    if body.startswith("STDOUT:\n"):
        remainder = body[len("STDOUT:\n") :]
        stderr_marker = "\nSTDERR:\n"
        if stderr_marker in remainder:
            parsed_stdout, parsed_stderr = remainder.split(stderr_marker, 1)
        else:
            parsed_stdout = remainder
    elif body.startswith("STDERR:\n"):
        parsed_stderr = body[len("STDERR:\n") :]
    elif result.is_error:
        parsed_stderr = body
    else:
        parsed_stdout = body

    return inferred_exit_code, parsed_stdout.rstrip("\n"), parsed_stderr.rstrip("\n")


def _metadata_int(metadata: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                continue
    return None


def _metadata_str(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    return value if isinstance(value, str) else None


class WebShellEscapeRunner:
    """Check permission, execute a local bash escape, and publish Web events."""

    def __init__(
        self,
        manager: WebSessionManager,
        *,
        tool_registry: ToolRegistryLike | None = None,
        executor_factory: ExecutorFactory | None = None,
        permission_context_factory: PermissionContextFactory | None = None,
    ) -> None:
        self.manager = manager
        self.tool_registry: ToolRegistryLike = tool_registry or _default_tool_registry()
        self.executor_factory: ExecutorFactory = executor_factory or _default_executor
        self.permission_context_factory: PermissionContextFactory = (
            permission_context_factory or _default_permission_context
        )

    async def run(self, session: WebSession, command: str) -> dict[str, Any]:
        """Execute a local shell escape and return the local.shell.end payload."""
        shell_use_id = "local-shell-{}".format(uuid.uuid4().hex)
        await session.events.publish(
            "local.shell.start",
            _local_shell_start_payload(command, shell_use_id=shell_use_id),
        )
        end_published = False

        async def publish_end(*, exit_code: int, stdout: str = "", stderr: str = "") -> dict[str, Any]:
            nonlocal end_published
            payload = await self._publish_end(
                session,
                command,
                shell_use_id=shell_use_id,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
            )
            end_published = True
            return payload

        try:
            tool = self.tool_registry.get("bash")
            if tool is None:
                return await publish_end(
                    exit_code=127,
                    stderr="Shell command support is unavailable.",
                )

            tool_input = {"command": command}
            permission_context = (
                self.manager.ensure_permission_context(session)
                if self.permission_context_factory is _default_permission_context
                else self.permission_context_factory(session)
            )
            permission = await check_tool_permission(tool, tool_input, permission_context)
            if permission.behavior == "deny":
                return await publish_end(
                    exit_code=1,
                    stderr=permission.message if permission.message else "Permission denied.",
                )
            if permission.behavior == "ask":
                allowed = await self._ask_permission(session, permission, tool=tool, tool_input=tool_input)
                if not allowed:
                    return await publish_end(
                        exit_code=1,
                        stderr="Permission denied.",
                    )

            executor = self.executor_factory(self.tool_registry)
            try:
                results = await executor.execute_batch(
                    [ToolCallRequest(id=SHELL_ESCAPE_TOOL_USE_ID, name="bash", input=tool_input)],
                    ToolContext(cwd=session.cwd),
                )
                result = results[0] if results else ToolResult.error("Shell command did not return a result.")
            except Exception as exc:
                result = ToolResult.error(str(exc)[:500])
            exit_code, stdout, stderr = _parse_tool_result(result)
            return await publish_end(exit_code=exit_code, stdout=stdout, stderr=stderr)
        except asyncio.CancelledError:
            if not end_published:
                await asyncio.shield(
                    publish_end(
                        exit_code=130,
                        stderr="Shell command canceled.",
                    )
                )
            raise
        except Exception as exc:
            if not end_published:
                return await publish_end(exit_code=1, stderr=str(exc)[:500])
            raise

    async def _ask_permission(
        self,
        session: WebSession,
        permission: PermissionResult,
        *,
        tool: Any,
        tool_input: dict[str, Any],
    ) -> bool:
        future = asyncio.get_running_loop().create_future()
        permission_context = self.manager.ensure_permission_context(session)
        audit_event = PermissionRequestEvent(
            tool_name="bash",
            tool_input=tool_input,
            tool_use_id=SHELL_ESCAPE_TOOL_USE_ID,
            response_future=future,
            permission_result=permission,
            audit_context={
                "session_id": session.session_id,
                "cwd": session.cwd,
                "settings": permission_context.audit_settings,
                "metadata": permission.audit,
            },
        )
        request_id = self.manager.add_permission_request(
            session,
            _permission_request_payload(permission, tool=tool, tool_input=tool_input),
            future=future,
            audit_event=audit_event,
        )
        try:
            return bool(await asyncio.shield(future))
        except asyncio.CancelledError:
            self.manager.cancel_permission_request(request_id, session_id=session.session_id)
            raise
        finally:
            self.manager.discard_permission_request(request_id, session_id=session.session_id)

    async def _publish_end(
        self,
        session: WebSession,
        command: str,
        *,
        shell_use_id: str,
        exit_code: int,
        stdout: str = "",
        stderr: str = "",
    ) -> dict[str, Any]:
        payload = _local_shell_end_payload(
            command,
            shell_use_id=shell_use_id,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        )
        await session.events.publish("local.shell.end", payload)
        return payload
