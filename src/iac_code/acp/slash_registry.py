"""ACP slash command registry.

Manages commands supported over the ACP protocol.
Only /compact, /clear, /debug, and /memory are allowed;
all other slash commands are rejected with a clear message.
"""

from __future__ import annotations

import logging

from iac_code.i18n import _

logger = logging.getLogger(__name__)

ACP_SUPPORTED_COMMANDS: frozenset[str] = frozenset({"compact", "clear", "debug", "memory"})


class ACPSlashRegistry:
    """Registry for ACP-supported slash commands.

    Parses incoming text for slash command patterns, validates them against the
    supported set, dispatches execution, and returns plain-text results.
    """

    def is_slash_command(self, text: str) -> bool:
        """Return True if *text* starts with a slash command pattern."""
        stripped = text.strip()
        return stripped.startswith("/") and len(stripped) > 1 and not stripped.startswith("//")

    async def execute(self, text: str, agent_loop, **context) -> str:
        """Execute a slash command and return the result text.

        If the command is not in :data:`ACP_SUPPORTED_COMMANDS`, returns a
        rejection message listing available commands.
        """
        stripped = text.strip()
        parts = stripped[1:].split(None, 1)
        cmd_name = parts[0].lower() if parts else ""
        args_str = parts[1] if len(parts) > 1 else ""

        if cmd_name not in ACP_SUPPORTED_COMMANDS:
            supported = ", ".join(f"/{c}" for c in sorted(ACP_SUPPORTED_COMMANDS))
            return _("Command '/{cmd_name}' is not supported over ACP. Supported commands: {supported}").format(
                cmd_name=cmd_name, supported=supported
            )

        if cmd_name == "compact":
            return await self._handle_compact(agent_loop)
        if cmd_name == "clear":
            return await self._handle_clear(agent_loop)
        if cmd_name == "debug":
            return self._handle_debug(args_str)
        if cmd_name == "memory":
            return self._handle_memory(args_str, context.get("memory_manager"))

        # Should not reach here
        return _("Command '/{cmd_name}' handler not implemented.").format(cmd_name=cmd_name)  # pragma: no cover

    # ------------------------------------------------------------------
    # Individual command handlers
    # ------------------------------------------------------------------

    async def _handle_compact(self, agent_loop) -> str:
        """Invoke agent_loop.compact() and return a summary."""
        try:
            result = await agent_loop.compact()
        except Exception as exc:
            logger.warning("ACP /compact failed: %s", exc)
            return _("Compaction failed: {error}").format(error=exc)

        if result.status == "empty":
            return _("Nothing to compact: conversation is empty.")
        if result.status == "too_short":
            return _(
                "Conversation too short to compact: all messages are within "
                "the recent {turns}-turn preservation window."
            ).format(turns=result.preserve_recent_turns)
        if result.status == "failed":
            return _("Compaction failed. See logs for details.")

        usage_after = agent_loop.get_context_usage()
        percent = (1 - result.compacted_tokens / result.original_tokens) * 100 if result.original_tokens > 0 else 0
        return _(
            "Context compacted: {original} → {compacted} tokens ({percent} reduction). Context usage: {usage}"
        ).format(
            original=result.original_tokens,
            compacted=result.compacted_tokens,
            percent=f"{percent:.0f}%",
            usage=f"{usage_after['usage_percent']:.0f}%",
        )

    async def _handle_clear(self, agent_loop) -> str:
        """Clear the agent_loop conversation history."""
        try:
            agent_loop.reset()
        except Exception as exc:
            logger.warning("ACP /clear failed: %s", exc)
            return _("Clear failed: {error}").format(error=exc)
        return _("Conversation history cleared.")

    def _handle_debug(self, args: str) -> str:
        """Toggle debug logging based on args ('on'/'off'/empty)."""
        from iac_code.utils.log import (
            current_log_file,
            disable_debug_at_runtime,
            enable_debug_at_runtime,
            is_debug_enabled,
        )

        action = args.strip().lower()

        if action in ("", "status"):
            if is_debug_enabled():
                log_path = current_log_file()
                return _("Debug logging is on. Log file: {path}").format(path=log_path)
            return _("Debug logging is off.")

        if action == "on":
            log_path = enable_debug_at_runtime("acp")
            return _("Debug logging enabled. Log file: {path}").format(path=log_path)

        if action == "off":
            disable_debug_at_runtime()
            return _("Debug logging disabled.")

        return _("Usage: /debug [on|off]")

    def _handle_memory(self, args: str, memory_manager) -> str:
        """View and manage persistent memories."""
        if memory_manager is None:
            return _("Memory manager is unavailable.")

        from iac_code.commands.memory import execute_memory_command

        return execute_memory_command(memory_manager, args.split())
