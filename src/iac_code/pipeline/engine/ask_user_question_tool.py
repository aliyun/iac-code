"""AskUserQuestionTool — pipeline-only tool for user clarification."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.types.stream_events import AskUserQuestionEvent


class AskUserQuestionTool(Tool):
    """Emit a user question event and wait for the UI to resolve it."""

    def __init__(self, completion_guard_state: dict[str, Any] | None = None) -> None:
        self._completion_guard_state = completion_guard_state

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

        payload = {
            "selected_id": answer.get("selected_id", ""),
            "selected_label": answer.get("selected_label", ""),
            "free_text": answer.get("free_text", ""),
        }
        if self._completion_guard_state is not None:
            successful_tools = self._completion_guard_state.setdefault("successful_tools", set())
            successful_tools.add(self.name)
            tool_results = self._completion_guard_state.setdefault("tool_results", {})
            tool_results[self.name] = payload
        return ToolResult.success(json.dumps(payload, ensure_ascii=False))
