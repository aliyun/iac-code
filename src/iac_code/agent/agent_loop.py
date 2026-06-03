"""Agent Loop - the core execution loop using ProviderManager and concurrent tools."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncGenerator
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

        model_name = ""
        if hasattr(provider_manager, "get_model_name"):
            model_name = provider_manager.get_model_name()

        self.context_manager = ContextManager(system_prompt=system_prompt, model=model_name)
        if resume_messages:
            self.context_manager.load_messages(resume_messages)
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

    def set_auto_trigger_skills(self, skill_commands: list[Any] | None) -> None:
        """Refresh skills considered for automatic trigger injection."""
        self._auto_trigger_skills = list(skill_commands or [])

    def _get_tool_definitions(self):
        """Convert tool registry to provider ToolDefinition format."""
        from iac_code.providers.base import ToolDefinition

        tools = []
        for tool in self.tool_registry.list_tools():
            tools.append(
                ToolDefinition(
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                )
            )
        return tools

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

    async def run_streaming(self, user_input: str | list[ContentBlock]) -> AsyncGenerator[StreamEvent, None]:
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
                try:
                    async for event in self._run_streaming_inner(user_input):
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
                    log_event(Events.SESSION_CANCELLED, {"stage": "in_query"})
                    raise
            finally:
                elapsed = time.monotonic() - interaction_started
                add_metric(Metrics.ACTIVE_TIME_TOTAL, int(elapsed), {})
                if should_capture_content_on_span() and final_text_chunks:
                    entry_span.set_attribute(
                        GenAiAttr.OUTPUT_MESSAGES,
                        serialize_output_messages("".join(final_text_chunks), final_stop_reason),
                    )

    async def _run_streaming_inner(self, user_input: str | list[ContentBlock]) -> AsyncGenerator[StreamEvent, None]:
        """Inner streaming loop (called from run_streaming inside the ENTRY span)."""
        from iac_code.services.telemetry import start_span
        from iac_code.services.telemetry.names import GenAiAttr, GenAiOperationName, GenAiSpanKind, Spans

        tool_definitions = self._get_tool_definitions()

        for _turn in range(self._max_turns):
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

                # Stream from provider
                async for event in self._provider_manager.stream(
                    messages=self._get_provider_messages(),
                    system=self.system_prompt,
                    tools=tool_definitions if self.tool_registry.list_tools() else None,
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
                                self._cwd, self._session_id, Message(role="user", content=denied_content)
                            )
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
        else:
            yield MessageEndEvent(stop_reason="max_turns", usage=Usage())

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

        self._session_id = session_id
        self._current_git_branch = None
        self._auto_loaded_skills.clear()
        self.context_manager.reset()
        if resume_messages:
            self.context_manager.load_messages(resume_messages)
        self._session_usage_totals = self._session_usage_store.load(self._cwd, self._session_id)
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
        self._auto_loaded_skills.clear()
        self.context_manager.reset()

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
