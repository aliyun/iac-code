"""Project-partitioned JSONL session storage.

Layout::

    ~/.iac-code/projects/<sanitize(cwd)>/<session_id>.jsonl

Each session file is a stream of two kinds of JSONL lines:

* **Message rows** — one per :class:`Message`, with extra stamp fields
  (``session_id``, ``cwd``, ``git_branch``, ``version``) appended at write
  time. ``Message.from_dict`` ignores unknown fields, so loading is
  schema-agnostic.

* **Lite-meta rows** — special rows without a ``role``, identified by a
  ``type`` field (``last-prompt``, …). They are appended for the picker
  to read via tail-scan, without being part of the conversation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from iac_code import __version__
from iac_code.agent.message import ContentBlock, Message, ToolResultBlock
from iac_code.utils.file_security import ensure_private_dir, ensure_private_file
from iac_code.utils.project_paths import (
    get_project_dir,
    get_projects_dir,
    get_session_path,
    is_conversation_session_file,
)


class SessionStorage:
    """Persist conversation sessions partitioned by working directory."""

    def __init__(self, projects_dir: Path | str | None = None) -> None:
        self._projects_dir = ensure_private_dir(Path(projects_dir) if projects_dir is not None else get_projects_dir())

    # ------------------------------------------------------------------
    # Internal path helpers
    # ------------------------------------------------------------------

    def _session_path(self, cwd: str, session_id: str) -> Path:
        if self._projects_dir == get_projects_dir():
            return get_session_path(cwd, session_id)
        from iac_code.utils.project_paths import sanitize_path

        return self._projects_dir / sanitize_path(cwd) / f"{session_id}.jsonl"

    def _project_dir_for(self, cwd: str) -> Path:
        if self._projects_dir == get_projects_dir():
            return get_project_dir(cwd)
        from iac_code.utils.project_paths import sanitize_path

        return self._projects_dir / sanitize_path(cwd)

    def session_path(self, cwd: str, session_id: str) -> Path:
        """Public accessor for the on-disk JSONL path of a session."""
        return self._session_path(cwd, session_id)

    # ------------------------------------------------------------------
    # Stamp helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _stamp(data: dict[str, Any], cwd: str, session_id: str, git_branch: str | None) -> dict[str, Any]:
        data["session_id"] = session_id
        data["cwd"] = cwd
        if git_branch is not None:
            data["git_branch"] = git_branch
        data["version"] = __version__
        return data

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def append(
        self,
        cwd: str,
        session_id: str,
        message: Message,
        *,
        git_branch: str | None = None,
    ) -> None:
        """Append a single message (real-time persistence)."""
        path = self._session_path(cwd, session_id)
        ensure_private_dir(path.parent)
        data = self._stamp(message.to_dict(), cwd, session_id, git_branch)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
        ensure_private_file(path)

    def append_meta(self, cwd: str, session_id: str, meta_entry: dict[str, Any]) -> None:
        """Append a lite-meta row (no ``role``, distinguished by ``type``)."""
        if "type" not in meta_entry:
            raise ValueError("meta_entry must include a 'type' field")
        path = self._session_path(cwd, session_id)
        ensure_private_dir(path.parent)
        entry = dict(meta_entry)
        entry["session_id"] = session_id
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        ensure_private_file(path)

    def save(
        self,
        cwd: str,
        session_id: str,
        messages: list[Message],
        *,
        git_branch: str | None = None,
    ) -> None:
        """Overwrite the session file with the given messages."""
        path = self._session_path(cwd, session_id)
        ensure_private_dir(path.parent)
        with open(path, "w", encoding="utf-8") as f:
            for msg in messages:
                data = self._stamp(msg.to_dict(), cwd, session_id, git_branch)
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        ensure_private_file(path)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def load(self, cwd: str, session_id: str) -> list[Message]:
        """Return the conversation messages, skipping lite-meta rows."""
        path = self._session_path(cwd, session_id)
        if not path.exists():
            return []
        messages: list[Message] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if "role" not in obj:
                    # Lite-meta or unknown row — skip.
                    continue
                try:
                    messages.append(Message.from_dict(obj))
                except Exception:
                    continue
        return messages

    def exists(self, cwd: str, session_id: str) -> bool:
        return self._session_path(cwd, session_id).exists()

    # ------------------------------------------------------------------
    # Cross-project lookups (used by CLI --resume / --continue)
    # ------------------------------------------------------------------

    def find_session_anywhere(self, session_id: str) -> tuple[str, Path] | None:
        """Locate a session file across all known project dirs.

        Returns ``(cwd, path)`` where ``cwd`` is the *original* working
        directory of the session (read back from the first stamped
        message), or ``None`` if the file isn't found.
        """
        if not self._projects_dir.exists():
            return None
        for proj_dir in self._projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            candidate = proj_dir / f"{session_id}.jsonl"
            if candidate.exists() and is_conversation_session_file(candidate):
                cwd = self._read_cwd_from_file(candidate) or ""
                return cwd, candidate
        return None

    def get_latest_session_anywhere(self) -> tuple[str, str] | None:
        """Return ``(cwd, session_id)`` for the most-recently-modified session."""
        if not self._projects_dir.exists():
            return None
        latest: tuple[float, Path] | None = None
        for proj_dir in self._projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            for jsonl in proj_dir.glob("*.jsonl"):
                if not is_conversation_session_file(jsonl):
                    continue
                mtime = jsonl.stat().st_mtime
                if latest is None or mtime > latest[0]:
                    latest = (mtime, jsonl)
        if latest is None:
            return None
        path = latest[1]
        cwd = self._read_cwd_from_file(path) or ""
        session_id = path.stem
        return cwd, session_id

    @staticmethod
    def _read_cwd_from_file(path: Path) -> str | None:
        """Read the first message-row's ``cwd`` stamp from a session file."""
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict) and "cwd" in obj:
                        cwd = obj["cwd"]
                        if isinstance(cwd, str):
                            return cwd
        except OSError:
            return None
        return None

    # ------------------------------------------------------------------
    # Interruption repair
    # ------------------------------------------------------------------

    @staticmethod
    def detect_interruption(messages: list[Message]) -> bool:
        """True if the session ends mid-tool-execution (assistant tool_use without results)."""
        if not messages:
            return False
        last = messages[-1]
        return last.role == "assistant" and last.has_tool_use()

    @classmethod
    def repair_interrupted(cls, messages: list[Message]) -> list[Message]:
        """Append synthetic error tool_results for any orphaned tool_use blocks."""
        if not cls.detect_interruption(messages):
            return messages
        last_msg = messages[-1]
        tool_uses = last_msg.get_tool_use_blocks()
        repair_results: list[ContentBlock] = [
            ToolResultBlock(
                tool_use_id=tu.id,
                content="Session interrupted before tool execution completed.",
                is_error=True,
            )
            for tu in tool_uses
        ]
        repaired = list(messages)
        repaired.append(Message(role="user", content=repair_results))
        return repaired
