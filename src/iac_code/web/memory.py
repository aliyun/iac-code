"""Web API helpers for memory inspection and management."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from iac_code.config import get_config_dir
from iac_code.memory.memory_manager import MemoryManager
from iac_code.memory.project_memory import (
    ProjectMemoryRuntime,
    get_project_memory_dir,
    is_auto_memory_enabled,
    save_auto_memory_enabled,
)
from iac_code.utils.file_security import atomic_write_text, ensure_private_dir, ensure_private_file
from iac_code.utils.state_io import atomic_write_text as atomic_write_project_text


def memory_payload(cwd: Path) -> dict[str, Any]:
    """Return memory state for the project rooted at *cwd* without unrelated content."""
    runtime = ProjectMemoryRuntime(str(cwd))
    return {
        "project": _instruction_payload(runtime.project_instruction_path),
        "user": _instruction_payload(runtime.user_instruction_path),
        "autoMemoryEnabled": is_auto_memory_enabled(),
        "legacy": legacy_memory_summaries("", cwd),
    }


def _resolve_cwd(cwd: Path) -> Path:
    return cwd.expanduser().resolve(strict=False)


def memory_projects(current_cwd: Path, project_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Selectable projects for the memory panel.

    The launch directory *current_cwd* is always first and flagged ``current``; the
    remaining entries come from known session projects, deduplicated by resolved path.
    """
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    current = _resolve_cwd(current_cwd)
    current_key = str(current)
    result.append({"cwd": current_key, "label": current.name or current_key, "current": True})
    seen.add(current_key)

    for entry in project_entries:
        raw = str(entry.get("cwd") or "").strip()
        if not raw:
            continue
        # Session-project groups store a real absolute working directory; storage-only
        # folders (e.g. the launch dir's own project-memory dir) surface as a slug name,
        # which is not a real cwd and would resolve to a phantom path under the launch dir.
        if not Path(raw).is_absolute():
            continue
        resolved = _resolve_cwd(Path(raw))
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        label = str(entry.get("label") or "").strip() or resolved.name or key
        result.append({"cwd": key, "label": label, "current": False})
    return result


def allowed_project_cwds(current_cwd: Path, project_entries: list[dict[str, Any]]) -> set[str]:
    """Resolved paths the memory panel may read or write, matching :func:`memory_projects`."""
    return {str(item["cwd"]) for item in memory_projects(current_cwd, project_entries)}


def resolve_project_cwd(cwd_param: str, current_cwd: Path, project_entries: list[dict[str, Any]]) -> Path | None:
    """Return the resolved cwd if *cwd_param* is a known project, otherwise ``None``."""
    if not cwd_param.strip():
        return None
    resolved = _resolve_cwd(Path(cwd_param))
    if str(resolved) in allowed_project_cwds(current_cwd, project_entries):
        return resolved
    return None


def save_project_instruction(cwd: Path, content: str) -> dict[str, Any]:
    runtime = ProjectMemoryRuntime(str(cwd.expanduser().resolve(strict=False)))
    path = runtime.ensure_instruction_file("project")
    _write_project_file(path, content)
    return _saved_instruction_payload(path, content)


def save_user_instruction(content: str) -> dict[str, Any]:
    runtime = ProjectMemoryRuntime(str(Path.cwd()))
    path = runtime.ensure_instruction_file("user")
    if path.is_symlink():
        raise ValueError("user memory path is invalid")
    ensure_private_dir(path.parent)
    atomic_write_text(path, content, encoding="utf-8")
    ensure_private_file(path)
    return _saved_instruction_payload(path, content)


def save_auto_memory(enabled: bool) -> dict[str, bool]:
    save_auto_memory_enabled(enabled)
    return {"autoMemoryEnabled": is_auto_memory_enabled()}


def _project_memory_manager(cwd: Path | None) -> MemoryManager | None:
    """Manager for *cwd*'s project memory, or ``None`` if it has no memory dir yet.

    Constructing a :class:`MemoryManager` creates its directory, so we only build one
    when the project already has memories — browsing a project must not leave empty
    ``projects/<key>/memory`` folders behind.
    """
    if cwd is None:
        return None
    project_dir = get_project_memory_dir(str(cwd))
    if not project_dir.is_dir():
        return None
    return MemoryManager(str(project_dir))


def _summaries_from(manager: MemoryManager, query: str, scope: str) -> list[dict[str, str]]:
    memories = manager.search(query) if query.strip() else manager.list_memory_metadata()
    summaries: list[dict[str, str]] = []
    for memory in memories:
        summary = _legacy_summary(memory, scope)
        if summary is not None:
            summaries.append(summary)
    return summaries


def legacy_memory_summaries(query: str, cwd: Path | None = None) -> list[dict[str, str]]:
    """Structured memories for the panel: the selected project's, then the global ones."""
    summaries: list[dict[str, str]] = []
    project_manager = _project_memory_manager(cwd)
    if project_manager is not None:
        summaries.extend(_summaries_from(project_manager, query, "project"))
    global_manager = MemoryManager(str(get_config_dir() / "memory"))
    summaries.extend(_summaries_from(global_manager, query, "global"))
    return summaries


def delete_legacy_memory(memory_id: str, cwd: Path | None = None, scope: str = "global") -> bool:
    if scope == "project":
        manager = _project_memory_manager(cwd)
        if manager is None:
            return False
    else:
        manager = MemoryManager(str(get_config_dir() / "memory"))
    if manager.load(memory_id) is None:
        return False
    manager.delete(memory_id)
    return True


def _instruction_payload(path: Path) -> dict[str, str]:
    return {"path": str(path), "content": _read_text_if_present(path)}


def _saved_instruction_payload(path: Path, content: str) -> dict[str, Any]:
    return {"path": str(path), "content": content, "updated": True}


def _read_text_if_present(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_project_file(path: Path, content: str) -> None:
    if path.is_symlink():
        raise ValueError("project memory path is invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_project_text(path, content, encoding="utf-8")


def _legacy_summary(memory: dict[str, Any], scope: str) -> dict[str, str] | None:
    name = str(memory.get("name") or "").strip()
    if not name:
        return None
    description = str(memory.get("description") or "").strip()
    memory_type = str(memory.get("type") or "").strip()
    return {
        "memoryId": name,
        "name": name,
        "description": description,
        "type": memory_type,
        "summary": description,
        "scope": scope,
    }
