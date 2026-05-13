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
from iac_code.types.stream_events import (
    ErrorEvent,
    MessageEndEvent,
    PermissionRequestEvent,
)
from iac_code.utils.background_housekeeping import start_background_housekeeping

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_MAX_TURNS = 2
__all__ = ["HeadlessRunner", "logger"]


class HeadlessRunner:
    """Run a single prompt headlessly, auto-approving all permission requests."""

    def __init__(
        self,
        model: str,
        output_format: OutputFormat = OutputFormat.TEXT,
        max_turns: int = 100,
        output_stream: IO[str] | None = None,
    ) -> None:
        self._model = model
        self._output_format = output_format
        self._max_turns = max_turns
        self._output_stream = output_stream or sys.stdout

    def _create_agent_loop(self) -> Any:
        """Create and return a fully configured AgentLoop."""
        from iac_code.services.agent_factory import AgentFactoryOptions, create_agent_runtime

        runtime = create_agent_runtime(
            AgentFactoryOptions(
                model=self._model,
                max_turns=self._max_turns,
            )
        )
        return runtime.agent_loop

    async def run(self, prompt: str) -> int:
        """Execute a single prompt to completion and return an exit code."""
        from iac_code.services.telemetry import graceful_shutdown, log_event
        from iac_code.services.telemetry.names import Events

        started = time.monotonic()
        start_background_housekeeping()
        agent_loop = self._create_agent_loop()
        writer = create_writer(self._output_format, self._output_stream)

        has_error = False
        hit_max_turns = False

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

            writer.handle(event)

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
