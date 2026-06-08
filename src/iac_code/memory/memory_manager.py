"""Persistent memory system — stores memories across sessions."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from iac_code.utils.file_security import ensure_private_dir, ensure_private_file

MEMORY_TYPES = {"user", "feedback", "project", "reference"}
INDEX_FILE = "MEMORY.md"
MAX_INDEX_LINES = 200
_MEMORY_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_RESERVED_MEMORY_FILENAMES = {INDEX_FILE.casefold()}


class MemoryManager:
    def __init__(self, memory_dir: str):
        memory_path = ensure_private_dir(Path(memory_dir))
        self._memory_dir = memory_path
        self._memory_root = memory_path.resolve()

    @staticmethod
    def _validate_name(name: str) -> str:
        cleaned = name.strip()
        if not cleaned or cleaned in {".", ".."}:
            raise ValueError(f"Invalid memory name: {name!r}")
        if "/" in cleaned or "\\" in cleaned or ".." in cleaned:
            raise ValueError(f"Invalid memory name: {name!r}")
        if os.path.isabs(cleaned) or not _MEMORY_NAME_RE.fullmatch(cleaned):
            raise ValueError(f"Invalid memory name: {name!r}")
        if f"{cleaned}.md".casefold() in _RESERVED_MEMORY_FILENAMES:
            raise ValueError(f"Invalid memory name: {name!r}")
        return cleaned

    def _memory_path(self, name: str) -> Path:
        safe_name = self._validate_name(name)
        return self._memory_dir / f"{safe_name}.md"

    def _index_path(self) -> Path:
        return self._memory_dir / INDEX_FILE

    def save(self, name: str, content: str, memory_type: str, description: str) -> None:
        if memory_type not in MEMORY_TYPES:
            raise ValueError(f"Invalid memory type: {memory_type}")
        file_content = f"---\nname: {name}\ndescription: {description}\ntype: {memory_type}\n---\n\n{content}\n"
        path = self._memory_path(name)
        self._ensure_writable_path(path)
        self._ensure_writable_path(self._index_path())
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(file_content)
        ensure_private_file(path)
        self._update_index()

    def load(self, name: str) -> dict[str, Any] | None:
        path = self._memory_path(name)
        safe_path = self._safe_existing_file(path)
        if safe_path is None:
            return None
        return self._load_memory_file(safe_path)

    def delete(self, name: str) -> None:
        path = self._memory_path(name)
        self._ensure_writable_path(self._index_path())
        if path.is_symlink():
            raise ValueError(f"Invalid memory path: {path.name}")
        if path.exists():
            self._ensure_writable_path(path)
            os.remove(path)
        self._update_index()

    def list_memories(self) -> list[dict[str, Any]]:
        memories = []
        for path in self._iter_memory_files():
            mem = self._load_memory_file(path)
            if mem:
                memories.append(mem)
        return memories

    def list_memory_metadata(self) -> list[dict[str, Any]]:
        memories = []
        for path in self._iter_memory_files():
            mem = self._load_memory_metadata(path)
            if mem:
                memories.append(mem)
        return memories

    def search(self, query: str) -> list[dict[str, Any]]:
        needle = query.strip().casefold()
        if not needle:
            return []

        matches: list[dict[str, Any]] = []
        for memory in self.list_memories():
            haystack = "\n".join(
                str(memory.get(field, "")) for field in ("name", "description", "type", "content")
            ).casefold()
            if needle in haystack:
                matches.append(memory)
        return matches

    def get_index_content(self) -> str:
        path = self._index_path()
        safe_path = self._safe_existing_file(path)
        if safe_path is None:
            return ""
        with open(safe_path, encoding="utf-8") as f:
            return f.read()

    def get_prompt_content(self) -> str:
        memories = self.list_memories()
        if not memories:
            return ""
        return "\n\n".join(f"[{m.get('type', '')}] {m['content']}" for m in memories)

    def _update_index(self) -> None:
        entries = []
        for path in sorted(self._iter_memory_files(), key=lambda item: item.name):
            mem = self._load_memory_file(path)
            if mem:
                entries.append(f"- [{path.stem}]({path.name}) — {mem.get('description', '')}")
        index_path = self._index_path()
        self._ensure_writable_path(index_path)
        with open(index_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(entries[:MAX_INDEX_LINES]) + "\n")
        ensure_private_file(index_path)

    def _iter_memory_files(self) -> list[Path]:
        root = self._memory_dir
        return [
            safe_path
            for path in root.iterdir()
            if (safe_path := self._safe_existing_file(path)) is not None
            and path.suffix == ".md"
            and path.name.casefold() != INDEX_FILE.casefold()
        ]

    def _safe_existing_file(self, path: Path) -> Path | None:
        if path.is_symlink():
            return None
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if not resolved.is_relative_to(self._memory_root) or not path.is_file():
            return None
        return path

    def _ensure_writable_path(self, path: Path) -> None:
        if path.is_symlink():
            raise ValueError(f"Invalid memory path: {path.name}")
        try:
            parent = path.parent.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"Invalid memory path: {path.name}") from exc
        if parent != self._memory_root:
            raise ValueError(f"Invalid memory path: {path.name}")
        if not path.exists():
            return
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"Invalid memory path: {path.name}") from exc
        if not resolved.is_relative_to(self._memory_root) or not path.is_file():
            raise ValueError(f"Invalid memory path: {path.name}")

    def _load_memory_file(self, path: Path) -> dict[str, Any] | None:
        try:
            return self._parse_memory_file(path.read_text(encoding="utf-8"))
        except OSError:
            return None

    def _load_memory_metadata(self, path: Path) -> dict[str, Any] | None:
        result: dict[str, Any] = {}
        try:
            with open(path, encoding="utf-8") as f:
                if not f.readline().startswith("---"):
                    return None
                for line in f:
                    if line.strip() == "---":
                        return result
                    if ":" in line:
                        key, value = line.split(":", 1)
                        result[key.strip()] = value.strip()
        except OSError:
            return None
        return None

    @staticmethod
    def _parse_memory_file(text: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                for line in parts[1].strip().split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        result[key.strip()] = value.strip()
                result["content"] = parts[2].strip()
            else:
                result["content"] = text
        else:
            result["content"] = text
        return result
