"""Compact command - compresses conversation context."""

from __future__ import annotations

from iac_code.i18n import _


async def compact_command(**kwargs) -> str:
    """Compact conversation context by summarizing history."""
    context = kwargs.get("context")
    if context is None:
        return _("Compact command requires a context.")

    repl = getattr(context, "repl", None)
    if repl is None:
        return _("No active REPL.")

    agent_loop = getattr(repl, "_agent_loop", None)
    if agent_loop is None:
        return _("No active agent loop.")

    result = await agent_loop.compact()
    if result.status == "empty":
        return _("Nothing to compact: conversation is empty.")
    if result.status == "too_short":
        return _(
            "Conversation too short to compact: all messages are within the recent {turns}-turn preservation window."
        ).format(turns=result.preserve_recent_turns)
    if result.status == "failed":
        return _("Compaction failed. See logs for details.")

    usage_after = agent_loop.get_context_usage()
    percent = (1 - result.compacted_tokens / result.original_tokens) * 100 if result.original_tokens > 0 else 0
    return _(
        "Context compacted: {original} → {compacted} tokens "
        "({percent_display} reduction). "
        "Context usage: {usage_display}"
    ).format(
        original=result.original_tokens,
        compacted=result.compacted_tokens,
        percent_display=f"{percent:.0f}%",
        usage_display=f"{usage_after['usage_percent']:.0f}%",
    )
