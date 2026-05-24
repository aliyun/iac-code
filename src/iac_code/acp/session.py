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

from iac_code.acp.convert import ACPEventConverter, acp_blocks_to_prompt_text
from iac_code.acp.metrics import ACPMetrics
from iac_code.acp.slash_registry import ACPSlashRegistry
from iac_code.acp.state import TurnState
from iac_code.acp.tools import ACPTerminalBashTool
from iac_code.acp.types import ACPContentBlock
from iac_code.agent.message import Message, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock
from iac_code.state.app_state import lookup_permission, record_permission
from iac_code.types.permissions import PermissionDecision
from iac_code.types.stream_events import PermissionRequestEvent

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


def _history_message_to_updates(msg: Message) -> list[Any]:
    """Convert a single persisted *Message* to a list of ACP session updates.

    * **user** messages become ``UserMessageUpdate`` (ACP "user_message").
    * **assistant** text / thinking become ``AgentMessageChunk`` / ``AgentThoughtChunk``.
    * **assistant** tool-use blocks become ``ToolCallStart`` then a completed
      ``ToolCallProgress``.
    * **user** tool-result blocks are emitted as completed ``ToolCallProgress``.
    """
    updates: list[Any] = []
    content = msg.content

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
                updates.append(
                    acp.schema.ToolCallProgress(
                        session_update="tool_call_update",
                        tool_call_id=block.tool_use_id,
                        status=status,
                        content=[
                            acp.schema.ContentToolCallContent(
                                type="content",
                                content=acp.schema.TextContentBlock(type="text", text=block.content),
                            )
                        ],
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
            updates.append(
                acp.schema.ToolCallStart(
                    session_update="tool_call",
                    tool_call_id=block.id,
                    title=block.name,
                    status="completed",
                )
            )
            input_text = json.dumps(block.input, ensure_ascii=False) if block.input else ""
            updates.append(
                acp.schema.ToolCallProgress(
                    session_update="tool_call_update",
                    tool_call_id=block.id,
                    status="completed",
                    content=[
                        acp.schema.ContentToolCallContent(
                            type="content",
                            content=acp.schema.TextContentBlock(type="text", text=input_text),
                        )
                    ],
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


class ACPSession:
    def __init__(
        self,
        session_id: str,
        agent_loop,
        conn: acp.Client,
        mcp_configs: list[dict] | None = None,
        metrics: ACPMetrics | None = None,
    ) -> None:
        self.id = session_id
        self.agent_loop = agent_loop
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
        # MCP server configs passed from the client (internal dict format)
        # TODO: Wire into agent tool registry when MCP tool integration is implemented
        self.mcp_configs: list[dict] = mcp_configs or []
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
        for i, msg in enumerate(messages):
            updates = _history_message_to_updates(msg)
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
        if slash_registry.is_slash_command(prompt_text):
            result = await slash_registry.execute(prompt_text, self.agent_loop)
            await self._conn.session_update(
                session_id=self.id,
                update=acp.schema.AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=acp.schema.TextContentBlock(type="text", text=result),
                ),
            )
            return acp.PromptResponse(stop_reason="end_turn")

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
            async for event in self.agent_loop.run_streaming(prompt_text):
                if isinstance(event, PermissionRequestEvent):
                    allowed = await self._request_permission(event)
                    if event.response_future is not None and not event.response_future.done():
                        event.response_future.set_result(allowed)
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
            raise acp.RequestError.internal_error({"error": str(exc)}) from exc
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

        # Check permission cache first; helper marks the entry as recently-used.
        cached = lookup_permission(self._permission_cache, tool_name)
        if cached == "always_allow":
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
                name="Allow once",
                kind="allow_once",
            ),
        ]

        if suggestions:
            rules_display = ",".join(s.rule_content for s in suggestions)
            options.append(
                acp.schema.PermissionOption(
                    option_id=_PREFIX_ALLOW_RULE + rules_display,
                    name='Always allow "{}" (this session)'.format(rules_display),
                    kind="allow_always",
                )
            )
        else:
            options.append(
                acp.schema.PermissionOption(
                    option_id=_OPTION_ALLOW_ALWAYS,
                    name="Always allow this tool",
                    kind="allow_always",
                )
            )

        options.append(
            acp.schema.PermissionOption(
                option_id=_OPTION_REJECT_ONCE,
                name="Reject once",
                kind="reject_once",
            )
        )

        if suggestions:
            rules_display = ",".join(s.rule_content for s in suggestions)
            options.append(
                acp.schema.PermissionOption(
                    option_id=_PREFIX_DENY_RULE + rules_display,
                    name='Always deny "{}" (this session)'.format(rules_display),
                    kind="reject_always",
                )
            )

        options.append(
            acp.schema.PermissionOption(
                option_id=_OPTION_REJECT_ALWAYS,
                name="Always reject this tool",
                kind="reject_always",
            ),
        )

        # Build content with command details and suggested rule.
        content_text = "Approve tool call: {}\nInput: {}".format(tool_name, event.tool_input)
        if suggestions:
            content_text += "\nSuggested rule: {}".format(",".join(s.rule_content for s in suggestions))

        response = await self._conn.request_permission(
            options,
            self.id,
            acp.schema.ToolCallUpdate(
                tool_call_id="permission/{}".format(event.tool_use_id),
                title=event.tool_name,
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
            option_id = response.outcome.option_id
            if option_id == _OPTION_ALLOW_ALWAYS:
                self._cache_permission(tool_name, "always_allow")
            elif option_id and option_id.startswith(_PREFIX_ALLOW_RULE):
                rules_str = option_id[len(_PREFIX_ALLOW_RULE) :]
                self._apply_rule(tool_name, rules_str, "allow")
            return True

        # DeniedOutcome — parse option_id from meta or direct field.
        if isinstance(response.outcome, acp.schema.DeniedOutcome):
            option_id = getattr(response.outcome, "option_id", None)
            if option_id is None:
                resp_meta = getattr(response, "field_meta", None) or {}
                option_id = resp_meta.get("option_id")

            if option_id == _OPTION_REJECT_ALWAYS:
                self._cache_permission(tool_name, "always_deny")
            elif option_id and option_id.startswith(_PREFIX_DENY_RULE):
                rules_str = option_id[len(_PREFIX_DENY_RULE) :]
                self._apply_rule(tool_name, rules_str, "deny")

        return False

    def _cache_permission(self, tool_name: str, decision: PermissionDecision) -> None:
        """Record a sticky permission decision via the shared helper."""
        record_permission(self._permission_cache, tool_name, decision)
