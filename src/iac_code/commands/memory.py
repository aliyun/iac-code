"""Memory commands."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from iac_code.agent.system_prompt import build_system_prompt
from iac_code.i18n import _
from iac_code.memory.memory_manager import MemoryManager
from iac_code.memory.project_memory import ProjectMemoryRuntime, is_auto_memory_enabled, save_auto_memory_enabled
from iac_code.utils.file_security import ensure_private_dir, ensure_private_file

MEMORY_USAGE = _("Usage: /memory-folder [<name>|search <query>|delete <name>|help]")
_RESERVED_SUBCOMMANDS = {"search", "delete", "help"}

if TYPE_CHECKING:
    from iac_code.ui.dialogs.memory_editor import MemoryEditResult


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


async def memory_folder_command(**kwargs) -> str:
    context = kwargs.get("context")
    repl = getattr(context, "repl", None) if context is not None else None
    memory_manager = getattr(repl, "_legacy_memory_manager", None) or getattr(repl, "_memory_manager", None)
    if memory_manager is None:
        return _("Memory manager is unavailable.")
    return execute_memory_command(memory_manager, kwargs.get("args") or [])


async def memory_command(**kwargs) -> str | None:
    context = kwargs.get("context")
    repl = getattr(context, "repl", None) if context is not None else None
    runtime = getattr(repl, "_memory_runtime", None)
    if runtime is None:
        return _("Memory runtime is unavailable.")

    initial_action: str | None = None
    while True:
        action = _select_memory_action(
            runtime,
            auto_memory_enabled=is_auto_memory_enabled(),
            initial_action=initial_action,
            on_toggle=save_auto_memory_enabled,
        )
        if action is None:
            return None

        try:
            if action == "project":
                path = runtime.ensure_instruction_file("project")
                result = _edit_memory_file(path, _("Project memory"))
                return _handle_instruction_edit_result(
                    result,
                    path=path,
                    refresh_target=repl,
                    scope_label=_("project memory"),
                    private_file=False,
                )
            if action == "user":
                path = runtime.ensure_instruction_file("user")
                result = _edit_memory_file(path, _("User memory"))
                return _handle_instruction_edit_result(
                    result,
                    path=path,
                    refresh_target=repl,
                    scope_label=_("user memory"),
                    private_file=True,
                )
            if action == "folder":
                path = runtime.ensure_auto_memory_dir()
                _open_folder(path)
                initial_action = "folder"
                continue
        except Exception as exc:
            return _("Failed to open memory: {error}").format(error=exc)


def _select_memory_action(
    runtime: ProjectMemoryRuntime,
    *,
    auto_memory_enabled: bool,
    initial_action: str | None = None,
    on_toggle: Callable[[bool], None] | None = None,
) -> str | None:
    from iac_code.ui.dialogs.memory import MemoryDialog

    return MemoryDialog(
        project_path=runtime.project_instruction_path,
        user_path=runtime.user_instruction_path,
        auto_memory_dir=runtime.auto_memory_dir,
        auto_memory_enabled=auto_memory_enabled,
        initial_focus_action=initial_action,
        on_toggle=on_toggle,
    ).run()


def _edit_memory_file(path: Path, title: str) -> "MemoryEditResult":
    from iac_code.ui.dialogs.memory_editor import VimMemoryEditor

    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        content = ""
    return VimMemoryEditor(content, title=title, path=str(path)).run()


def _handle_instruction_edit_result(
    result: "MemoryEditResult",
    *,
    path: Path,
    refresh_target: object | None,
    scope_label: str,
    private_file: bool,
) -> str | None:
    if result.status == "cancelled":
        return None
    if result.status == "unchanged":
        return _("No changes made to {scope}: {path}").format(scope=scope_label, path=path)
    if result.status == "saved":
        if private_file:
            ensure_private_dir(path.parent)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result.content, encoding="utf-8", newline="\n")
        if private_file:
            ensure_private_file(path)
        _refresh_repl_memory_context(refresh_target)
        return _("Saved {scope}: {path}").format(scope=scope_label, path=path)
    return None


def _open_folder(path: Path) -> None:
    _open_path(path)


def _open_path(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=True)
        return
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
        return
    subprocess.run(["xdg-open", str(path)], check=True)


def _refresh_repl_memory_context(repl: object | None) -> None:
    if repl is None:
        return
    refresh_system_prompt = getattr(repl, "_refresh_system_prompt", None)
    if callable(refresh_system_prompt):
        refresh_system_prompt()
        return
    refresh_memory_context = getattr(repl, "_refresh_memory_context", None)
    if not callable(refresh_memory_context):
        return
    memory_context = refresh_memory_context()
    agent_loop = getattr(repl, "_agent_loop", None)
    provider_manager = getattr(repl, "_provider_manager", None)
    if agent_loop is None or provider_manager is None:
        return
    cwd = getattr(repl, "_original_cwd", os.getcwd())
    skill_listing = getattr(repl, "_skill_listing", "")
    current_time = getattr(repl, "_runtime_current_time", None)
    agent_loop.set_provider(
        provider_manager,
        system_prompt=build_system_prompt(
            cwd=cwd,
            memory_context=memory_context,
            skill_listing=skill_listing,
            current_time=current_time if isinstance(current_time, str) else None,
        ),
    )
