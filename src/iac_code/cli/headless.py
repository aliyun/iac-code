"""Headless (non-interactive) runner for iac-code.

Executes a single prompt to completion without user interaction.
Tool permissions are auto-approved. Output is written via format-specific writers.

Exit codes:
    0 — normal completion
    1 — LLM / network error
    2 — reached max-turns limit
"""

from __future__ import annotations

import sys
import time
from typing import IO, Any

from loguru import logger

from iac_code.cli.output_formats import OutputFormat, create_writer
from iac_code.i18n import _
from iac_code.providers.manager import ProviderNotConfiguredError
from iac_code.types.stream_events import (
    ErrorEvent,
    MessageEndEvent,
    PermissionRequestEvent,
    StackInstancesProgressEvent,
    StackProgressEvent,
    SubAgentToolEvent,
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

        if line is not None:
            self._stream.write(line + "\n")
            self._stream.flush()


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
        from iac_code.services.agent_factory import AgentFactoryOptions, create_agent_runtime

        runtime = create_agent_runtime(
            AgentFactoryOptions(
                model=self._model,
                max_turns=self._max_turns,
                cli_allowed_tools=self._cli_allowed_tools,
                cli_disallowed_tools=self._cli_disallowed_tools,
                cli_permission_mode=self._cli_permission_mode,
            )
        )
        return runtime.agent_loop

    async def run(self, prompt: str) -> int:
        """Execute a single prompt to completion and return an exit code."""
        from iac_code.services.telemetry import graceful_shutdown, log_event
        from iac_code.services.telemetry.names import Events

        started = time.monotonic()
        start_background_housekeeping()

        writer = create_writer(self._output_format, self._output_stream)
        progress_writer = _ProgressWriter(self._progress_stream) if self._verbose else None

        has_error = False
        hit_max_turns = False

        try:
            agent_loop = self._create_agent_loop()
            async for event in agent_loop.run_streaming(prompt):
                if isinstance(event, PermissionRequestEvent):
                    if event.response_future is not None:
                        from iac_code.services.telemetry import log_event
                        from iac_code.services.telemetry.names import Events

                        log_event(
                            Events.TOOL_USE_GRANTED_IN_PROMPT,
                            {
                                "tool_name": event.tool_name,
                                "scope": "once",
                            },
                        )
                        event.response_future.set_result(True)
                    continue

                if isinstance(event, ErrorEvent):
                    has_error = True

                if isinstance(event, MessageEndEvent) and event.stop_reason == "max_turns":
                    hit_max_turns = True

                if progress_writer is not None:
                    progress_writer.handle(event)

                writer.handle(event)
        except ProviderNotConfiguredError as exc:
            self._print_provider_not_configured(exc)
            self._record_structured_error(writer, exc)
            has_error = True
        except Exception as exc:
            self._print_unexpected_error(exc)
            self._record_structured_error(writer, exc)
            has_error = True

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
