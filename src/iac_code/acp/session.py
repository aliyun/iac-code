from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import time
import uuid
from collections import OrderedDict
from typing import Any

import acp

from iac_code.a2a.artifacts import sanitize_public_tool_output_data
from iac_code.acp.convert import ACPEventConverter, _tool_kind, acp_blocks_to_prompt_text
from iac_code.acp.metrics import ACPMetrics
from iac_code.acp.slash_registry import ACPSlashRegistry
from iac_code.acp.state import TurnState, display_tool_title
from iac_code.acp.tools import ACPTerminalBashTool
from iac_code.acp.types import ACPContentBlock
from iac_code.agent.message import (
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    is_recalled_memory_message,
)
from iac_code.commands.registry import PromptCommand
from iac_code.i18n import _
from iac_code.services.permissions.audit import (
    build_input_summary,
    build_prompt_tool_input,
    emit_permission_boundary_audit,
    is_permission_audit_non_read_only,
    should_fail_closed_permission_audit,
)
from iac_code.services.telemetry import use_session_id
from iac_code.state.app_state import lookup_permission, record_permission
from iac_code.types.permissions import PermissionDecision
from iac_code.types.stream_events import PermissionRequestEvent, SubPipelineStreamEvent
from iac_code.utils.public_errors import public_error

logger = logging.getLogger(__name__)

_current_turn_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("_current_turn_id", default=None)


def _is_auth_error(exc: Exception) -> bool:
    """Detect authentication / credential configuration errors."""
    # Provider not configured (ValueError from create_provider)
    if isinstance(exc, ValueError):
        msg = str(exc).lower()
        if "provider" in msg or "configure" in msg or "/auth" in msg:
            return True

    # SDK-level authentication errors (openai / anthropic)
    exc_type_name = type(exc).__name__
    if exc_type_name == "AuthenticationError":
        return True

    # HTTP 401 status from provider SDKs
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status == 401:
        return True

    return False


# ---------------------------------------------------------------------------
# History replay — convert Message objects to ACP session_update events
# ---------------------------------------------------------------------------


def _history_tool_call_id(tool_use_id: str, history_index: int) -> str:
    return f"history/{history_index}/{tool_use_id}"


def _history_tool_content(text: str) -> acp.schema.ContentToolCallContent:
    return acp.schema.ContentToolCallContent(
        type="content",
        content=acp.schema.TextContentBlock(type="text", text=text),
    )


def _history_tool_result_text(value: Any) -> str:
    sanitized = sanitize_public_tool_output_data(value)
    if isinstance(sanitized, str):
        return sanitized
    return json.dumps(sanitized, ensure_ascii=False, default=str)


def _history_tool_input_text(tool_name: str, tool_input: dict[str, Any]) -> str:
    if not tool_input:
        return ""
    return _display_tool_input_text(tool_name, tool_input)


def _display_tool_input_text(tool_name: str, tool_input: dict[str, Any]) -> str:
    if tool_name != "aliyun_api":
        tool_input_redacted = json.dumps(
            build_prompt_tool_input(tool_input),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return _("Input: {input}").format(input=tool_input_redacted)
    summary = json.dumps(build_input_summary(tool_name, tool_input), ensure_ascii=False, sort_keys=True)
    return _("Input summary: {summary}").format(summary=summary)


def _permission_request_event(event: Any) -> PermissionRequestEvent | None:
    if isinstance(event, PermissionRequestEvent):
        return event
    if isinstance(event, SubPipelineStreamEvent):
        return _permission_request_event(event.inner)
    return None


def _history_message_to_updates(
    msg: Message,
    *,
    history_index: int = 0,
    tool_call_ids: dict[str, str] | None = None,
) -> list[Any]:
    """Convert a single persisted *Message* to a list of ACP session updates.

    * **user** messages become ``UserMessageUpdate`` (ACP "user_message").
    * **assistant** text / thinking become ``AgentMessageChunk`` / ``AgentThoughtChunk``.
    * **assistant** tool-use blocks become ``ToolCallStart`` then an
      in-progress input update.
    * **user** tool-result blocks are emitted as an in-progress content update
      followed by a terminal ``ToolCallProgress``.
    """
    if is_recalled_memory_message(msg):
        return []

    updates: list[Any] = []
    content = msg.content
    tool_call_ids = tool_call_ids if tool_call_ids is not None else {}

    if msg.role == "user":
        # Simple text prompt
        if isinstance(content, str):
            updates.append(
                acp.schema.UserMessageChunk(
                    session_update="user_message_chunk",
                    content=acp.schema.TextContentBlock(type="text", text=content),
                )
            )
            return updates

        # Tool-result blocks from a user message
        for block in content:
            if isinstance(block, ToolResultBlock):
                status = "failed" if block.is_error else "completed"
                text = _history_tool_result_text(block.content)
                tool_call_id = tool_call_ids.get(block.tool_use_id) or _history_tool_call_id(
                    block.tool_use_id,
                    history_index,
                )
                updates.append(
                    acp.schema.ToolCallProgress(
                        session_update="tool_call_update",
                        tool_call_id=tool_call_id,
                        status="in_progress",
                        content=[_history_tool_content(text)],
                    )
                )
                updates.append(
                    acp.schema.ToolCallProgress(
                        session_update="tool_call_update",
                        tool_call_id=tool_call_id,
                        status=status,
                    )
                )
        return updates

    # role == "assistant"
    if isinstance(content, str):
        updates.append(
            acp.schema.AgentMessageChunk(
                session_update="agent_message_chunk",
                content=acp.schema.TextContentBlock(type="text", text=content),
            )
        )
        return updates

    for block in content:
        if isinstance(block, TextBlock):
            updates.append(
                acp.schema.AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=acp.schema.TextContentBlock(type="text", text=block.text),
                )
            )
        elif isinstance(block, ThinkingBlock):
            updates.append(
                acp.schema.AgentThoughtChunk(
                    session_update="agent_thought_chunk",
                    content=acp.schema.TextContentBlock(type="text", text=block.thinking),
                )
            )
        elif isinstance(block, ToolUseBlock):
            tool_call_id = _history_tool_call_id(block.id, history_index)
            tool_call_ids[block.id] = tool_call_id
            updates.append(
                acp.schema.ToolCallStart(
                    session_update="tool_call",
                    tool_call_id=tool_call_id,
                    title=display_tool_title(block.name),
                    kind=_tool_kind(block.name),
                    status="pending",
                )
            )
            input_text = _history_tool_input_text(block.name, block.input)
            updates.append(
                acp.schema.ToolCallProgress(
                    session_update="tool_call_update",
                    tool_call_id=tool_call_id,
                    status="in_progress",
                    content=[_history_tool_content(input_text)],
                )
            )
    return updates


# Permission option IDs used in request_permission and cache lookups.
_OPTION_ALLOW_ONCE = "allow_once"
_OPTION_ALLOW_ALWAYS = "allow_always"
_OPTION_REJECT_ONCE = "reject_once"
_OPTION_REJECT_ALWAYS = "reject_always"
_PREFIX_ALLOW_RULE = "allow_rule:"
_PREFIX_DENY_RULE = "deny_rule:"


def _tool_supports_blanket_allow(agent_loop, tool_name: str) -> bool:
    """Return False only when the registered tool explicitly disables blanket allow."""
    registry = getattr(agent_loop, "tool_registry", None)
    get_tool = getattr(registry, "get", None)
    if get_tool is None:
        return True

    tool = get_tool(tool_name)
    if tool is None:
        return True

    return bool(getattr(tool, "supports_blanket_allow", True))


class ACPSession:
    def __init__(
        self,
        session_id: str,
        agent_loop,
        conn: acp.Client,
        mcp_configs: list[dict] | None = None,
        mcp_manager=None,
        command_registry=None,
        metrics: ACPMetrics | None = None,
        memory_manager=None,
        runtime=None,
        mcp_config_warnings: list[Any] | None = None,
    ) -> None:
        self.id = session_id
        self.agent_loop = agent_loop
        self.runtime = runtime
        self.memory_manager = memory_manager
        self._conn = conn
        self._current_task: asyncio.Task | None = None
        self._replay_task: asyncio.Task[None] | None = None
        self._current_turn: TurnState | None = None
        self.last_active: float = time.monotonic()
        # Per-session permission memory: tool_name -> "always_allow" | "always_deny".
        # Bounded LRU to avoid unbounded growth on long-running sessions; oldest
        # decisions are evicted once ``_PERMISSION_CACHE_MAX_SIZE`` is reached.
        self._permission_cache: OrderedDict[str, PermissionDecision] = OrderedDict()
        # Auto-detect tool names whose output is already displayed via ACP terminal.
        self._terminal_tool_names: set[str] = self._detect_terminal_tools()
        self.mcp_configs: list[dict] = mcp_configs or []
        self.mcp_manager = mcp_manager
        self.mcp_config_warnings = mcp_config_warnings if mcp_config_warnings is not None else []
        self._mcp_warnings_pushed_count = 0
        self.command_registry = command_registry
        # Dynamic session configuration (temperature, max_tokens, etc.)
        self._config: dict[str, Any] = {}
        # Whether this session has been closed
        self._closed: bool = False
        # Optional metrics collector (shared with ACPServer)
        self._metrics: ACPMetrics | None = metrics

    def _detect_terminal_tools(self) -> set[str]:
        """Inspect the agent_loop tool registry for ACP terminal tools."""
        names: set[str] = set()
        registry = getattr(self.agent_loop, "tool_registry", None)
        if registry is None:
            return names
        for tool in registry.list_tools():
            if isinstance(tool, ACPTerminalBashTool):
                names.add(tool.name)
        return names

    def _context_snapshot(self) -> tuple[int, int]:
        """Return ``(used_tokens, context_window_size)`` for this session.

        Used by :class:`ACPEventConverter` to emit ACP ``UsageUpdate`` events
        carrying current context-window occupancy. Returns ``(0, 0)`` if the
        underlying ``agent_loop`` does not expose a ``context_manager``.
        """
        ctx = getattr(self.agent_loop, "context_manager", None)
        if ctx is None:
            return (0, 0)
        return (ctx.get_total_tokens(), ctx.context_window)

    def touch(self) -> None:
        """Update last active timestamp."""
        self.last_active = time.monotonic()

    async def replay_history(self, messages: list[Message]) -> None:
        """Replay persisted history as ACP session_update events.

        Converts stored :class:`Message` objects into ACP ``session_update``
        notifications so the client can rebuild its UI state after
        ``load_session`` or ``fork_session``.
        """
        replay_batch_size = 50
        tool_call_ids: dict[str, str] = {}
        for i, msg in enumerate(messages):
            updates = _history_message_to_updates(msg, history_index=i, tool_call_ids=tool_call_ids)
            for update in updates:
                await self._conn.session_update(session_id=self.id, update=update)
            if (i + 1) % replay_batch_size == 0:
                await asyncio.sleep(0)

    def update_config(self, config: dict[str, Any]) -> None:
        """Update dynamic session configuration.

        Merges *config* into the current session config.  Keys like
        ``temperature``, ``max_tokens`` etc. can be used by the agent loop
        when supported.
        """
        self._config.update(config)

    @property
    def config(self) -> dict[str, Any]:
        """Return a read-only snapshot of the current dynamic config."""
        return dict(self._config)

    @property
    def is_closed(self) -> bool:
        """Whether this session has been closed."""
        return self._closed

    async def close(self) -> None:
        """Release all resources associated with this session.

        This method is **idempotent**: calling it on an already-closed session
        is a no-op.
        """
        if self._closed:
            return

        # Cancel any running prompt task
        if self._current_task is not None and not self._current_task.done():
            self._current_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._current_task
            self._current_task = None

        # Cancel any running replay task
        if self._replay_task is not None and not self._replay_task.done():
            self._replay_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._replay_task
            self._replay_task = None

        # Clean up turn state
        self._current_turn = None

        runtime_close = getattr(self.runtime, "aclose", None)
        if callable(runtime_close):
            with contextlib.suppress(Exception):
                await runtime_close()
        elif self.mcp_manager is not None:
            with contextlib.suppress(Exception):
                await self.mcp_manager.disconnect_all()

        # Clear permission cache and config
        self._permission_cache.clear()
        self._config.clear()

        self._closed = True
        logger.info("Session %s closed", self.id)

    async def prompt(self, prompt: list[ACPContentBlock]) -> acp.PromptResponse:
        if self._closed:
            raise acp.RequestError.internal_error({"error": "Session is closed"})
        self.touch()

        # Intercept slash commands before sending to agent loop
        prompt_text = acp_blocks_to_prompt_text(prompt)
        slash_registry = ACPSlashRegistry()
        stream_factory = None
        if slash_registry.is_slash_command(prompt_text):
            prompt_command = self._lookup_prompt_command(prompt_text)
            if prompt_command is not None:
                prompt_text, stream_factory = await self._prepare_prompt_command(prompt_text, prompt_command)
            else:
                result = await slash_registry.execute(
                    prompt_text,
                    self.agent_loop,
                    memory_manager=self.memory_manager,
                )
                await self._conn.session_update(
                    session_id=self.id,
                    update=acp.schema.AgentMessageChunk(
                        session_update="agent_message_chunk",
                        content=acp.schema.TextContentBlock(type="text", text=result),
                    ),
                )
                return acp.PromptResponse(stop_reason="end_turn")

        if stream_factory is None:

            def stream_factory():
                return self.agent_loop.run_streaming(prompt_text)

        converter: ACPEventConverter | None = None

        async def _run() -> None:
            nonlocal converter
            turn_id = str(uuid.uuid4())
            _current_turn_id.set(turn_id)
            self._current_turn = TurnState(turn_id=turn_id)
            converter = ACPEventConverter(
                turn_id=turn_id,
                turn_state=self._current_turn,
                terminal_tool_names=self._terminal_tool_names,
                context_snapshot=self._context_snapshot,
            )
            logger.debug("Prompt started, session_id=%s, turn_id=%s", self.id, turn_id)
            with use_session_id(self.id):
                async for event in stream_factory():
                    permission_event = _permission_request_event(event)
                    if permission_event is not None:
                        allowed = await self._request_permission(permission_event)
                        if permission_event.response_future is not None and not permission_event.response_future.done():
                            permission_event.response_future.set_result(allowed)
                        continue

                    for update in converter.event_to_updates(event):
                        await self._conn.session_update(session_id=self.id, update=update)

        prompt_start = time.monotonic()
        self._current_task = asyncio.create_task(_run())
        try:
            await self._current_task
        except asyncio.CancelledError:
            elapsed_ms = int((time.monotonic() - prompt_start) * 1000)
            logger.info("Prompt cancelled, session_id=%s, elapsed_ms=%d", self.id, elapsed_ms)
            return acp.PromptResponse(stop_reason="cancelled")
        except Exception as exc:
            if self._metrics is not None:
                self._metrics.record_error()
            if _is_auth_error(exc):
                logger.warning("ACP session %s: authentication error: %s", self.id, exc)
                raise acp.RequestError.internal_error(
                    {
                        "error": "Authentication required. Please configure your API credentials.",
                        "code": "auth_required",
                    }
                ) from exc
            logger.error("ACP session %s: unhandled error: %s", self.id, exc, exc_info=True)
            failure = public_error(message=f"{type(exc).__name__}: {exc}", error_type=type(exc).__name__)
            raise acp.RequestError.internal_error(
                {
                    "error": failure.summary,
                    "error_id": failure.error_id,
                }
            ) from exc
        finally:
            self._current_task = None
            duration_ms = (time.monotonic() - prompt_start) * 1000
            if self._metrics is not None:
                self._metrics.record_prompt(duration_ms)
            # Force-flush telemetry between prompts. The acp server may run in
            # an ephemeral sandbox that's destroyed immediately after the
            # response is delivered, before the natural batch interval or
            # process-exit graceful_shutdown can run. Synchronous flush is
            # offloaded to a worker thread so the event loop is not blocked.
            from iac_code.services.telemetry import flush_telemetry

            try:
                await asyncio.to_thread(flush_telemetry)
            except Exception:
                logger.debug("flush_telemetry after prompt failed", exc_info=True)

        self.touch()

        # Build _meta with timing and token usage
        elapsed_ms = int((time.monotonic() - prompt_start) * 1000)
        meta: dict[str, Any] = {"timing": {"elapsed_ms": elapsed_ms}}
        if converter is not None and converter._last_usage is not None:
            usage = converter._last_usage
            meta["usage"] = {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
            }
        logger.debug("Prompt completed, session_id=%s, elapsed_ms=%d", self.id, elapsed_ms)

        response = acp.PromptResponse(stop_reason="end_turn")
        response.field_meta = meta
        return response

    def _lookup_prompt_command(self, prompt_text: str) -> PromptCommand | None:
        command_registry = self.command_registry
        if command_registry is None:
            return None
        stripped = prompt_text.strip()
        parts = stripped[1:].split(None, 1)
        if not parts:
            return None
        name = parts[0]
        command = command_registry.get(name) or command_registry.get(name.lower())
        return command if isinstance(command, PromptCommand) else None

    async def _prepare_prompt_command(self, prompt_text: str, command: PromptCommand):
        from iac_code.skills.processor import process_prompt_command

        stripped = prompt_text.strip()
        parts = stripped[1:].split(None, 1)
        args = parts[1] if len(parts) > 1 else ""
        result = await process_prompt_command(command, args, session_id=self.id)
        if result.is_fork:
            return result.prompt_content, lambda: self.agent_loop.run_streaming(result.prompt_content)
        context_manager = getattr(self.agent_loop, "context_manager", None)
        if context_manager is not None:
            for message in result.new_messages:
                add_raw_message = getattr(context_manager, "add_raw_message", None)
                if add_raw_message is not None:
                    add_raw_message(message)
        if result.context_modifier:
            apply_context_modifier = getattr(self.agent_loop, "_apply_context_modifier", None)
            if apply_context_modifier is not None:
                apply_context_modifier(result.context_modifier)
        continue_streaming = getattr(self.agent_loop, "continue_streaming", None)
        if callable(continue_streaming):
            return result.prompt_content, continue_streaming
        return result.prompt_content, lambda: self.agent_loop.run_streaming(result.prompt_content)

    async def cancel(self) -> None:
        if self._current_task is not None and not self._current_task.done():
            logger.info("Session %s cancel requested", self.id)
            self._current_task.cancel()

    def _get_permission_context(self):
        """Read the agent_loop's mutable permission context."""
        return getattr(self.agent_loop, "_permission_context", None)

    def _set_permission_context(self, perm_ctx) -> None:
        """Write back the updated permission context to agent_loop."""
        if hasattr(self.agent_loop, "_permission_context"):
            self.agent_loop._permission_context = perm_ctx

    def _apply_rule(self, tool_name: str, rules_str: str, behavior: str) -> None:
        """Apply rule-level permission to the session's permission_context."""
        from iac_code.services.permissions.storage import apply_session_rule
        from iac_code.types.permissions import PermissionRuleValue

        perm_ctx = self._get_permission_context()
        if perm_ctx is None:
            return
        for rule_content in rules_str.split(","):
            rule_content = rule_content.strip()
            if rule_content:
                rule_value = PermissionRuleValue(tool_name=tool_name, rule_content=rule_content)
                perm_ctx = apply_session_rule(perm_ctx, behavior, rule_value)
        self._set_permission_context(perm_ctx)

    async def _request_permission(self, event: PermissionRequestEvent) -> bool:
        tool_name = event.tool_name
        is_non_read_only = is_permission_audit_non_read_only(event)

        def _rule_from_option(option_id: str, prefix: str) -> str | None:
            rules_str = option_id[len(prefix) :]
            if not rules_str:
                return None
            return ", ".join(f"{tool_name}({rule.strip()})" for rule in rules_str.split(",") if rule.strip())

        def _prompt_option_reason(option_id: str) -> str:
            if option_id.startswith(_PREFIX_ALLOW_RULE):
                return _PREFIX_ALLOW_RULE.rstrip(":")
            if option_id.startswith(_PREFIX_DENY_RULE):
                return _PREFIX_DENY_RULE.rstrip(":")
            return option_id

        # Check permission cache first; helper marks the entry as recently-used.
        cached = lookup_permission(self._permission_cache, tool_name)
        if cached == "always_allow" and tool_name == "aliyun_api" and is_non_read_only:
            self._permission_cache.pop(tool_name, None)
            cached = None
        cached_audit_ok = True
        if cached in ("always_allow", "always_deny"):
            cached_audit_ok = emit_permission_boundary_audit(
                event,
                session_id=self.id,
                decision="allow" if cached == "always_allow" else "deny",
                scope="tool_cache",
                source="acp_tool_cache",
                rule_source="tool_cache",
                reason_type="tool_cache",
                reason_detail=cached,
            )
        if cached == "always_allow":
            if not cached_audit_ok and should_fail_closed_permission_audit(event, "allow"):
                return False
            logger.debug("Permission auto-allowed for tool %s (cached)", tool_name)
            return True
        if cached == "always_deny":
            logger.debug("Permission auto-denied for tool %s (cached)", tool_name)
            return False

        # Extract suggestions from permission_result for rule-level options.
        suggestions = []
        if (
            event.permission_result is not None
            and hasattr(event.permission_result, "suggestions")
            and event.permission_result.suggestions
        ):
            suggestions = event.permission_result.suggestions

        # Build dynamic option list aligned with local REPL behavior.
        options: list[acp.schema.PermissionOption] = [
            acp.schema.PermissionOption(
                option_id=_OPTION_ALLOW_ONCE,
                name=_("Allow once"),
                kind="allow_once",
            ),
        ]

        if suggestions:
            rules_display = ",".join(s.rule_content for s in suggestions)
            options.append(
                acp.schema.PermissionOption(
                    option_id=_PREFIX_ALLOW_RULE + rules_display,
                    name=_('Always allow "{rule}" (this session)').format(rule=rules_display),
                    kind="allow_always",
                )
            )
        elif _tool_supports_blanket_allow(self.agent_loop, tool_name):
            options.append(
                acp.schema.PermissionOption(
                    option_id=_OPTION_ALLOW_ALWAYS,
                    name=_("Always allow this tool"),
                    kind="allow_always",
                )
            )

        options.append(
            acp.schema.PermissionOption(
                option_id=_OPTION_REJECT_ONCE,
                name=_("Reject once"),
                kind="reject_once",
            )
        )

        if suggestions:
            rules_display = ",".join(s.rule_content for s in suggestions)
            options.append(
                acp.schema.PermissionOption(
                    option_id=_PREFIX_DENY_RULE + rules_display,
                    name=_('Always deny "{rule}" (this session)').format(rule=rules_display),
                    kind="reject_always",
                )
            )

        options.append(
            acp.schema.PermissionOption(
                option_id=_OPTION_REJECT_ALWAYS,
                name=_("Always reject this tool"),
                kind="reject_always",
            ),
        )
        offered_option_ids = {option.option_id for option in options}

        # Build content with command details and suggested rule.
        tool_title = display_tool_title(tool_name)
        if tool_name == "aliyun_api":
            input_summary = json.dumps(
                build_input_summary(tool_name, event.tool_input),
                ensure_ascii=False,
                sort_keys=True,
            )
            content_text = _("Approve tool call: {tool}\nInput summary: {summary}").format(
                tool=tool_title,
                summary=input_summary,
            )
        else:
            tool_input_redacted = json.dumps(
                build_prompt_tool_input(event.tool_input),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            content_text = _("Approve tool call: {tool}\nInput: {input}").format(
                tool=tool_title,
                input=tool_input_redacted,
            )
        if suggestions:
            content_text += "\n" + _("Suggested rule: {rule}").format(
                rule=",".join(s.rule_content for s in suggestions)
            )

        response = await self._conn.request_permission(
            options,
            self.id,
            acp.schema.ToolCallUpdate(
                tool_call_id="permission/{}".format(event.tool_use_id),
                title=tool_title,
                content=[
                    acp.schema.ContentToolCallContent(
                        type="content",
                        content=acp.schema.TextContentBlock(
                            type="text",
                            text=content_text,
                        ),
                    )
                ],
            ),
        )

        # Interpret the outcome and update permission state.
        if isinstance(response.outcome, acp.schema.AllowedOutcome):
            option_id = response.outcome.option_id or _OPTION_ALLOW_ONCE
            if option_id not in offered_option_ids:
                emit_permission_boundary_audit(
                    event,
                    session_id=self.id,
                    decision="deny",
                    scope="once",
                    source="acp_prompt",
                    reason_type="invalid_option",
                    reason_detail="invalid_option",
                )
                return False
            audit_scope = "once"
            audit_rule = None
            cache_decision: PermissionDecision | None = None
            allow_rules_str: str | None = None
            if option_id == _OPTION_ALLOW_ALWAYS:
                cache_decision = "always_allow"
                audit_scope = "tool_cache"
            elif option_id and option_id.startswith(_PREFIX_ALLOW_RULE):
                allow_rules_str = option_id[len(_PREFIX_ALLOW_RULE) :]
                audit_scope = "session_rule"
                audit_rule = _rule_from_option(option_id, _PREFIX_ALLOW_RULE)
            audit_ok = emit_permission_boundary_audit(
                event,
                session_id=self.id,
                decision="allow",
                scope=audit_scope,
                source="acp_prompt",
                reason_type="prompt_selection",
                reason_detail=_prompt_option_reason(option_id),
                rule=audit_rule,
            )
            if not audit_ok and should_fail_closed_permission_audit(event, "allow"):
                return False
            if cache_decision is not None:
                self._cache_permission(tool_name, cache_decision)
            if allow_rules_str is not None:
                self._apply_rule(tool_name, allow_rules_str, "allow")
            return True

        # DeniedOutcome — parse option_id from meta or direct field.
        if isinstance(response.outcome, acp.schema.DeniedOutcome):
            option_id = getattr(response.outcome, "option_id", None)
            if option_id is None:
                resp_meta = getattr(response, "field_meta", None) or {}
                option_id = resp_meta.get("option_id")
            option_id = option_id or _OPTION_REJECT_ONCE
            if option_id not in offered_option_ids:
                option_id = _OPTION_REJECT_ONCE

            audit_scope = "once"
            audit_rule = None
            if option_id == _OPTION_REJECT_ALWAYS:
                self._cache_permission(tool_name, "always_deny")
                audit_scope = "tool_cache"
            elif option_id and option_id.startswith(_PREFIX_DENY_RULE):
                rules_str = option_id[len(_PREFIX_DENY_RULE) :]
                self._apply_rule(tool_name, rules_str, "deny")
                audit_scope = "session_rule"
                audit_rule = _rule_from_option(option_id, _PREFIX_DENY_RULE)
            emit_permission_boundary_audit(
                event,
                session_id=self.id,
                decision="deny",
                scope=audit_scope,
                source="acp_prompt",
                reason_type="prompt_selection",
                reason_detail=_prompt_option_reason(option_id),
                rule=audit_rule,
            )

        return False

    def _cache_permission(self, tool_name: str, decision: PermissionDecision) -> None:
        """Record a sticky permission decision via the shared helper."""
        record_permission(self._permission_cache, tool_name, decision)
