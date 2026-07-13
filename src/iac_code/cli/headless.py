"""Headless (non-interactive) runner for iac-code.

Executes a single prompt to completion without user interaction.
Tool permissions are auto-approved. Output is written via format-specific writers.

Exit codes:
    0 — normal completion
    1 — LLM / network error
    2 — reached max-turns limit
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import IO, Any

from loguru import logger

from iac_code.cli.output_formats import OutputFormat, create_writer
from iac_code.i18n import _
from iac_code.mcp.progress import format_mcp_progress_text
from iac_code.mcp.prompt_dispatch import mcp_prompt_command_stream
from iac_code.providers.manager import ProviderNotConfiguredError
from iac_code.services.permissions.audit import emit_auto_permission_audit, is_aliyun_api_non_read_only_permission_event
from iac_code.services.session_backup import BackupReason, SessionBackupService
from iac_code.services.telemetry import graceful_shutdown, log_event
from iac_code.services.telemetry.names import Events
from iac_code.types.stream_events import (
    ErrorEvent,
    MCPProgressEvent,
    MessageEndEvent,
    PermissionRequestEvent,
    StackInstancesProgressEvent,
    StackProgressEvent,
    SubAgentToolEvent,
    SubPipelineStreamEvent,
    ToolResultEvent,
    ToolUseStartEvent,
)
from iac_code.utils.background_housekeeping import start_background_housekeeping
from iac_code.utils.public_errors import public_error_from_exception, sanitize_public_text

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_MAX_TURNS = 2
__all__ = ["HeadlessRunner", "logger"]


class _ProgressWriter:
    """Write human-readable headless progress to stderr."""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream

    def handle(self, event: Any) -> None:
        line: str | None = None
        if isinstance(event, ToolUseStartEvent):
            line = _("Tool started: {}").format(event.name)
        elif isinstance(event, ToolResultEvent):
            if event.is_error:
                line = _("Tool failed: {}").format(event.tool_name)
            else:
                line = _("Tool finished: {}").format(event.tool_name)
        elif isinstance(event, SubAgentToolEvent):
            if event.is_done:
                if event.is_error:
                    line = _("Child tool failed: {}").format(event.child_tool_name)
                else:
                    line = _("Child tool finished: {}").format(event.child_tool_name)
            else:
                line = _("Child tool started: {}").format(event.child_tool_name)
        elif isinstance(event, StackProgressEvent):
            line = _("Stack {}: {} ({:.1f}%)").format(
                event.stack_name,
                event.status,
                event.progress_percentage,
            )
        elif isinstance(event, StackInstancesProgressEvent):
            line = _("Stack group {}: {} ({}%)").format(
                event.stack_group_name,
                event.status,
                event.progress_percentage,
            )
        elif isinstance(event, MCPProgressEvent):
            line = _format_mcp_progress(event)

        if line is not None:
            self._stream.write(line + "\n")
            self._stream.flush()


def _format_mcp_progress(event: MCPProgressEvent) -> str:
    return format_mcp_progress_text(event)


async def _headless_mcp_elicitation_handler(server_name: str, params: dict[str, Any]) -> dict[str, Any]:
    _ = server_name, params
    return {"action": "cancel"}


def _permission_request_event(event: Any) -> PermissionRequestEvent | None:
    if isinstance(event, PermissionRequestEvent):
        return event
    if isinstance(event, SubPipelineStreamEvent):
        return _permission_request_event(event.inner)
    return None


class HeadlessRunner:
    """Run a single prompt headlessly, auto-approving all permission requests."""

    def __init__(
        self,
        model: str,
        output_format: OutputFormat = OutputFormat.TEXT,
        max_turns: int = 100,
        output_stream: IO[str] | None = None,
        cli_allowed_tools: list[str] | None = None,
        cli_disallowed_tools: list[str] | None = None,
        cli_permission_mode: str | None = None,
        verbose: bool = False,
        progress_stream: IO[str] | None = None,
        resume_session_id: str | bool | None = None,
        thinking_enabled: bool | None = True,
    ) -> None:
        self._model = model
        self._output_format = output_format
        self._max_turns = max_turns
        self._output_stream = output_stream or sys.stdout
        self._cli_allowed_tools = cli_allowed_tools
        self._cli_disallowed_tools = cli_disallowed_tools
        self._cli_permission_mode = cli_permission_mode
        self._verbose = verbose
        self._progress_stream = progress_stream or sys.stderr
        self._resume_session_id = resume_session_id
        self._thinking_enabled = thinking_enabled
        self._mcp_config_warnings: list[Any] = []
        self._mcp_warnings_printed_count = 0
        self._runtime: Any | None = None

    def _print_provider_not_configured(self, exc: Exception) -> None:
        logger.error("Provider not configured: {}", exc)
        hint = _(
            "\n"
            "  {error}\n"
            "\n"
            "  Fix: run  iac-code  then type /auth\n"
            "   or: set  IAC_CODE_API_KEY=<your-key>\n"
            "  Docs: https://aliyun.github.io/iac-code/docs/configuration/authentication\n"
        ).format(error=sanitize_public_text(exc))
        print(hint, file=sys.stderr)

    def _print_unexpected_error(self, exc: Exception) -> None:
        logger.error("Headless execution failed: {}", exc)
        print(_("Error: {error}").format(error=sanitize_public_text(exc)), file=sys.stderr)

    def _record_structured_error(self, writer: Any, exc: Exception) -> None:
        if self._output_format != OutputFormat.TEXT:
            failure = public_error_from_exception(exc)
            writer.handle(ErrorEvent(error=failure.summary, is_retryable=False, error_id=failure.error_id))

    def _create_agent_loop(self) -> Any:
        """Create and return a fully configured AgentLoop."""
        from iac_code.providers.request_policy import ProviderRequestPolicy
        from iac_code.services.agent_factory import AgentFactoryOptions, create_agent_runtime

        cwd = os.getcwd()
        session_id, resume_messages = self._resolve_resume_options()
        request_policy_override = (
            ProviderRequestPolicy(thinking_enabled=self._thinking_enabled)
            if self._thinking_enabled is not None
            else None
        )

        runtime = create_agent_runtime(
            AgentFactoryOptions(
                model=self._model,
                session_id=session_id,
                cwd=cwd,
                max_turns=self._max_turns,
                request_policy_override=request_policy_override,
                cli_allowed_tools=self._cli_allowed_tools,
                cli_disallowed_tools=self._cli_disallowed_tools,
                cli_permission_mode=self._cli_permission_mode,
                resume_messages=resume_messages,
                mcp_elicitation_handler=_headless_mcp_elicitation_handler,
            )
        )
        self._runtime = runtime
        self._mcp_config_warnings = runtime.mcp_config_warnings if runtime.mcp_config_warnings is not None else []
        return runtime.agent_loop

    def _resolve_resume_options(self) -> tuple[str | None, list[Any] | None]:
        resume = self._resume_session_id
        if resume is None:
            return None, None

        from iac_code.services.session_backup import SessionBackupService
        from iac_code.services.session_index import SessionIndex
        from iac_code.services.session_resolver import ResolutionStatus, resolve_session_argument
        from iac_code.services.session_storage import SessionStorage
        from iac_code.utils.project_paths import same_project_path

        cwd = os.getcwd()
        storage = SessionStorage()
        session_id: str | None = None
        if resume is True:
            latest = storage.get_latest_session_anywhere()
            if latest is None:
                return None, None
            latest_cwd, latest_session_id = latest
            if latest_cwd and not same_project_path(latest_cwd, cwd):
                raise ValueError(_("Session not found: {session_id}").format(session_id=latest_session_id))
            session_id = latest_session_id
        elif isinstance(resume, str) and resume:
            index = SessionIndex()
            resolution = resolve_session_argument(index, cwd, resume)
            if resolution.status == ResolutionStatus.NOT_FOUND and SessionBackupService.is_safe_session_id(resume):
                SessionBackupService(session_storage=storage).restore_session(cwd, resume)
                resolution = resolve_session_argument(index, cwd, resume)
            if resolution.status != ResolutionStatus.FOUND or resolution.entry is None:
                raise ValueError(_("Session not found: {session_id}").format(session_id=resume))
            if resolution.entry.cwd and not same_project_path(resolution.entry.cwd, cwd):
                raise ValueError(_("Session not found: {session_id}").format(session_id=resume))
            session_id = resolution.entry.session_id

        if not session_id:
            return None, None
        loaded = storage.load(cwd, session_id)
        repaired = SessionStorage.repair_interrupted(loaded) if loaded else []
        return session_id, repaired or None

    def _print_mcp_config_warnings(self) -> None:
        warnings = list(self._mcp_config_warnings or [])
        for warning in warnings[self._mcp_warnings_printed_count :]:
            self._progress_stream.write(
                _("MCP warning: {message}\n").format(message=getattr(warning, "message", warning))
            )
            self._progress_stream.flush()
        self._mcp_warnings_printed_count = len(warnings)

    async def _backup_normal_turn_end(self, agent_loop: Any) -> None:
        cwd = getattr(agent_loop, "_cwd", None)
        session_id = getattr(agent_loop, "_session_id", None)
        session_storage = getattr(agent_loop, "_session_storage", None)
        if not isinstance(cwd, str) or not isinstance(session_id, str) or session_storage is None:
            return
        try:
            result = await asyncio.to_thread(
                SessionBackupService(session_storage=session_storage).backup_session,
                cwd,
                session_id,
                reason=BackupReason.NORMAL_TURN_END,
                critical=False,
            )
            if getattr(result, "enabled", False) and not getattr(result, "succeeded", True):
                logger.warning(
                    "Headless session backup failed (reason={}, retry_count={}): {}",
                    BackupReason.NORMAL_TURN_END.value,
                    getattr(result, "retry_count", 0),
                    getattr(result, "error", None) or "unknown",
                )
        except Exception as exc:
            logger.warning(
                "Headless session backup failed (reason={}, retry_count={}, error_type={})",
                BackupReason.NORMAL_TURN_END.value,
                getattr(exc, "retry_count", 0),
                type(exc).__name__,
            )

    async def run(self, prompt: str) -> int:
        """Execute a single prompt to completion and return an exit code."""
        started = time.monotonic()
        start_background_housekeeping()

        writer = create_writer(self._output_format, self._output_stream)
        progress_writer = _ProgressWriter(self._progress_stream) if self._verbose else None

        has_error = False
        hit_max_turns = False
        agent_loop = None

        try:
            agent_loop = self._create_agent_loop()
            self._print_mcp_config_warnings()
            runtime = self._runtime
            stream = await mcp_prompt_command_stream(
                agent_loop=agent_loop,
                commands=getattr(runtime, "command_registry", None),
                prompt=prompt,
                session_id=str(getattr(runtime, "session_id", "") or ""),
            )
            if stream is None:
                stream = agent_loop.run_streaming(prompt)
            async for event in stream:
                self._print_mcp_config_warnings()
                permission_event = _permission_request_event(event)
                if permission_event is not None:
                    if permission_event.response_future is not None and not permission_event.response_future.done():
                        approved = not is_aliyun_api_non_read_only_permission_event(permission_event)
                        audit_ok = emit_auto_permission_audit(
                            permission_event,
                            decision="allow" if approved else "deny",
                            scope="auto_approve" if approved else "auto_deny",
                            source="headless_auto_approve" if approved else "headless_auto_deny",
                        )
                        if approved and not audit_ok:
                            approved = False
                        permission_event.response_future.set_result(approved)
                    continue

                if isinstance(event, ErrorEvent):
                    has_error = True

                if isinstance(event, MessageEndEvent) and event.stop_reason == "max_turns":
                    hit_max_turns = True

                if progress_writer is not None:
                    progress_writer.handle(event)

                writer.handle(event)
            if not has_error:
                await self._backup_normal_turn_end(agent_loop)
        except ProviderNotConfiguredError as exc:
            self._print_provider_not_configured(exc)
            self._record_structured_error(writer, exc)
            has_error = True
        except Exception as exc:
            self._print_unexpected_error(exc)
            self._record_structured_error(writer, exc)
            has_error = True
        finally:
            runtime = self._runtime
            self._runtime = None
            close = getattr(runtime, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    logger.debug("Headless runtime close failed", exc_info=True)

        writer.finalize()

        # Emit session exit event and gracefully shutdown telemetry
        log_event(
            Events.SESSION_EXITED,
            {
                "reason": "normal" if not has_error else "error",
                "duration_s": int(time.monotonic() - started),
            },
        )
        graceful_shutdown()

        if has_error:
            return EXIT_ERROR
        if hit_max_turns:
            return EXIT_MAX_TURNS
        return EXIT_OK
