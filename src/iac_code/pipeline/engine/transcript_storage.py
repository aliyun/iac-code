"""Sidecar-local JSONL transcript storage for pipeline step attempts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from iac_code import __version__
from iac_code.agent.message import Message
from iac_code.services.session_metadata import SESSION_JSONL_FILENAME
from iac_code.services.session_storage import SessionStorage
from iac_code.utils.file_security import ensure_private_dir, ensure_private_file

_SAFE_TRANSCRIPT_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


class PipelineTranscriptStorage:
    """Persist AgentLoop messages under a pipeline sidecar instead of root sessions."""

    def __init__(self, sidecar_dir: Path | str) -> None:
        self._sidecar_dir = Path(sidecar_dir)
        self._transcripts_dir = self._sidecar_dir / "transcripts"

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
        ensure_private_dir(path.parent)
        data = self._stamp(message.to_dict(), cwd, session_id, git_branch)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
        ensure_private_file(path)

    def append_meta(self, cwd: str, session_id: str, meta_entry: dict[str, Any]) -> None:
        if "type" not in meta_entry:
            raise ValueError("meta_entry must include a 'type' field")
        path = self.session_path(cwd, session_id)
        ensure_private_dir(path.parent)
        entry = dict(meta_entry)
        entry["session_id"] = session_id
        with path.open("a", encoding="utf-8") as f:
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
        path = self.session_path(cwd, session_id)
        ensure_private_dir(path.parent)
        with path.open("w", encoding="utf-8") as f:
            for message in messages:
                data = self._stamp(message.to_dict(), cwd, session_id, git_branch)
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        ensure_private_file(path)

    def load(self, cwd: str, session_id: str) -> list[Message]:
        path = self.session_path(cwd, session_id)
        if not path.exists():
            return []
        messages: list[Message] = []
        with path.open(encoding="utf-8") as f:
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
        return messages

    def exists(self, cwd: str, session_id: str) -> bool:
        return self.session_path(cwd, session_id).exists()

    def list_transcript_ids(self) -> list[str]:
        """Return existing sidecar transcript ids, ignoring unsafe directory names."""
        if not self._transcripts_dir.exists():
            return []
        return sorted(
            path.name
            for path in self._transcripts_dir.iterdir()
            if path.is_dir() and _SAFE_TRANSCRIPT_ID.fullmatch(path.name)
        )

    @staticmethod
    def repair_interrupted(messages: list[Message]) -> list[Message]:
        return SessionStorage.repair_interrupted(messages)
