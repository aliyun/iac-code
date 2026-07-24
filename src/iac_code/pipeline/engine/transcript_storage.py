"""Sidecar-local JSONL transcript storage for pipeline step attempts."""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path
from typing import Any

from iac_code import __version__
from iac_code.agent.message import Message
from iac_code.services.session_layout import ensure_session_owned_parent, require_supported_session_layout
from iac_code.services.session_metadata import SESSION_JSONL_FILENAME, session_metadata_entry_exists
from iac_code.services.session_storage import SessionStorage, merge_preserved_cleanup_prompts
from iac_code.utils.file_security import ensure_private_dir, ensure_private_file
from iac_code.utils.state_io import append_jsonl_locked, open_text_no_follow, write_text_no_follow

_SAFE_TRANSCRIPT_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


class PipelineTranscriptStorage:
    """Persist AgentLoop messages under a pipeline sidecar instead of root sessions."""

    def __init__(self, sidecar_dir: Path | str) -> None:
        self._sidecar_dir = Path(sidecar_dir)
        self._transcripts_dir = self._sidecar_dir / "transcripts"
        self._session_root = self._infer_session_root(self._sidecar_dir)

    def _validate_transcript_id(self, transcript_id: str) -> str:
        if not transcript_id or transcript_id in {".", ".."}:
            raise ValueError("unsafe transcript id")
        if "/" in transcript_id or "\\" in transcript_id or ".." in transcript_id:
            raise ValueError("unsafe transcript id")
        if not _SAFE_TRANSCRIPT_ID.fullmatch(transcript_id):
            raise ValueError("unsafe transcript id")
        return transcript_id

    def session_dir(self, cwd: str, session_id: str) -> Path:
        transcript_id = self._validate_transcript_id(session_id)
        return self._transcripts_dir / transcript_id

    def session_path(self, cwd: str, session_id: str) -> Path:
        return self.session_dir(cwd, session_id) / SESSION_JSONL_FILENAME

    @staticmethod
    def _stamp(data: dict[str, Any], cwd: str, session_id: str, git_branch: str | None) -> dict[str, Any]:
        data["session_id"] = session_id
        data["cwd"] = cwd
        if git_branch is not None:
            data["git_branch"] = git_branch
        data["version"] = __version__
        return data

    def append(
        self,
        cwd: str,
        session_id: str,
        message: Message,
        *,
        git_branch: str | None = None,
    ) -> None:
        path = self.session_path(cwd, session_id)
        self._ensure_transcript_parent(path)
        data = self._stamp(message.to_dict(), cwd, session_id, git_branch)
        append_jsonl_locked(path, [data])
        ensure_private_file(path)

    def append_meta(self, cwd: str, session_id: str, meta_entry: dict[str, Any]) -> None:
        if "type" not in meta_entry:
            raise ValueError("meta_entry must include a 'type' field")
        path = self.session_path(cwd, session_id)
        self._ensure_transcript_parent(path)
        entry = dict(meta_entry)
        entry["session_id"] = session_id
        append_jsonl_locked(path, [entry])
        ensure_private_file(path)

    def save(
        self,
        cwd: str,
        session_id: str,
        messages: list[Message],
        *,
        git_branch: str | None = None,
        preserve_cleanup_prompts: bool = False,
    ) -> None:
        if preserve_cleanup_prompts and self.exists(cwd, session_id):
            messages = merge_preserved_cleanup_prompts(self.load(cwd, session_id), messages)
        path = self.session_path(cwd, session_id)
        self._ensure_transcript_parent(path)
        content = "".join(
            json.dumps(self._stamp(message.to_dict(), cwd, session_id, git_branch), ensure_ascii=False) + "\n"
            for message in messages
        )
        write_text_no_follow(path, content, encoding="utf-8")
        ensure_private_file(path)

    def load(self, cwd: str, session_id: str) -> list[Message]:
        path = self.session_path(cwd, session_id)
        if not _is_regular_file_entry(path):
            return []
        messages: list[Message] = []
        try:
            with open_text_no_follow(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(value, dict) or "role" not in value:
                        continue
                    try:
                        messages.append(Message.from_dict(value))
                    except Exception:
                        continue
        except OSError:
            if _is_regular_file_entry(path):
                raise
            return []
        return messages

    def exists(self, cwd: str, session_id: str) -> bool:
        return _is_regular_file_entry(self.session_path(cwd, session_id))

    def list_transcript_ids(self) -> list[str]:
        """Return existing sidecar transcript ids, ignoring unsafe directory names."""
        if not _is_directory_entry(self._transcripts_dir):
            return []
        return sorted(
            path.name
            for path in self._transcripts_dir.iterdir()
            if _is_directory_entry(path) and _SAFE_TRANSCRIPT_ID.fullmatch(path.name)
        )

    @staticmethod
    def repair_interrupted(messages: list[Message]) -> list[Message]:
        return SessionStorage.repair_interrupted(messages)

    def _ensure_transcript_parent(self, path: Path) -> None:
        if self._session_root is not None and require_supported_session_layout(self._session_root) is not None:
            ensure_session_owned_parent(self._session_root, path)
            return
        ensure_private_dir(path.parent)

    @staticmethod
    def _infer_session_root(sidecar_dir: Path) -> Path | None:
        session_root = sidecar_dir.parent
        if sidecar_dir.name == "pipeline" and session_metadata_entry_exists(session_root):
            return session_root
        return None


def _is_directory_entry(path: Path) -> bool:
    if path.is_symlink() or _is_reparse_point(path):
        return False
    try:
        return stat.S_ISDIR(path.stat(follow_symlinks=False).st_mode)
    except OSError:
        return False


def _is_regular_file_entry(path: Path) -> bool:
    if path.is_symlink() or _is_reparse_point(path):
        return False
    try:
        return stat.S_ISREG(path.stat(follow_symlinks=False).st_mode)
    except OSError:
        return False


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
