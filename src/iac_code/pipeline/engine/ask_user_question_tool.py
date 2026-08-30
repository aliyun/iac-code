"""AskUserQuestionTool — pipeline-only tool for user clarification."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from iac_code.i18n import _
from iac_code.pipeline.engine.completion_guard_state import record_completion_guard_tool_result
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.types.stream_events import AskUserQuestionEvent

QuestionAnsweredObserver = Callable[[str | None, int, str], None]
logger = logging.getLogger(__name__)


class AskUserQuestionTool(Tool):
    """Emit a user question event and wait for the UI to resolve it."""

    def __init__(
        self,
        completion_guard_state: dict[str, Any] | None = None,
        *,
        question_answered_observer: QuestionAnsweredObserver | None = None,
    ) -> None:
        self._completion_guard_state = completion_guard_state
        self._question_answered_observer = question_answered_observer

    @property
    def name(self) -> str:
        return "ask_user_question"

    @property
    def description(self) -> str:
        return _("Pipeline-only tool that asks the user to choose an option or type clarification details.")

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["question", "options"],
            "additionalProperties": False,
            "properties": {
                "question": {
                    "type": "string",
                    "description": _("The user-facing question to ask."),
                },
                "options": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["id", "label"],
                        "additionalProperties": False,
                        "properties": {
                            "id": {"type": "string"},
                            "label": {"type": "string"},
                            "description": {"type": "string"},
                        },
                    },
                },
                "allow_free_text": {"type": "boolean", "default": True},
                "free_text_prompt": {"type": "string"},
            },
        }

    @property
    def timeout(self) -> float | None:
        return 3600.0

    def is_read_only(self, input: dict | None = None) -> bool:
        return True

    def is_concurrency_safe(self, tool_input: dict[str, Any]) -> bool:
        return False

    def needs_event_queue(self) -> bool:
        return True

    def render_tool_result_message(self, output: str, *, is_error: bool = False, verbose: bool = False) -> str | None:
        if not output or not is_error:
            return None
        if output.startswith("Invalid input for tool 'ask_user_question':"):
            if verbose:
                return output.strip()
            return _("ask_user_question validation failed.")
        return output.strip()

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.event_queue is None:
            return ToolResult.error(_("ask_user_question requires a pipeline event queue."))

        future: asyncio.Future[dict[str, str] | None] = asyncio.get_running_loop().create_future()
        await context.event_queue.put(
            AskUserQuestionEvent(
                tool_use_id=context.tool_use_id or "",
                question=tool_input["question"],
                options=tool_input["options"],
                allow_free_text=tool_input.get("allow_free_text", True),
                free_text_prompt=tool_input.get("free_text_prompt", ""),
                response_future=future,
            )
        )

        try:
            answer = await asyncio.shield(future)
        except asyncio.CancelledError:
            if not future.done():
                future.set_result(None)
            raise
        if answer is None:
            return ToolResult.error(_("User cancelled ask_user_question."))

        if answer.get("selected_id") and answer.get("free_text"):
            answer_type = "option_and_free_text"
        elif answer.get("selected_id"):
            answer_type = "option"
        elif answer.get("free_text"):
            answer_type = "free_text"
        else:
            answer_type = "empty"
        if self._question_answered_observer is not None:
            try:
                self._question_answered_observer(context.tool_use_id, len(tool_input["options"]), answer_type)
            except Exception:
                logger.warning("Failed to observe submitted pipeline question answer", exc_info=True)

        payload = {
            "selected_id": answer.get("selected_id", ""),
            "selected_label": answer.get("selected_label", ""),
            "free_text": answer.get("free_text", ""),
        }
        content = json.dumps(payload, ensure_ascii=False)
        if self._completion_guard_state is not None:
            # Route through the shared recorder so live answers and transcript
            # replay produce the same ordered guard records.
            record_completion_guard_tool_result(
                self._completion_guard_state,
                tool_name=self.name,
                tool_input=tool_input,
                content=content,
                is_error=False,
            )
        return ToolResult.success(content)
