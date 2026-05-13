"""Persistent memory system — stores memories across sessions."""

from __future__ import annotations

import os
from typing import Any

MEMORY_TYPES = {"user", "feedback", "project", "reference"}
INDEX_FILE = "MEMORY.md"
MAX_INDEX_LINES = 200


class MemoryManager:
    def __init__(self, memory_dir: str):
        self._memory_dir = memory_dir
        os.makedirs(memory_dir, exist_ok=True)

    def _memory_path(self, name: str) -> str:
        return os.path.join(self._memory_dir, f"{name}.md")

    def _index_path(self) -> str:
        return os.path.join(self._memory_dir, INDEX_FILE)

    def save(self, name: str, content: str, memory_type: str, description: str) -> None:
        if memory_type not in MEMORY_TYPES:
            raise ValueError(f"Invalid memory type: {memory_type}")
        file_content = f"---\nname: {name}\ndescription: {description}\ntype: {memory_type}\n---\n\n{content}\n"
        with open(self._memory_path(name), "w", encoding="utf-8") as f:
            f.write(file_content)
        self._update_index()

    def load(self, name: str) -> dict[str, Any] | None:
        path = self._memory_path(name)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return self._parse_memory_file(f.read())

    def delete(self, name: str) -> None:
        path = self._memory_path(name)
        if os.path.exists(path):
            os.remove(path)
        self._update_index()

    def list_memories(self) -> list[dict[str, Any]]:
        memories = []
        for filename in os.listdir(self._memory_dir):
            if filename.endswith(".md") and filename != INDEX_FILE:
                mem = self.load(filename[:-3])
                if mem:
                    memories.append(mem)
        return memories

    def get_index_content(self) -> str:
        path = self._index_path()
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as f:
            return f.read()

    def get_prompt_content(self) -> str:
        memories = self.list_memories()
        if not memories:
            return ""
        return "\n\n".join(f"[{m.get('type', '')}] {m['content']}" for m in memories)

    def _update_index(self) -> None:
        entries = []
        for filename in sorted(os.listdir(self._memory_dir)):
            if filename.endswith(".md") and filename != INDEX_FILE:
                mem = self.load(filename[:-3])
                if mem:
                    entries.append(f"- [{filename[:-3]}]({filename}) — {mem.get('description', '')}")
        with open(self._index_path(), "w", encoding="utf-8") as f:
            f.write("\n".join(entries[:MAX_INDEX_LINES]) + "\n")

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
