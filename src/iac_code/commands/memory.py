"""Memory command - view and manage persistent memories."""

from __future__ import annotations

from typing import Any

from iac_code.i18n import _
from iac_code.memory.memory_manager import MemoryManager

MEMORY_USAGE = _("Usage: /memory [<name>|search <query>|delete <name>|help]")
_RESERVED_SUBCOMMANDS = {"search", "delete", "help"}


def _format_summary(title: str, memories: list[dict[str, Any]]) -> str:
    if not memories:
        return ""

    lines = [title]
    for memory in sorted(memories, key=lambda item: str(item.get("name", ""))):
        lines.append(
            "  - {name} - {description}".format(
                name=memory.get("name", ""),
                description=memory.get("description", ""),
            )
        )
    return "\n".join(lines)


def _format_memory(memory: dict[str, Any]) -> str:
    return "[{type}] {description}\n\n{content}".format(
        type=memory.get("type", ""),
        description=memory.get("description", ""),
        content=memory.get("content", ""),
    )


def execute_memory_command(memory_manager: MemoryManager, args: list[str]) -> str:
    if not args:
        memories = memory_manager.list_memories()
        return _format_summary(_("Saved memories:"), memories) or _("No memories saved yet.")

    action = args[0].lower()
    if action == "help":
        return MEMORY_USAGE

    if action == "search":
        query = " ".join(args[1:]).strip()
        if not query:
            return MEMORY_USAGE
        matches = memory_manager.search(query)
        return _format_summary(_("Matching memories:"), matches) or _("No matching memories.")

    if action == "delete":
        if len(args) != 2:
            return MEMORY_USAGE
        name = args[1]
        try:
            existing = memory_manager.load(name)
            if existing is None:
                return _("Memory '{name}' not found.").format(name=name)
            memory_manager.delete(name)
        except ValueError as exc:
            return str(exc)
        return _("Memory '{name}' deleted.").format(name=name)

    if len(args) != 1 or action in _RESERVED_SUBCOMMANDS:
        return MEMORY_USAGE

    name = args[0]
    try:
        memory = memory_manager.load(name)
    except ValueError as exc:
        return str(exc)
    if memory is None:
        return _("Memory '{name}' not found.").format(name=name)
    return _format_memory(memory)


async def memory_command(**kwargs) -> str:
    context = kwargs.get("context")
    repl = getattr(context, "repl", None) if context is not None else None
    memory_manager = getattr(repl, "_memory_manager", None)
    if memory_manager is None:
        return _("Memory manager is unavailable.")
    return execute_memory_command(memory_manager, kwargs.get("args") or [])
