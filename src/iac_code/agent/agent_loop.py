"""Agent Loop - the core execution loop using ProviderManager and concurrent tools."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Any, Literal

from loguru import logger

from iac_code.agent.message import ContentBlock, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock
from iac_code.i18n import _
from iac_code.services.context_manager import ContextManager
from iac_code.services.session_usage import SessionUsageStore, SessionUsageTotals
from iac_code.tools.base import ToolContext, ToolRegistry, ToolResult
from iac_code.tools.result_storage import ResultStorage
from iac_code.tools.tool_executor import ToolCallRequest, ToolExecutor
from iac_code.types.stream_events import (
    CompactionEvent,
    MessageEndEvent,
    PermissionRequestEvent,
    QueuedInputSubmittedEvent,
    StackInstancesProgressEvent,
    StackProgressEvent,
    StreamEvent,
    SubAgentToolEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    TombstoneEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)


@dataclass
class CompactResult:
    """Outcome of a manual /compact invocation.

    ``status`` distinguishes between meaningful no-ops ("empty",
    "too_short") and real failures so the UI can show an accurate message
    instead of lumping them together.
    """

    status: Literal["success", "empty", "too_short", "failed"]
    original_tokens: int = 0
    compacted_tokens: int = 0
    preserve_recent_turns: int = 0


def _user_input_to_text(user_input: str | list[ContentBlock]) -> str:
    if isinstance(user_input, str):
        return user_input
    parts: list[str] = []
    for block in user_input:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return " ".join(part for part in parts if part)


def _normalize_memory_filename(filename: Any) -> str:
    name = str(filename).strip()
    if not name:
        return ""
    if not name.endswith(".md"):
        name = f"{name}.md"
    return name


def _filter_recalled_memory_content(content: str, selected_files: list[str]) -> str:
    keep = [_normalize_memory_filename(filename) for filename in selected_files]
    keep = [filename for filename in keep if filename]
    if not keep:
        return ""

    lines = content.splitlines()
    sections: dict[str, list[str]] = {}
    current_filename = ""
    current_lines: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if current_filename:
                sections[current_filename] = current_lines
            current_filename = _normalize_memory_filename(line[3:].strip())
            current_lines = [line]
            continue
        if current_filename:
            current_lines.append(line)
    if current_filename:
        sections[current_filename] = current_lines

    kept_sections = [sections[filename] for filename in keep if filename in sections]
    if len(kept_sections) != len(keep):
        return ""

    parts = ["# Recalled Memory"]
    for section in kept_sections:
        parts.append("\n".join(section).strip())
    return "\n\n".join(part for part in parts if part)


class AgentLoop:
    """The main agent execution loop.

    Uses ProviderManager for LLM calls, ToolExecutor for concurrent tool execution,
    and yields fine-grained StreamEvents for the UI layer.
    """

    def __init__(
        self,
        provider_manager: Any,  # ProviderManager (avoid circular import)
        system_prompt: str,
        tool_registry: ToolRegistry,
        max_turns: int = 100,
        session_storage: Any = None,  # SessionStorage
        session_usage_store: SessionUsageStore | None = None,
        session_id: str | None = None,
        resume_messages: list | None = None,
        cwd: str | None = None,
        permission_context: Any = None,  # ToolPermissionContext
        permission_context_getter: Any = None,  # Callable[[], ToolPermissionContext | None]
        auto_trigger_skills: list[Any] | None = None,
        memory_recall_service: Any = None,
        system_prompt_refresher: Callable[[], str] | None = None,
    ) -> None:
        self._provider_manager = provider_manager
        self.system_prompt = system_prompt
        self.tool_registry = tool_registry
        self._max_turns = max_turns
        self._session_storage = session_storage
        self._session_id = session_id or str(uuid.uuid4())[:8]
        self._cwd = cwd or os.getcwd()
        self._session_usage_store = session_usage_store or SessionUsageStore()
        self._session_usage_totals = self._session_usage_store.load(self._cwd, self._session_id)
        self._permission_context = permission_context
        self._permission_context_getter = permission_context_getter
        self._auto_trigger_skills = auto_trigger_skills or []
        self._auto_loaded_skills: set[str] = set()
        self._current_git_branch: str | None = None
        self._memory_recall_service = memory_recall_service
        self._recorded_memory_prefetch_ids: set[int] = set()
        self._pending_memory_prefetches: list[Any] = []
        self._memory_recall_generation = 0
        self._memory_recall_active_turns = 0
        self._last_provider_request_snapshot: dict[str, Any] | None = None
        self._system_prompt_refresher = system_prompt_refresher

        model_name = ""
        if hasattr(provider_manager, "get_model_name"):
            model_name = provider_manager.get_model_name()

        self.context_manager = ContextManager(system_prompt=system_prompt, model=model_name)
        self._sync_tool_definitions()
        if resume_messages:
            self.context_manager.load_messages(resume_messages)
        self._sync_recall_suppression_from_context()
        self._tool_executor = ToolExecutor(registry=tool_registry)
        from iac_code.config import get_config_dir

        self._result_storage = ResultStorage(
            storage_dir=os.path.join(str(get_config_dir()), "tool-results", self._session_id),
        )

    def set_provider(self, provider_manager: Any, system_prompt: str | None = None) -> None:
        """Swap the provider manager in place, preserving conversation history.

        Updates the tokenizer/context-window config when the model name changes.
        Optionally refreshes the system prompt — useful when memory or skill
        listing has changed since the loop was constructed.
        """
        self._provider_manager = provider_manager
        new_model = provider_manager.get_model_name() if hasattr(provider_manager, "get_model_name") else ""
        self.context_manager.set_model(new_model)
        if system_prompt is not None:
            self.system_prompt = system_prompt
            self.context_manager.set_system_prompt(system_prompt)
        self._sync_tool_definitions(system_prompt=self.system_prompt if system_prompt is not None else None)

    def set_auto_trigger_skills(self, skill_commands: list[Any] | None) -> None:
        """Refresh skills considered for automatic trigger injection."""
        self._auto_trigger_skills = list(skill_commands or [])

    def get_memory_recall_stats(self) -> dict[str, Any]:
        if self._memory_recall_service is None:
            return {
                "total_side_queries": 0,
                "in_flight_side_queries": 0,
                "successful_side_queries": 0,
                "failed_side_queries": 0,
                "cancelled_side_queries": 0,
                "total_selected_files": 0,
                "last_duration_ms": 0,
                "last_status": "skipped",
                "last_selected_files": [],
                "last_side_query_duration_ms": 0,
                "last_side_query_status": "skipped",
                "last_side_query_selected_files": [],
                "last_prompt_preview": "",
                "last_response_preview": "",
                "last_prompt_chars": 0,
                "last_response_chars": 0,
                "total_usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "total_tokens": 0,
                    "recorded_events": 0,
                    "has_recorded_usage": False,
                },
                "last_usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "total_tokens": 0,
                    "recorded_events": 0,
                    "has_recorded_usage": False,
                },
            }
        get_snapshot = getattr(self._memory_recall_service, "get_stats_snapshot", None)
        if not callable(get_snapshot):
            return {}
        return dict(get_snapshot())

    def get_last_provider_request_snapshot(self) -> dict[str, Any]:
        if self._last_provider_request_snapshot is None:
            return {}
        return {
            "system_prompt": self._last_provider_request_snapshot.get("system_prompt", ""),
            "provider_messages": list(self._last_provider_request_snapshot.get("provider_messages") or []),
            "tools": list(self._last_provider_request_snapshot.get("tools") or []),
        }

    def _start_memory_prefetch_for_turn(self, user_input: str | list[ContentBlock]) -> Any:
        if self._memory_recall_service is None:
            return None
        query = _user_input_to_text(user_input).strip()
        if not query:
            return None
        self._sync_recall_suppression_from_context()
        start_prefetch = getattr(self._memory_recall_service, "start_prefetch", None)
        if callable(start_prefetch):
            prefetch = start_prefetch(query)
        else:
            recall = getattr(self._memory_recall_service, "recall", None)
            if not callable(recall):
                return None
            from iac_code.memory.recall import MemoryRecallPrefetch

            prefetch = MemoryRecallPrefetch(asyncio.create_task(recall(query)))
        if prefetch is not None:
            self._pending_memory_prefetches.append(prefetch)
            add_done_callback = getattr(prefetch, "add_done_callback", None)
            if callable(add_done_callback):
                session_id = self._session_id
                generation = self._memory_recall_generation
                add_done_callback(
                    lambda task, handle=prefetch, sid=session_id, gen=generation: self._handle_memory_prefetch_done(
                        handle,
                        task,
                        session_id=sid,
                        generation=gen,
                    )
                )
        return prefetch

    def _cancel_pending_memory_prefetches(self) -> None:
        for prefetch in list(self._pending_memory_prefetches):
            done = getattr(prefetch, "done", None)
            cancel = getattr(prefetch, "cancel", None)
            if callable(done) and callable(cancel) and not done():
                cancel()
        self._pending_memory_prefetches.clear()
        self._recorded_memory_prefetch_ids.clear()

    def _sync_recall_suppression_from_context(self) -> None:
        if self._memory_recall_service is None:
            return
        mark_files_surfaced = getattr(self._memory_recall_service, "mark_files_surfaced", None)
        if callable(mark_files_surfaced):
            mark_files_surfaced(self.context_manager.get_surfaced_memory_files())

    def _persist_context_messages(self) -> None:
        if not self._session_storage:
            return
        self._session_storage.save(
            self._cwd,
            self._session_id,
            self.context_manager.get_messages(),
            git_branch=self._current_git_branch,
        )

    def _inject_recalled_memory_result(self, result: Any) -> bool:
        content = str(getattr(result, "content", "") or "").strip()
        selected_files = list(getattr(result, "selected_files", None) or [])
        if not content or not selected_files:
            return False
        selected_names = {_normalize_memory_filename(filename) for filename in selected_files}
        selected_names.discard("")
        if not selected_names:
            return False
        suppressed: set[str] = set()
        get_suppressed_files = getattr(self._memory_recall_service, "get_suppressed_files", None)
        if callable(get_suppressed_files):
            suppressed = {_normalize_memory_filename(filename) for filename in get_suppressed_files()}
            suppressed.discard("")
        surfaced = {
            _normalize_memory_filename(filename) for filename in self.context_manager.get_surfaced_memory_files()
        }
        surfaced.discard("")
        suppressed |= surfaced
        injectable_files = [
            filename
            for filename in selected_files
            if (normalized := _normalize_memory_filename(filename)) and normalized not in suppressed
        ]
        if not injectable_files:
            return False
        if len(injectable_files) != len(selected_files):
            content = _filter_recalled_memory_content(content, injectable_files)
            if not content:
                return False
        msg = self.context_manager.add_recalled_memory_message(content, injectable_files)
        if self._session_storage:
            self._session_storage.append(
                self._cwd,
                self._session_id,
                msg,
                git_branch=self._current_git_branch,
            )
        self._mark_recalled_files_surfaced(injectable_files)
        return True

    async def _consume_ready_memory_prefetches(self, prefetch: Any | None = None) -> None:
        await asyncio.sleep(0)
        for item in list(self._pending_memory_prefetches):
            if prefetch is not None and item is not prefetch:
                continue
            done = getattr(item, "done", None)
            if not callable(done) or not done():
                continue
            self._pending_memory_prefetches = [
                pending for pending in self._pending_memory_prefetches if pending is not item
            ]
            try:
                result = item.result()
            except asyncio.CancelledError:
                self._forget_memory_prefetch(item)
                continue
            except Exception as exc:
                logger.debug("Memory recall prefetch failed: {}", exc)
                self._forget_memory_prefetch(item)
                continue
            self._record_memory_recall_result_usage_once(item, result)
            self._inject_recalled_memory_result(result)
            self._forget_memory_prefetch(item)

    def _mark_recalled_files_surfaced(self, selected_files: list[str]) -> None:
        if self._memory_recall_service is None:
            return
        mark_files_surfaced = getattr(self._memory_recall_service, "mark_files_surfaced", None)
        if not callable(mark_files_surfaced):
            return
        if selected_files:
            mark_files_surfaced(selected_files)

    def _handle_memory_prefetch_done(
        self,
        prefetch: Any,
        task: asyncio.Task,
        *,
        session_id: str | None = None,
        generation: int | None = None,
    ) -> None:
        if not any(item is prefetch for item in self._pending_memory_prefetches):
            return
        if session_id is not None and session_id != self._session_id:
            self._pending_memory_prefetches = [item for item in self._pending_memory_prefetches if item is not prefetch]
            self._forget_memory_prefetch(prefetch)
            return
        if generation is not None and generation != self._memory_recall_generation:
            self._pending_memory_prefetches = [item for item in self._pending_memory_prefetches if item is not prefetch]
            self._forget_memory_prefetch(prefetch)
            return
        try:
            result = task.result()
        except asyncio.CancelledError:
            self._pending_memory_prefetches = [item for item in self._pending_memory_prefetches if item is not prefetch]
            self._forget_memory_prefetch(prefetch)
            return
        except Exception as exc:
            logger.debug("Memory recall prefetch usage unavailable: {}", exc)
            self._pending_memory_prefetches = [item for item in self._pending_memory_prefetches if item is not prefetch]
            self._forget_memory_prefetch(prefetch)
            return
        self._record_memory_recall_result_usage_once(prefetch, result)
        if self._memory_recall_active_turns > 0:
            return
        self._pending_memory_prefetches = [item for item in self._pending_memory_prefetches if item is not prefetch]
        self._inject_recalled_memory_result(result)
        self._forget_memory_prefetch(prefetch)

    def _record_memory_recall_result_usage_once(self, prefetch: Any, result: Any) -> None:
        prefetch_id = id(prefetch)
        if prefetch_id in self._recorded_memory_prefetch_ids:
            return
        self._recorded_memory_prefetch_ids.add(prefetch_id)
        self._record_response_usage(result)

    def _forget_memory_prefetch(self, prefetch: Any) -> None:
        self._recorded_memory_prefetch_ids.discard(id(prefetch))

    def _refresh_system_prompt(self) -> None:
        if self._system_prompt_refresher is None:
            return
        try:
            system_prompt = self._system_prompt_refresher()
        except Exception as exc:
            logger.debug("Failed to refresh system prompt: {}", exc)
            return
        if not isinstance(system_prompt, str) or system_prompt == self.system_prompt:
            return
        self.system_prompt = system_prompt
        self.context_manager.set_system_prompt(system_prompt)

    def _sync_tool_system_prompt(self, system_prompt: str, tools: list[Any] | None = None) -> None:
        if tools is None:
            try:
                tools = list(self.tool_registry.list_tools())
            except Exception as exc:
                logger.debug("Failed to list tools while syncing system prompt: {}", exc)
                return
        for tool in tools:
            setter = getattr(tool, "set_system_prompt", None)
            if not callable(setter):
                continue
            try:
                setter(system_prompt)
            except Exception as exc:
                logger.debug("Failed to sync system prompt to tool {}: {}", getattr(tool, "name", ""), exc)

    def _system_prompt_for_current_turn(self) -> str:
        return self.system_prompt

    def _prepare_provider_system_prompt(self) -> str:
        self._refresh_system_prompt()
        system_prompt = self._system_prompt_for_current_turn()
        self.context_manager.set_system_prompt(system_prompt)
        return system_prompt

    def _get_tool_definitions(self, tools: list[Any] | None = None):
        """Convert tool registry to provider ToolDefinition format."""
        from iac_code.providers.base import ToolDefinition

        if tools is None:
            tools = list(self.tool_registry.list_tools())
        tool_definitions = []
        for tool in tools:
            tool_definitions.append(
                ToolDefinition(
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                )
            )
        return tool_definitions

    def _sync_tool_definitions(self, system_prompt: str | None = None):
        """Refresh context token accounting from the current tool registry."""
        tools = list(self.tool_registry.list_tools())
        if system_prompt is not None:
            self._sync_tool_system_prompt(system_prompt, tools=tools)
        tool_definitions = self._get_tool_definitions(tools)
        self.context_manager.set_tool_definitions(tool_definitions)
        return tool_definitions

    def _get_provider_messages(self):
        """Convert context manager messages to provider Message format."""
        from iac_code.providers.base import ContentBlock
        from iac_code.providers.base import Message as ProviderMessage

        api_messages = self.context_manager.get_api_messages()
        provider_messages = []
        for msg in api_messages:
            role = msg["role"]
            content = msg["content"]
            if isinstance(content, str):
                provider_messages.append(ProviderMessage(role=role, content=content))
            elif isinstance(content, list):
                blocks = []
                for block in content:
                    if isinstance(block, dict):
                        block_type = block.get("type", "text")
                        text_value = block.get("thinking") if block_type == "thinking" else block.get("text")
                        blocks.append(
                            ContentBlock(
                                type=block_type,
                                text=text_value,
                                tool_use_id=block.get("tool_use_id") or block.get("id"),
                                name=block.get("name"),
                                input=block.get("input"),
                                content=block.get("content"),
                                is_error=block.get("is_error", False),
                                media_type=block.get("media_type"),
                                data=block.get("data"),
                            )
                        )
                provider_messages.append(ProviderMessage(role=role, content=blocks))
        return provider_messages

    async def run(self, user_input: str | list[ContentBlock]) -> str:
        """Non-streaming execution. Returns final text."""
        final_text = ""
        async for event in self.run_streaming(user_input):
            if isinstance(event, TextDeltaEvent):
                final_text += event.text
        return final_text

    async def run_streaming(
        self,
        user_input: str | list[ContentBlock],
        queued_input_provider: Callable[[], list[str]] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Streaming execution yielding fine-grained StreamEvents.

        Flow:
        1. Add user message to context
        2. Call provider.stream() -> yields StreamEvents
        3. Collect tool_use from events
        4. Execute tools concurrently via ToolExecutor
        5. Yield ToolResultEvents
        6. Loop back to step 2 if tools were called
        """
        from iac_code.services.telemetry import add_metric, get_session_id, get_user_id, log_event, start_span
        from iac_code.services.telemetry.config import should_capture_content_on_span
        from iac_code.services.telemetry.content_serializer import serialize_output_messages
        from iac_code.services.telemetry.names import (
            FRAMEWORK_IAC_CODE,
            Events,
            GenAiAttr,
            GenAiOperationName,
            GenAiSpanKind,
            Metrics,
            Spans,
        )

        entry_attrs: dict[str, Any] = {
            GenAiAttr.SPAN_KIND: GenAiSpanKind.ENTRY,
            GenAiAttr.OPERATION_NAME: GenAiOperationName.ENTER,
            GenAiAttr.SESSION_ID: get_session_id(),
            GenAiAttr.USER_ID: get_user_id(),
            GenAiAttr.FRAMEWORK: FRAMEWORK_IAC_CODE,
        }
        if should_capture_content_on_span():
            from iac_code.services.telemetry.content_serializer import (
                serialize_system_instructions,
                serialize_user_input,
            )

            # serialize_user_input expects str; for structured input (list[ContentBlock]),
            # extract text-only segments so telemetry stays readable without leaking image bytes.
            if isinstance(user_input, str):
                input_text_for_telemetry = user_input
            else:
                input_text_for_telemetry = " ".join(
                    getattr(b, "text", "") for b in user_input if getattr(b, "type", None) == "text"
                )
            entry_attrs[GenAiAttr.INPUT_MESSAGES] = serialize_user_input(input_text_for_telemetry)
            entry_attrs[GenAiAttr.SYSTEM_INSTRUCTIONS] = serialize_system_instructions(self.system_prompt)

        with start_span(Spans.ENTRY, entry_attrs) as entry_span:
            interaction_started = time.monotonic()
            first_token_received = False
            final_text_chunks: list[str] = []
            final_stop_reason = "stop"
            memory_prefetch = None
            turn_cancelled = False
            self._memory_recall_active_turns += 1
            try:
                # Refresh the git branch once per turn — branch may change
                # between turns (user runs git checkout via Bash tool), but
                # is treated as stable within a single in-flight request.
                self._refresh_git_branch()
                await self._apply_auto_triggers(user_input)
                self.context_manager.add_user_message(user_input)
                if self._session_storage:
                    from iac_code.agent.message import Message

                    self._session_storage.append(
                        self._cwd,
                        self._session_id,
                        Message(role="user", content=user_input),
                        git_branch=self._current_git_branch,
                    )
                memory_prefetch = self._start_memory_prefetch_for_turn(user_input)
                try:
                    async for event in self._run_streaming_inner(
                        user_input,
                        queued_input_provider=queued_input_provider,
                        memory_prefetch=memory_prefetch,
                    ):
                        if isinstance(event, TextDeltaEvent) and not first_token_received:
                            first_token_received = True
                            ttft_ns = int((time.monotonic() - interaction_started) * 1_000_000_000)
                            entry_span.set_attribute(GenAiAttr.RESPONSE_TIME_TO_FIRST_TOKEN, ttft_ns)
                            entry_span.set_attribute(GenAiAttr.USER_TIME_TO_FIRST_TOKEN, ttft_ns)
                        if isinstance(event, TextDeltaEvent):
                            final_text_chunks.append(event.text)
                        if isinstance(event, MessageEndEvent):
                            final_stop_reason = event.stop_reason
                            self._record_session_usage(event.usage)
                        yield event
                except asyncio.CancelledError:
                    turn_cancelled = True
                    self._memory_recall_generation += 1
                    self._cancel_pending_memory_prefetches()
                    log_event(Events.SESSION_CANCELLED, {"stage": "in_query"})
                    raise
            finally:
                self._memory_recall_active_turns = max(0, self._memory_recall_active_turns - 1)
                if not turn_cancelled:
                    await self._consume_ready_memory_prefetches()
                self.context_manager.set_system_prompt(self.system_prompt)
                elapsed = time.monotonic() - interaction_started
                add_metric(Metrics.ACTIVE_TIME_TOTAL, int(elapsed), {})
                if should_capture_content_on_span() and final_text_chunks:
                    entry_span.set_attribute(
                        GenAiAttr.OUTPUT_MESSAGES,
                        serialize_output_messages("".join(final_text_chunks), final_stop_reason),
                    )

    async def _run_streaming_inner(
        self,
        user_input: str | list[ContentBlock],
        *,
        queued_input_provider: Callable[[], list[str]] | None = None,
        memory_prefetch: Any = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Inner streaming loop (called from run_streaming inside the ENTRY span)."""
        from iac_code.services.telemetry import start_span
        from iac_code.services.telemetry.names import GenAiAttr, GenAiOperationName, GenAiSpanKind, Spans

        for _turn in range(self._max_turns):
            system_prompt = self._prepare_provider_system_prompt()
            tool_definitions = self._sync_tool_definitions(system_prompt=system_prompt)
            await self._consume_ready_memory_prefetches(memory_prefetch)

            # Auto-compact if needed
            if self.context_manager.needs_compaction():
                compact_event = await self._auto_compact()
                if compact_event:
                    yield compact_event

            step_attrs = {
                GenAiAttr.SPAN_KIND: GenAiSpanKind.STEP,
                GenAiAttr.OPERATION_NAME: GenAiOperationName.REACT,
                GenAiAttr.REACT_ROUND: _turn + 1,
            }

            with start_span(Spans.REACT_STEP, step_attrs) as step_span:
                # Collect tool uses from this turn (keyed by tool_use_id)
                pending_tool_uses_by_id: dict[str, dict[str, Any]] = {}
                text_chunks: list[str] = []
                thinking_chunks: list[str] = []
                message_ended = False

                provider_messages = self._get_provider_messages()
                provider_tools = tool_definitions or None
                self._last_provider_request_snapshot = {
                    "system_prompt": system_prompt,
                    "provider_messages": list(provider_messages),
                    "tools": list(provider_tools or []),
                }

                # Stream from provider
                async for event in self._provider_manager.stream(
                    messages=provider_messages,
                    system=system_prompt,
                    tools=provider_tools,
                ):
                    yield event  # Forward all provider events to UI

                    # Collect data from events
                    if isinstance(event, TextDeltaEvent):
                        text_chunks.append(event.text)
                    elif isinstance(event, ThinkingDeltaEvent):
                        thinking_chunks.append(event.text)
                    elif isinstance(event, ToolUseStartEvent):
                        pending_tool_uses_by_id.setdefault(event.tool_use_id, {})
                        pending_tool_uses_by_id[event.tool_use_id]["id"] = event.tool_use_id
                        pending_tool_uses_by_id[event.tool_use_id]["name"] = event.name
                    elif isinstance(event, ToolUseEndEvent):
                        pending_tool_uses_by_id.setdefault(event.tool_use_id, {})
                        pending_tool_uses_by_id[event.tool_use_id]["id"] = event.tool_use_id
                        pending_tool_uses_by_id[event.tool_use_id]["input"] = event.input
                    elif isinstance(event, TombstoneEvent):
                        pending_tool_uses_by_id.clear()
                        text_chunks.clear()
                        thinking_chunks.clear()
                    elif isinstance(event, MessageEndEvent):
                        message_ended = True

                if not message_ended:
                    step_span.set_attribute(GenAiAttr.REACT_FINISH_REASON, "error")
                    yield MessageEndEvent(stop_reason="stream_error", usage=Usage())
                    break

                # Build assistant message for context
                assistant_blocks = []
                full_thinking = "".join(thinking_chunks)
                if full_thinking:
                    assistant_blocks.append(ThinkingBlock(thinking=full_thinking))
                full_text = "".join(text_chunks)
                if full_text:
                    assistant_blocks.append(TextBlock(text=full_text))

                # Collect completed tool uses (those with both name and input)
                completed_tools = []
                for tu in pending_tool_uses_by_id.values():
                    if "name" in tu and "input" in tu:
                        completed_tools.append(tu)
                        assistant_blocks.append(ToolUseBlock(id=tu["id"], name=tu["name"], input=tu.get("input", {})))

                if assistant_blocks:
                    self.context_manager.add_assistant_message(assistant_blocks)
                    if self._session_storage:
                        from iac_code.agent.message import Message

                        self._session_storage.append(
                            self._cwd,
                            self._session_id,
                            Message(role="assistant", content=assistant_blocks),
                            git_branch=self._current_git_branch,
                        )

                # No tool calls -> end turn
                if not completed_tools:
                    step_span.set_attribute(GenAiAttr.REACT_FINISH_REASON, "stop")
                    break

                step_span.set_attribute(GenAiAttr.REACT_FINISH_REASON, "tool_calls")

                # Execute tools (concurrent read-only, serial writes)
                tools_with_progress = {"agent", "ros_stack", "ros_stack_instances"}
                requests = []
                event_queues: dict[str, asyncio.Queue] = {}
                for tu in completed_tools:
                    queue = None
                    if tu["name"] in tools_with_progress:
                        queue = asyncio.Queue()
                        event_queues[tu["id"]] = queue
                    requests.append(
                        ToolCallRequest(
                            id=tu["id"],
                            name=tu["name"],
                            input=tu.get("input", {}),
                            event_queue=queue,
                        )
                    )
                context = ToolContext(cwd=self._cwd)

                allowed_requests: list[ToolCallRequest] = []
                denied_results: list[tuple[ToolCallRequest, ToolResult]] = []
                for request in requests:
                    tool = self.tool_registry.get(request.name)
                    if tool is None:
                        allowed_requests.append(request)
                        continue

                    perm_ctx = None
                    if self._permission_context_getter is not None:
                        perm_ctx = self._permission_context_getter()
                    if perm_ctx is None:
                        perm_ctx = self._permission_context

                    if perm_ctx is not None:
                        from iac_code.services.permissions.pipeline import check_tool_permission

                        permission = await check_tool_permission(tool, request.input, perm_ctx)
                    else:
                        permission = await tool.check_permissions(request.input, {"cwd": context.cwd})

                    if permission.behavior == "allow":
                        allowed_requests.append(request)
                        continue
                    if permission.behavior == "deny":
                        msg = permission.message or _("Permission denied.")
                        denied_results.append((request, ToolResult.error(msg)))
                        continue

                    response_future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
                    yield PermissionRequestEvent(
                        tool_name=request.name,
                        tool_input=request.input,
                        tool_use_id=request.id,
                        response_future=response_future,
                        permission_result=permission,
                    )
                    if await response_future:
                        allowed_requests.append(request)
                    else:
                        denied_results.append((request, ToolResult.error(_("Permission denied."))))

                for request, result in denied_results:
                    yield ToolResultEvent(
                        tool_use_id=request.id,
                        tool_name=request.name,
                        result=result.content,
                        is_error=True,
                    )

                if not allowed_requests:
                    if denied_results:
                        denied_blocks: list[ToolResultBlock] = [
                            ToolResultBlock(
                                tool_use_id=request.id,
                                content=result.content,
                                is_error=True,
                            )
                            for request, result in denied_results
                        ]
                        self.context_manager.add_tool_results(denied_blocks)
                        if self._session_storage:
                            from iac_code.agent.message import Message

                            denied_content: list[ContentBlock] = list(denied_blocks)
                            self._session_storage.append(
                                self._cwd,
                                self._session_id,
                                Message(role="user", content=denied_content),
                                git_branch=self._current_git_branch,
                            )
                        async for event in self._submit_queued_inputs_after_tool_call(queued_input_provider):
                            yield event
                    continue

                requests = allowed_requests

                # Start tool execution
                exec_task = asyncio.create_task(self._tool_executor.execute_batch(requests, context))

                # Poll event queues while tools execute
                async def poll_event_queues():
                    while not exec_task.done():
                        for req_id, queue in event_queues.items():
                            try:
                                while True:
                                    item = queue.get_nowait()
                                    if item is None:
                                        break
                                    if isinstance(item, (StackProgressEvent, StackInstancesProgressEvent)):
                                        yield item
                                    elif isinstance(item, dict):
                                        yield SubAgentToolEvent(
                                            parent_tool_use_id=req_id,
                                            child_tool_name=item["child_tool_name"],
                                            child_tool_input=item.get("child_tool_input", {}),
                                            is_done=item.get("is_done", False),
                                            is_error=item.get("is_error", False),
                                        )
                            except asyncio.QueueEmpty:
                                pass
                        await asyncio.sleep(0.05)
                    # Final drain
                    for req_id, queue in event_queues.items():
                        while not queue.empty():
                            item = queue.get_nowait()
                            if item is None:
                                continue
                            if isinstance(item, (StackProgressEvent, StackInstancesProgressEvent)):
                                yield item
                            elif isinstance(item, dict):
                                yield SubAgentToolEvent(
                                    parent_tool_use_id=req_id,
                                    child_tool_name=item["child_tool_name"],
                                    child_tool_input=item.get("child_tool_input", {}),
                                    is_done=item.get("is_done", False),
                                    is_error=item.get("is_error", False),
                                )

                async for sub_event in poll_event_queues():
                    yield sub_event

                results = await exec_task

                # Process results and yield ToolResultEvents
                tool_result_blocks: list[ToolResultBlock] = [
                    ToolResultBlock(
                        tool_use_id=request.id,
                        content=result.content,
                        is_error=True,
                    )
                    for request, result in denied_results
                ]
                for req, result in zip(requests, results):
                    processed = self._result_storage.process(req.id, result.content)
                    self._mark_read_memory_tool_result(req, result)

                    yield ToolResultEvent(
                        tool_use_id=req.id,
                        tool_name=req.name,
                        result=processed.content,
                        is_error=result.is_error,
                    )

                    tool_result_blocks.append(
                        ToolResultBlock(
                            tool_use_id=req.id,
                            content=processed.content,
                            is_error=result.is_error,
                        )
                    )

                self.context_manager.add_tool_results(tool_result_blocks)
                if self._session_storage:
                    from iac_code.agent.message import Message

                    result_content: list[ContentBlock] = list(tool_result_blocks)
                    self._session_storage.append(
                        self._cwd,
                        self._session_id,
                        Message(role="user", content=result_content),
                        git_branch=self._current_git_branch,
                    )

                for req, result in zip(requests, results):
                    if result.new_messages:
                        for msg in result.new_messages:
                            self.context_manager.add_raw_message(msg)
                    if result.context_modifier is not None:
                        self._apply_context_modifier(result.context_modifier)

                async for event in self._submit_queued_inputs_after_tool_call(queued_input_provider):
                    yield event
        else:
            yield MessageEndEvent(stop_reason="max_turns", usage=Usage())

    async def _submit_queued_inputs_after_tool_call(
        self,
        queued_input_provider: Callable[[], list[str]] | None,
    ) -> AsyncGenerator[QueuedInputSubmittedEvent, None]:
        if queued_input_provider is None:
            return

        queued_inputs = queued_input_provider()
        for raw_input in queued_inputs:
            text = raw_input.strip()
            if not text:
                continue
            await self._apply_auto_triggers(text)
            message = self.context_manager.add_user_message(text)
            if self._session_storage:
                self._session_storage.append(
                    self._cwd,
                    self._session_id,
                    message,
                    git_branch=self._current_git_branch,
                )
            yield QueuedInputSubmittedEvent(text=text)

    def _mark_read_memory_tool_result(self, request: ToolCallRequest, result: ToolResult) -> None:
        if request.name != "read_memory" or result.is_error or self._memory_recall_service is None:
            return
        name = request.input.get("name")
        if not isinstance(name, str) or not name.strip():
            return
        mark_files_read = getattr(self._memory_recall_service, "mark_files_read", None)
        if callable(mark_files_read):
            filename = name.strip()
            if not filename.endswith(".md"):
                filename = f"{filename}.md"
            mark_files_read([filename])

    async def _apply_auto_triggers(self, user_input: str | list[ContentBlock]) -> None:
        if not self._auto_trigger_skills:
            return
        if all(command.name in self._auto_loaded_skills for command in self._auto_trigger_skills):
            return
        prompt_text = self._auto_trigger_text(user_input)
        if not prompt_text:
            return

        from iac_code.skills.auto_trigger import process_auto_triggered_skills

        results = await process_auto_triggered_skills(
            prompt_text,
            self._auto_trigger_skills,
            loaded_skill_names=self._auto_loaded_skills,
            context_messages=self.context_manager.get_messages(),
            session_id=self._session_id,
        )
        for result in results:
            for msg in result.new_messages:
                injected = self.context_manager.add_raw_message(msg)
                if self._session_storage:
                    self._session_storage.append(
                        self._cwd,
                        self._session_id,
                        injected,
                        git_branch=self._current_git_branch,
                    )
            if result.context_modifier is not None:
                self._apply_context_modifier(result.context_modifier)

    @staticmethod
    def _auto_trigger_text(user_input: str | list[ContentBlock]) -> str:
        if isinstance(user_input, str):
            return user_input
        parts = [block.text for block in user_input if isinstance(block, TextBlock)]
        return " ".join(part for part in parts if part).strip()

    def _apply_context_modifier(self, modifier: Any) -> None:
        """Apply a context modifier from a ToolResult to the current execution context."""
        current_ctx: dict[str, Any] = {
            "allowed_tool_rules": getattr(self, "_allowed_tool_rules", []),
            "model_override": getattr(self, "_model_override", None),
            "effort_override": getattr(self, "_effort_override", None),
        }
        modified = modifier(current_ctx)
        self._allowed_tool_rules = modified.get("allowed_tool_rules", [])
        self._model_override = modified.get("model_override")
        self._effort_override = modified.get("effort_override")

    async def _auto_compact(self) -> CompactionEvent | None:
        """Perform automatic context compaction via provider."""
        from iac_code.services.telemetry import log_event
        from iac_code.services.telemetry.names import Events

        compaction_prompt = self.context_manager.build_compaction_prompt()
        if not compaction_prompt:
            return None
        started = time.monotonic()
        try:
            from iac_code.providers.base import Message as ProviderMessage

            response = await self._provider_manager.complete(
                messages=[ProviderMessage.user(compaction_prompt)],
                system="You are a helpful assistant that summarizes conversations concisely.",
            )
            self._record_response_usage(response)
            if response.text:
                original, new = self.context_manager.apply_compaction(response.text)
                self._sync_recall_suppression_from_context()
                self._persist_context_messages()
                duration_ms = int((time.monotonic() - started) * 1000)
                log_event(
                    Events.MEMORY_COMPACT_SUCCEEDED,
                    {
                        "rounds": 1,
                        "from_tokens": original,
                        "to_tokens": new,
                        "duration_ms": duration_ms,
                    },
                )
                return CompactionEvent(original_tokens=original, compacted_tokens=new)
        except Exception as e:
            log_event(
                Events.MEMORY_COMPACT_FAILED,
                {
                    "rounds": 1,
                    "error_type": type(e).__name__,
                },
            )
            logger.error(f"Auto-compaction failed: {e}", exc_info=True)
        return None

    async def compact(self) -> CompactResult:
        """Manual compaction for /compact command."""
        if not self.context_manager.get_messages():
            return CompactResult(status="empty")
        compaction_prompt = self.context_manager.build_compaction_prompt()
        if not compaction_prompt:
            return CompactResult(
                status="too_short",
                preserve_recent_turns=self.context_manager.preserve_recent_turns,
            )
        try:
            from iac_code.providers.base import Message as ProviderMessage

            response = await self._provider_manager.complete(
                messages=[ProviderMessage.user(compaction_prompt)],
                system="You are a helpful assistant that summarizes conversations concisely.",
            )
            self._record_response_usage(response)
            if response.text:
                original, compacted = self.context_manager.apply_compaction(response.text)
                self._sync_recall_suppression_from_context()
                self._persist_context_messages()
                return CompactResult(
                    status="success",
                    original_tokens=original,
                    compacted_tokens=compacted,
                )
        except Exception as e:
            logger.error(f"Manual compaction failed: {e}", exc_info=True)
        return CompactResult(status="failed")

    def stamp_last_turn_elapsed(self, elapsed: float) -> None:
        """Record turn duration on the last assistant message and persist it."""
        msgs = self.context_manager.get_messages()
        for msg in reversed(msgs):
            if msg.role == "assistant":
                msg.elapsed_seconds = elapsed
                if self._session_storage:
                    self._session_storage.save(
                        self._cwd,
                        self._session_id,
                        msgs,
                        git_branch=self._current_git_branch,
                    )
                break

    def replace_session(self, session_id: str, resume_messages: list | None) -> None:
        """Swap the active session in-place, preserving provider/tools.

        Resets the conversation context to ``resume_messages`` (or empty),
        repoints the session id, and rebuilds the per-session ResultStorage
        directory. Used by the /resume command for in-process hot-swap.
        """
        from iac_code.config import get_config_dir

        self._cancel_pending_memory_prefetches()
        self._memory_recall_generation += 1
        self._last_provider_request_snapshot = None
        self._session_id = session_id
        self._current_git_branch = None
        self._auto_loaded_skills.clear()
        self.context_manager.reset()
        if resume_messages:
            self.context_manager.load_messages(resume_messages)
        self._session_usage_totals = self._session_usage_store.load(self._cwd, self._session_id)
        reset_recall_stats = getattr(self._memory_recall_service, "reset_stats", None)
        if callable(reset_recall_stats):
            reset_recall_stats()
        self._sync_recall_suppression_from_context()
        self._result_storage = ResultStorage(
            storage_dir=os.path.join(str(get_config_dir()), "tool-results", session_id),
        )

    def _refresh_git_branch(self) -> None:
        """Probe ``git`` once per turn and cache the result.

        Failures (no git, not a repo, timeout) silently leave the cache
        as ``None`` so the storage layer omits the field.
        """
        from iac_code.utils.project_paths import get_git_branch

        try:
            self._current_git_branch = get_git_branch(self._cwd)
        except Exception:
            self._current_git_branch = None

    def reset(self) -> None:
        self._cancel_pending_memory_prefetches()
        self._memory_recall_generation += 1
        self._last_provider_request_snapshot = None
        self._auto_loaded_skills.clear()
        self.context_manager.reset()
        reset_recall_stats = getattr(self._memory_recall_service, "reset_stats", None)
        if callable(reset_recall_stats):
            reset_recall_stats()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def max_turns(self) -> int:
        return self._max_turns

    def get_context_usage(self) -> dict:
        return self.context_manager.get_usage()

    def get_session_usage(self) -> SessionUsageTotals:
        return self._session_usage_totals.copy()

    def _record_session_usage(self, usage: Usage) -> None:
        if not self._session_usage_totals.add(usage):
            return

        provider = self._get_runtime_provider_key()
        model = self._provider_manager.get_model_name() if hasattr(self._provider_manager, "get_model_name") else ""
        try:
            self._session_usage_store.append(
                self._cwd,
                self._session_id,
                usage,
                provider=provider,
                model=model,
            )
        except Exception as exc:
            logger.debug("Failed to persist session usage for {}: {}", self._session_id, exc)

    def _record_response_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if isinstance(usage, Usage):
            self._record_session_usage(usage)

    def _get_runtime_provider_key(self) -> str:
        if hasattr(self._provider_manager, "get_provider_key"):
            try:
                provider_key = self._provider_manager.get_provider_key()
            except Exception:
                pass
            else:
                if isinstance(provider_key, str):
                    return provider_key
        try:
            from iac_code.config import get_active_provider_key

            return get_active_provider_key() or ""
        except Exception:
            return ""
