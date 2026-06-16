"""Project-scoped memory paths and prompt context."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from iac_code.config import _load_yaml, _save_yaml, get_config_dir, get_settings_path
from iac_code.memory.memory_manager import MemoryManager
from iac_code.utils.file_security import ensure_private_dir
from iac_code.utils.project_paths import find_git_worktree_root, sanitize_path

DEFAULT_INSTRUCTION_MEMORY_FILE = "AGENTS.md"
INSTRUCTION_MEMORY_FILE = DEFAULT_INSTRUCTION_MEMORY_FILE
INSTRUCTION_MEMORY_FILE_ENV = "IAC_CODE_INSTRUCTION_MEMORY_FILE"
_MEMORY_SETTINGS_KEY = "memory"
_AUTO_MEMORY_SETTINGS_KEY = "autoMemory"


@dataclass(frozen=True)
class MemoryContext:
    instruction_memory_content: str = ""
    memory_index_content: str = ""
    memory_mechanics_content: str = ""

    def has_content(self) -> bool:
        return any(
            (
                self.instruction_memory_content.strip(),
                self.memory_mechanics_content.strip(),
            )
        )


def resolve_project_root(cwd: str) -> Path:
    git_root = find_git_worktree_root(cwd)
    if git_root is not None:
        return git_root
    path = Path(cwd).expanduser()
    if not path.is_absolute():
        path = Path(os.path.abspath(str(path)))
    return Path(os.path.normpath(str(path)))


def project_key_for_cwd(cwd: str) -> str:
    return sanitize_path(str(resolve_project_root(cwd)))


def get_project_memory_dir(cwd: str) -> Path:
    return get_config_dir() / "projects" / project_key_for_cwd(cwd) / "memory"


class ProjectMemoryRuntime:
    def __init__(self, cwd: str):
        self.project_root = resolve_project_root(cwd)
        self.instruction_memory_file = get_instruction_memory_file_name()
        self.user_instruction_path = get_config_dir() / self.instruction_memory_file
        self.project_instruction_path = self.project_root / self.instruction_memory_file
        self.auto_memory_dir = get_project_memory_dir(cwd)
        self.memory_manager = MemoryManager(memory_dir=str(self.auto_memory_dir))

    def ensure_instruction_file(self, scope: str) -> Path:
        if scope == "user":
            return self.user_instruction_path
        elif scope == "project":
            return self.project_instruction_path
        else:
            raise ValueError(f"Invalid memory scope: {scope}")

    def ensure_auto_memory_dir(self) -> Path:
        return ensure_private_dir(self.auto_memory_dir)

    def build_memory_context(self) -> MemoryContext:
        instruction_content = self._build_instruction_memory_content()
        return MemoryContext(
            instruction_memory_content=instruction_content,
            memory_mechanics_content=_memory_mechanics_content(
                is_auto_memory_enabled(),
                instruction_memory_file=self.instruction_memory_file,
            ),
        )

    def _build_instruction_memory_content(self) -> str:
        parts: list[str] = []
        for label, path in (
            (f"User {self.instruction_memory_file}", self.user_instruction_path),
            (f"Project {self.instruction_memory_file}", self.project_instruction_path),
        ):
            content = _read_text_if_present(path)
            if content:
                parts.append(f"## {label}\n{content}")
        return "\n\n".join(parts)


def get_instruction_memory_file_name() -> str:
    configured = os.environ.get(INSTRUCTION_MEMORY_FILE_ENV, "").strip()
    if not configured:
        return DEFAULT_INSTRUCTION_MEMORY_FILE
    if "/" in configured or "\\" in configured:
        return DEFAULT_INSTRUCTION_MEMORY_FILE
    if configured in {"", ".", ".."}:
        return DEFAULT_INSTRUCTION_MEMORY_FILE
    return configured


def _read_text_if_present(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def is_auto_memory_enabled() -> bool:
    settings = _load_yaml(get_settings_path())
    memory_settings = settings.get(_MEMORY_SETTINGS_KEY)
    if not isinstance(memory_settings, dict):
        return True
    enabled = memory_settings.get(_AUTO_MEMORY_SETTINGS_KEY)
    return enabled if isinstance(enabled, bool) else True


def save_auto_memory_enabled(enabled: bool) -> None:
    settings = _load_yaml(get_settings_path())
    memory_settings = settings.get(_MEMORY_SETTINGS_KEY)
    if not isinstance(memory_settings, dict):
        memory_settings = {}
    memory_settings[_AUTO_MEMORY_SETTINGS_KEY] = bool(enabled)
    settings[_MEMORY_SETTINGS_KEY] = memory_settings
    _save_yaml(get_settings_path(), settings)


def _memory_mechanics_content(auto_memory_enabled: bool, *, instruction_memory_file: str) -> str:
    auto_memory_line = (
        "- Auto-memory is on; topic memories may be recalled and updated when the user asks."
        if auto_memory_enabled
        else "- Auto-memory is off; do not use write_memory and topic memories are not automatically recalled."
    )
    return "\n".join(
        [
            "Use memory carefully:",
            f"- {instruction_memory_file} files contain always-on user and project instructions.",
            "- MEMORY.md is the project auto-memory topic index; it is used by side recall and is not always injected.",
            auto_memory_line,
            "- Topic files are selected by side recall and injected as hidden conversation context when relevant.",
            "- Use read_memory to inspect relevant topics that were not automatically recalled.",
            "- Use write_memory only when the user explicitly asks to remember or preserve information.",
            "- Treat recalled memories as potentially stale and verify before relying on time-sensitive facts.",
        ]
    )
