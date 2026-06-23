"""Project-partitioned JSONL session storage.

Layout::

    ~/.iac-code/projects/<sanitize(cwd)>/<session_id>/session.jsonl
    ~/.iac-code/projects/<sanitize(cwd)>/<session_id>/metadata.json

Legacy sessions at ``<session_id>.jsonl`` remain readable and are
migrated to the directory format when renamed.

Each ``session.jsonl`` file is a stream of two kinds of JSONL lines:

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from iac_code import __version__
from iac_code.agent.message import ContentBlock, Message, ToolResultBlock
from iac_code.i18n import _
from iac_code.pipeline.engine.constants import CLEANUP_PROMPT_METADATA_TYPE
from iac_code.services.session_metadata import (
    SESSION_JSONL_FILENAME,
    SessionMetadata,
    normalize_session_name,
    read_session_metadata,
    write_session_metadata,
)
from iac_code.utils.file_security import ensure_private_dir, ensure_private_file
from iac_code.utils.project_paths import (
    get_project_dir,
    get_projects_dir,
    get_session_path,
    is_conversation_session_file,
)
from iac_code.utils.state_io import append_jsonl_locked, atomic_write_text, safe_replace


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _cleanup_prompt_identity(message: Message) -> str:
    metadata = message.metadata
    if metadata.get("type") == CLEANUP_PROMPT_METADATA_TYPE:
        metadata = {
            "type": metadata.get("type"),
            "source": metadata.get("source"),
            "cleanupLedgerPath": metadata.get("cleanupLedgerPath") or metadata.get("cleanup_ledger_path"),
        }
    return json.dumps(
        {
            "role": message.role,
            "content": message.to_dict().get("content"),
            "metadata": metadata,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


class SessionStorage:
    """Persist conversation sessions partitioned by working directory."""

    def __init__(self, projects_dir: Path | str | None = None) -> None:
        self._projects_dir = ensure_private_dir(Path(projects_dir) if projects_dir is not None else get_projects_dir())

    # ------------------------------------------------------------------
    # Internal path helpers
    # ------------------------------------------------------------------

    def _legacy_session_path(self, cwd: str, session_id: str) -> Path:
        if self._projects_dir == get_projects_dir():
            return get_session_path(cwd, session_id)
        from iac_code.utils.project_paths import sanitize_path

        return self._projects_dir / sanitize_path(cwd) / f"{session_id}.jsonl"

    def _project_dir_for(self, cwd: str) -> Path:
        if self._projects_dir == get_projects_dir():
            return get_project_dir(cwd)
        from iac_code.utils.project_paths import sanitize_path

        return self._projects_dir / sanitize_path(cwd)

    def _session_dir(self, cwd: str, session_id: str) -> Path:
        return self._project_dir_for(cwd) / session_id

    def _directory_session_path(self, cwd: str, session_id: str) -> Path:
        return self._session_dir(cwd, session_id) / SESSION_JSONL_FILENAME

    def _session_path(self, cwd: str, session_id: str) -> Path:
        directory_path = self._directory_session_path(cwd, session_id)
        legacy_path = self._legacy_session_path(cwd, session_id)
        if directory_path.exists():
            return directory_path
        if legacy_path.exists():
            return legacy_path
        return directory_path

    def session_path(self, cwd: str, session_id: str) -> Path:
        """Public accessor for the on-disk JSONL path of a session."""
        return self._session_path(cwd, session_id)

    def legacy_session_path(self, cwd: str, session_id: str) -> Path:
        return self._legacy_session_path(cwd, session_id)

    def session_dir(self, cwd: str, session_id: str) -> Path:
        return self._session_dir(cwd, session_id)

    def read_metadata(self, cwd: str, session_id: str) -> SessionMetadata | None:
        return read_session_metadata(self._session_dir(cwd, session_id))

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
        append_jsonl_locked(path, [data])
        ensure_private_file(path)

    def append_meta(self, cwd: str, session_id: str, meta_entry: dict[str, Any]) -> None:
        """Append a lite-meta row (no ``role``, distinguished by ``type``)."""
        if "type" not in meta_entry:
            raise ValueError("meta_entry must include a 'type' field")
        path = self._session_path(cwd, session_id)
        ensure_private_dir(path.parent)
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
        """Overwrite the session file with the given messages."""
        if preserve_cleanup_prompts:
            messages = self._merge_preserved_cleanup_prompts(cwd, session_id, messages)
        path = self._session_path(cwd, session_id)
        ensure_private_dir(path.parent)
        lines = []
        for msg in messages:
            data = self._stamp(msg.to_dict(), cwd, session_id, git_branch)
            lines.append(json.dumps(data, ensure_ascii=False) + "\n")
        atomic_write_text(path, "".join(lines), durable=True)
        ensure_private_file(path)

    def _merge_preserved_cleanup_prompts(
        self,
        cwd: str,
        session_id: str,
        messages: list[Message],
    ) -> list[Message]:
        try:
            from iac_code.pipeline.engine.cleanup import is_cleanup_prompt_message
        except Exception:
            return messages

        path = self._session_path(cwd, session_id)
        if not path.exists():
            return messages
        existing = self.load(cwd, session_id)
        preserved = [message for message in existing if is_cleanup_prompt_message(message)]
        if not preserved:
            return messages
        existing_keys = {
            _cleanup_prompt_identity(message) for message in messages if is_cleanup_prompt_message(message)
        }
        missing = [message for message in preserved if _cleanup_prompt_identity(message) not in existing_keys]
        return [*messages, *missing] if missing else messages

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
    # Rename / migration
    # ------------------------------------------------------------------

    def _iter_project_session_dirs(self, cwd: str) -> list[Path]:
        project_dir = self._project_dir_for(cwd)
        if not project_dir.exists():
            return []
        return [p for p in project_dir.iterdir() if p.is_dir() and (p / SESSION_JSONL_FILENAME).exists()]

    def _name_owner_in_project(self, cwd: str, name: str) -> str | None:
        for session_dir in self._iter_project_session_dirs(cwd):
            metadata = read_session_metadata(session_dir)
            if metadata and metadata.name == name:
                return metadata.session_id
        return None

    def _ensure_directory_format(self, cwd: str, session_id: str) -> Path:
        session_dir = self._session_dir(cwd, session_id)
        directory_path = session_dir / SESSION_JSONL_FILENAME
        if directory_path.exists():
            return session_dir
        legacy_path = self._legacy_session_path(cwd, session_id)
        if not legacy_path.exists():
            ensure_private_dir(session_dir)
            directory_path.touch()
            ensure_private_file(directory_path)
            return session_dir
        ensure_private_dir(session_dir)
        safe_replace(str(legacy_path), str(directory_path))
        ensure_private_file(directory_path)
        return session_dir

    def rename_session(self, cwd: str, session_id: str, name: str, *, git_branch: str | None = None) -> str:
        normalized = normalize_session_name(name)
        current = self.read_metadata(cwd, session_id)
        if current and current.name == normalized:
            return "unchanged"
        owner = self._name_owner_in_project(cwd, normalized)
        if owner is not None and owner != session_id:
            raise ValueError(_("Session name already exists in this project: {name}").format(name=normalized))
        session_dir = self._ensure_directory_format(cwd, session_id)
        now = _utc_now()
        metadata = SessionMetadata(
            session_id=session_id,
            name=normalized,
            cwd=cwd,
            git_branch=git_branch,
            created_at=current.created_at if current else now,
            updated_at=now,
        )
        write_session_metadata(session_dir, metadata)
        return "renamed"

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
            candidate = proj_dir / session_id / SESSION_JSONL_FILENAME
            if candidate.exists():
                cwd = self._read_cwd_from_file(candidate) or ""
                return cwd, candidate
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
            for session_dir in proj_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                jsonl = session_dir / SESSION_JSONL_FILENAME
                if jsonl.exists():
                    mtime = jsonl.stat().st_mtime
                    if latest is None or mtime > latest[0]:
                        latest = (mtime, jsonl)
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
        session_id = path.parent.name if path.name == SESSION_JSONL_FILENAME else path.stem
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
