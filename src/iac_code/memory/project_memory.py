"""Project-scoped memory paths and prompt context."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from iac_code.config import _load_yaml, _save_yaml, get_config_dir, get_settings_path
from iac_code.memory.memory_manager import MemoryManager
from iac_code.utils.file_security import ensure_private_dir
from iac_code.utils.project_paths import find_git_worktree_root, sanitize_path

INSTRUCTION_MEMORY_FILE = "IAC-CODE.md"
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
                self.memory_index_content.strip(),
                self.memory_mechanics_content.strip(),
            )
        )


def resolve_project_root(cwd: str) -> Path:
    git_root = find_git_worktree_root(cwd)
    if git_root is not None:
        return git_root
    return Path(cwd).expanduser().resolve()


def project_key_for_cwd(cwd: str) -> str:
    return sanitize_path(str(resolve_project_root(cwd)))


def get_project_memory_dir(cwd: str) -> Path:
    return get_config_dir() / "projects" / project_key_for_cwd(cwd) / "memory"


class ProjectMemoryRuntime:
    def __init__(self, cwd: str):
        self.project_root = resolve_project_root(cwd)
        self.user_instruction_path = get_config_dir() / INSTRUCTION_MEMORY_FILE
        self.project_instruction_path = self.project_root / INSTRUCTION_MEMORY_FILE
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
        index_content = self.memory_manager.get_index_content().strip()
        return MemoryContext(
            instruction_memory_content=instruction_content,
            memory_index_content=index_content,
            memory_mechanics_content=_memory_mechanics_content(is_auto_memory_enabled()),
        )

    def _build_instruction_memory_content(self) -> str:
        parts: list[str] = []
        for label, path in (
            ("User IAC-CODE.md", self.user_instruction_path),
            ("Project IAC-CODE.md", self.project_instruction_path),
        ):
            content = _read_text_if_present(path)
            if content:
                parts.append(f"## {label}\n{content}")
        return "\n\n".join(parts)


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


def _memory_mechanics_content(auto_memory_enabled: bool) -> str:
    auto_memory_line = (
        "- Auto-memory is on; topic memories may be recalled and updated when the user asks."
        if auto_memory_enabled
        else "- Auto-memory is off; do not use write_memory and topic memories are not automatically recalled."
    )
    return "\n".join(
        [
            "Use memory carefully:",
            "- IAC-CODE.md files contain always-on user and project instructions.",
            "- MEMORY.md is an always-on index of project topic memories.",
            auto_memory_line,
            "- Topic files are not always injected; use read_memory to inspect relevant topics.",
            "- Use write_memory only when the user explicitly asks to remember or preserve information.",
            "- Treat recalled memories as potentially stale and verify before relying on time-sensitive facts.",
        ]
    )
