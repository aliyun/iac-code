"""Project-partitioned JSONL session storage.

Layout::

    ~/.iac-code/projects/<sanitize(cwd)>/<session_id>/session.jsonl
    ~/.iac-code/projects/<sanitize(cwd)>/<session_id>/metadata.json

Legacy sessions at ``<session_id>.jsonl`` remain readable and are
migrated to the directory format when renamed.

Each ``session.jsonl`` file is a stream of two kinds of JSONL lines:

* **Message rows** — one per :class:`Message`, with extra stamp fields
  (``session_id``, ``cwd``, ``git_branch``, ``version``) appended at write
  time, plus a ``metadata.createdAt`` timestamp. ``Message.from_dict``
  ignores unknown fields, so loading is schema-agnostic.

* **Lite-meta rows** — special rows without a ``role``, identified by a
  ``type`` field (``last-prompt``, …). They are appended for the picker
  to read via tail-scan, without being part of the conversation. They
  carry a top-level ``createdAt`` timestamp.

Every row therefore carries its own timestamp, so a session transcript can
be attributed to an analysis time window per message rather than only at
session granularity.
"""

from __future__ import annotations

import json
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from iac_code import __version__
from iac_code.agent.message import ContentBlock, Message, ToolResultBlock
from iac_code.i18n import _
from iac_code.pipeline.constants import CLEANUP_PROMPT_METADATA_TYPE
from iac_code.services.session_layout import (
    UnsupportedSessionLayoutError,
    is_supported_session_dir_for_id,
    require_supported_session_layout,
)
from iac_code.services.session_metadata import (
    SESSION_JSONL_FILENAME,
    SESSION_LAYOUT_VERSION_V2,
    SESSION_METADATA_FILENAME,
    SessionMetadata,
    normalize_session_name,
    read_session_metadata,
    session_metadata_entry_exists,
    write_session_metadata,
)
from iac_code.utils.file_security import ensure_private_dir, ensure_private_file
from iac_code.utils.project_paths import (
    get_projects_dir,
    is_conversation_session_file,
    project_dir_candidates,
)
from iac_code.utils.state_io import append_jsonl_locked, atomic_write_text, safe_replace


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


CREATED_AT_KEY = "createdAt"


def ensure_message_created_at(message: Message) -> str:
    """Stamp ``message.metadata['createdAt']`` if missing, returning the timestamp.

    The timestamp is written onto the message itself rather than only onto the
    serialized row, so that rewrites (``save`` after compaction) and reloads
    keep the moment the message was first persisted instead of drifting to the
    rewrite time.
    """
    existing = message.metadata.get(CREATED_AT_KEY)
    if isinstance(existing, str) and existing:
        return existing
    created_at = _utc_now()
    message.metadata[CREATED_AT_KEY] = created_at
    return created_at


def stamp_meta_row_created_at(entry: dict[str, Any]) -> dict[str, Any]:
    """Stamp a lite-meta row with ``createdAt`` unless the caller supplied one."""
    existing = entry.get(CREATED_AT_KEY)
    if not isinstance(existing, str) or not existing:
        entry[CREATED_AT_KEY] = _utc_now()
    return entry


def _long_project_dir_hash_suffix(project_dir: Path) -> str | None:
    name = project_dir.name
    if len(name) < 14 or name[-13] != "-":
        return None
    suffix = name[-12:]
    try:
        int(suffix, 16)
    except ValueError:
        return None
    return suffix


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


def merge_preserved_cleanup_prompts(existing: list[Message], messages: list[Message]) -> list[Message]:
    """把 existing 中的 cleanup 提示按 identity 去重合并进 messages(保留缺失者)。"""
    try:
        from iac_code.pipeline.engine.cleanup import is_cleanup_prompt_message
    except Exception:
        return messages
    preserved = [message for message in existing if is_cleanup_prompt_message(message)]
    if not preserved:
        return messages
    existing_keys = {_cleanup_prompt_identity(message) for message in messages if is_cleanup_prompt_message(message)}
    missing = [message for message in preserved if _cleanup_prompt_identity(message) not in existing_keys]
    return [*messages, *missing] if missing else messages


class SessionStorage:
    """Persist conversation sessions partitioned by working directory."""

    def __init__(self, projects_dir: Path | str | None = None) -> None:
        self._projects_dir = ensure_private_dir(Path(projects_dir) if projects_dir is not None else get_projects_dir())

    # ------------------------------------------------------------------
    # Internal path helpers
    # ------------------------------------------------------------------

    def _project_write_dir_for(self, cwd: str) -> Path:
        return project_dir_candidates(cwd, self._projects_dir)[0]

    def _project_read_dirs_for(self, cwd: str) -> tuple[Path, ...]:
        candidates = project_dir_candidates(cwd, self._projects_dir)
        existing = tuple(candidate for candidate in candidates if candidate.exists())
        return existing or (candidates[0],)

    def _legacy_session_paths(self, cwd: str, session_id: str) -> tuple[Path, ...]:
        return tuple(project_dir / f"{session_id}.jsonl" for project_dir in self._project_read_dirs_for(cwd))

    def _legacy_session_path(self, cwd: str, session_id: str) -> Path:
        for path in self._legacy_session_paths(cwd, session_id):
            if path.exists():
                return path
        return self._project_write_dir_for(cwd) / f"{session_id}.jsonl"

    def _existing_legacy_session_path(self, cwd: str, session_id: str) -> Path | None:
        for path in self._legacy_session_paths(cwd, session_id):
            if path.exists():
                return path
        return None

    def _project_dir_for(self, cwd: str) -> Path:
        return self._project_write_dir_for(cwd)

    def _session_write_dir(self, cwd: str, session_id: str) -> Path:
        return self._project_write_dir_for(cwd) / session_id

    def _legacy_sidecar_placeholder_dir(self, legacy_path: Path) -> Path:
        return legacy_path.with_name(f"{legacy_path.stem}.legacy-sidecars")

    def _safe_legacy_sidecar_placeholder_dir(self, legacy_path: Path) -> Path:
        placeholder_dir = self._legacy_sidecar_placeholder_dir(legacy_path)
        if self._entry_exists_no_follow(placeholder_dir) and not self._is_directory_entry(placeholder_dir):
            return self._conflicting_sidecar_placeholder_dir(placeholder_dir)
        return placeholder_dir

    def _conflicting_sidecar_placeholder_dir(self, session_dir: Path) -> Path:
        return session_dir.with_name(f"{session_dir.name}.conflict-sidecars")

    def _conflicting_session_path(self, session_dir: Path) -> Path:
        return self._conflicting_sidecar_placeholder_dir(session_dir) / SESSION_JSONL_FILENAME

    def _session_dirs_for(self, cwd: str, session_id: str) -> tuple[Path, ...]:
        return tuple(project_dir / session_id for project_dir in self._project_read_dirs_for(cwd))

    def _session_dir(self, cwd: str, session_id: str) -> Path:
        existing = self._existing_session_dir(cwd, session_id)
        if existing is not None:
            return existing
        legacy_path = self._existing_legacy_session_path(cwd, session_id)
        if legacy_path is not None:
            return self._safe_legacy_sidecar_placeholder_dir(legacy_path)
        conflicting_metadata_dir = self._conflicting_metadata_session_dir(cwd, session_id)
        if conflicting_metadata_dir is not None:
            return self._conflicting_sidecar_placeholder_dir(conflicting_metadata_dir)
        return self._session_write_dir(cwd, session_id)

    def _directory_session_path(self, cwd: str, session_id: str) -> Path:
        return self._session_dir(cwd, session_id) / SESSION_JSONL_FILENAME

    @staticmethod
    def _directory_session_path_for_dir(session_dir: Path) -> Path:
        return session_dir / SESSION_JSONL_FILENAME

    def _existing_session_dir(self, cwd: str, session_id: str) -> Path | None:
        directory_session_dir = self._directory_session_dir(cwd, session_id)
        if directory_session_dir is not None:
            return directory_session_dir
        legacy_path = self._existing_legacy_session_path(cwd, session_id)
        if legacy_path is not None:
            for session_dir in self._session_dirs_for(cwd, session_id):
                if self._is_reusable_legacy_sidecar_session_dir(session_dir):
                    return session_dir
            return None
        sidecar_session_dir = self._sidecar_only_session_dir(cwd, session_id)
        if sidecar_session_dir is not None:
            return sidecar_session_dir
        metadata_session_dir = self._metadata_only_session_dir(cwd, session_id)
        if metadata_session_dir is not None:
            return metadata_session_dir
        return None

    def _directory_session_dir(self, cwd: str, session_id: str) -> Path | None:
        for session_dir in self._session_dirs_for(cwd, session_id):
            if not self._is_directory_entry(session_dir):
                continue
            if not self._is_regular_file_entry(self._directory_session_path_for_dir(session_dir)):
                continue
            if not is_supported_session_dir_for_id(session_dir, session_id):
                continue
            return session_dir
        return None

    def _conflicting_metadata_session_dir(self, cwd: str, session_id: str) -> Path | None:
        for session_dir in self._session_dirs_for(cwd, session_id):
            if not self._is_directory_entry(session_dir):
                continue
            if not session_metadata_entry_exists(session_dir):
                continue
            if not self._is_regular_file_entry(session_dir / SESSION_METADATA_FILENAME):
                return session_dir
            metadata = read_session_metadata(session_dir)
            if metadata is None or metadata.session_id != session_id:
                return session_dir
        return None

    def _metadata_only_session_dir(self, cwd: str, session_id: str) -> Path | None:
        metadata_candidates: list[Path] = []
        for session_dir in self._session_dirs_for(cwd, session_id):
            if not self._is_directory_entry(session_dir):
                continue
            metadata_path = session_dir / SESSION_METADATA_FILENAME
            if not self._is_regular_file_entry(metadata_path) or self._entry_exists_no_follow(
                self._directory_session_path_for_dir(session_dir)
            ):
                continue
            if not is_supported_session_dir_for_id(session_dir, session_id):
                continue
            if self._has_runtime_sidecar_state(session_dir):
                return session_dir
            metadata_candidates.append(session_dir)
        return metadata_candidates[0] if metadata_candidates else None

    def _sidecar_only_session_dir(self, cwd: str, session_id: str, *, include_empty: bool = False) -> Path | None:
        empty_candidates: list[Path] = []
        for session_dir in self._session_dirs_for(cwd, session_id):
            if not self._is_sidecar_only_session_dir(session_dir):
                continue
            if self._has_runtime_sidecar_state(session_dir):
                return session_dir
            empty_candidates.append(session_dir)
        return empty_candidates[0] if include_empty and empty_candidates else None

    def _session_path(self, cwd: str, session_id: str) -> Path:
        directory_session_dir = self._directory_session_dir(cwd, session_id)
        if directory_session_dir is not None:
            return directory_session_dir / SESSION_JSONL_FILENAME
        legacy_path = self._existing_legacy_session_path(cwd, session_id)
        if legacy_path is not None:
            return legacy_path
        metadata_only_session_dir = self._metadata_only_session_dir(cwd, session_id)
        if metadata_only_session_dir is not None:
            return metadata_only_session_dir / SESSION_JSONL_FILENAME
        conflicting_metadata_dir = self._conflicting_metadata_session_dir(cwd, session_id)
        if conflicting_metadata_dir is not None:
            return self._conflicting_session_path(conflicting_metadata_dir)
        return self._session_write_dir(cwd, session_id) / SESSION_JSONL_FILENAME

    def session_path(self, cwd: str, session_id: str) -> Path:
        """Public accessor for the on-disk JSONL path of a session."""
        return self._session_path(cwd, session_id)

    def legacy_session_path(self, cwd: str, session_id: str) -> Path:
        return self._legacy_session_path(cwd, session_id)

    def session_dir(self, cwd: str, session_id: str) -> Path:
        return self._session_dir(cwd, session_id)

    def project_dir(self, cwd: str) -> Path:
        """Public accessor for the on-disk directory holding a project's sessions."""
        return self._project_dir_for(cwd)

    def project_read_dirs(self, cwd: str) -> tuple[Path, ...]:
        """Return existing project-directory aliases in canonical-first order."""
        return self._project_read_dirs_for(cwd)

    def delete_session(self, cwd: str, session_id: str) -> bool:
        """Permanently remove a session's on-disk storage.

        Deletes both the directory-format session folder (``<session_id>/``,
        which also carries ``metadata.json`` and the ``web-session.json``
        sidecar) and any legacy flat ``<session_id>.jsonl`` file. Returns
        ``True`` if anything was removed.
        """
        removed = False
        for project_dir in self._project_read_dirs_for(cwd):
            if project_dir.is_symlink() or self._is_reparse_point(project_dir):
                continue
            session_dir = project_dir / session_id
            if self._is_deletable_session_dir(session_dir, session_id):
                shutil.rmtree(session_dir)
                removed = True

            legacy_path = project_dir / f"{session_id}.jsonl"
            if self._is_regular_file_entry(legacy_path):
                legacy_path.unlink()
                removed = True

            primary_legacy_sidecar = self._legacy_sidecar_placeholder_dir(legacy_path)
            sidecar_dirs = dict.fromkeys(
                (
                    primary_legacy_sidecar,
                    self._conflicting_sidecar_placeholder_dir(primary_legacy_sidecar),
                    self._conflicting_sidecar_placeholder_dir(session_dir),
                )
            )
            for sidecar_dir in sidecar_dirs:
                if not self._is_sidecar_only_session_dir(sidecar_dir):
                    continue
                shutil.rmtree(sidecar_dir)
                removed = True
        return removed

    def _is_deletable_session_dir(self, session_dir: Path, session_id: str) -> bool:
        if not self._is_directory_entry(session_dir) or not self._is_safe_sidecar_directory_tree(session_dir):
            return False
        try:
            if not is_supported_session_dir_for_id(session_dir, session_id):
                return False
        except UnsupportedSessionLayoutError:
            return False
        return (
            self._is_regular_file_entry(session_dir / SESSION_JSONL_FILENAME)
            or self._is_regular_file_entry(session_dir / SESSION_METADATA_FILENAME)
            or self._is_sidecar_only_session_dir(session_dir)
        )

    def read_metadata(self, cwd: str, session_id: str) -> SessionMetadata | None:
        if self._legacy_file_wins_over_metadata_only_dir(cwd, session_id):
            return None
        session_dir = self._existing_session_dir(cwd, session_id)
        if session_dir is None:
            return None
        metadata = read_session_metadata(session_dir)
        if metadata is not None and metadata.session_id != session_id:
            return None
        return metadata

    def v2_session_dir(self, cwd: str, session_id: str) -> Path | None:
        """Return the session directory only when it is explicitly layout v2."""
        if self._legacy_file_wins_over_metadata_only_dir(cwd, session_id):
            return None
        directory_session_dir = self._directory_session_dir(cwd, session_id)
        if directory_session_dir is not None:
            version = require_supported_session_layout(directory_session_dir)
            return directory_session_dir if version == SESSION_LAYOUT_VERSION_V2 else None
        if self._sidecar_only_session_dir(cwd, session_id) is not None:
            return None
        session_dir = self._metadata_only_session_dir(cwd, session_id)
        if session_dir is None:
            return None
        version = require_supported_session_layout(session_dir)
        return session_dir if version == SESSION_LAYOUT_VERSION_V2 else None

    def ensure_v2_session_dir_for_new_session(
        self,
        cwd: str,
        session_id: str,
        *,
        git_branch: str | None = None,
    ) -> Path | None:
        """Create v2 metadata for a brand-new session, leaving legacy state untouched."""
        if self._existing_legacy_session_path(cwd, session_id) is not None:
            return None
        directory_session_dir = self._directory_session_dir(cwd, session_id)
        if directory_session_dir is not None:
            version = require_supported_session_layout(directory_session_dir)
            return directory_session_dir if version == SESSION_LAYOUT_VERSION_V2 else None
        sidecar_session_dir = self._sidecar_only_session_dir(cwd, session_id, include_empty=True)
        if sidecar_session_dir is not None:
            now = _utc_now()
            write_session_metadata(
                sidecar_session_dir,
                SessionMetadata(
                    session_id=session_id,
                    cwd=cwd,
                    git_branch=git_branch,
                    created_at=now,
                    updated_at=now,
                    layout_version=SESSION_LAYOUT_VERSION_V2,
                ),
            )
            return sidecar_session_dir
        metadata_session_dir = self._metadata_only_session_dir(cwd, session_id)
        if metadata_session_dir is not None:
            version = require_supported_session_layout(metadata_session_dir)
            return metadata_session_dir if version == SESSION_LAYOUT_VERSION_V2 else None
        if self._has_directory_session_state(cwd, session_id):
            return None
        session_dir = self._session_write_dir(cwd, session_id)
        if session_dir.is_symlink() or self._is_reparse_point(session_dir):
            return None
        if session_dir.exists() and not self._is_directory_entry(session_dir):
            return None
        if session_dir.exists() and not self._is_sidecar_only_session_dir(session_dir):
            return None
        now = _utc_now()
        write_session_metadata(
            session_dir,
            SessionMetadata(
                session_id=session_id,
                cwd=cwd,
                git_branch=git_branch,
                created_at=now,
                updated_at=now,
                layout_version=SESSION_LAYOUT_VERSION_V2,
            ),
        )
        return session_dir

    def _has_directory_session_state(self, cwd: str, session_id: str) -> bool:
        return any(
            self._entry_exists_no_follow(self._directory_session_path_for_dir(session_dir))
            or session_metadata_entry_exists(session_dir)
            for session_dir in self._session_dirs_for(cwd, session_id)
        )

    def _legacy_file_wins_over_metadata_only_dir(self, cwd: str, session_id: str) -> bool:
        return (
            self._existing_legacy_session_path(cwd, session_id) is not None
            and self._directory_session_dir(cwd, session_id) is None
        )

    @staticmethod
    def _is_sidecar_only_session_dir(session_dir: Path) -> bool:
        if not SessionStorage._is_directory_entry(session_dir):
            return False
        try:
            children = list(session_dir.iterdir())
        except OSError:
            return False
        return all(SessionStorage._is_allowed_sidecar_child(child) for child in children)

    @staticmethod
    def _is_reusable_legacy_sidecar_session_dir(session_dir: Path) -> bool:
        return SessionStorage._is_sidecar_only_session_dir(session_dir) and SessionStorage._has_pipeline_sidecar_marker(
            session_dir
        )

    @staticmethod
    def _has_pipeline_sidecar_marker(session_dir: Path) -> bool:
        pipeline_dir = session_dir / "pipeline"
        if SessionStorage._is_directory_entry(pipeline_dir) and any(
            SessionStorage._is_regular_file_entry(pipeline_dir / name)
            for name in ("meta.yaml", "context.yaml", "events.jsonl")
        ):
            return True
        a2a_pipeline_dir = session_dir / "a2a" / "pipeline"
        if not SessionStorage._is_directory_entry(a2a_pipeline_dir):
            return False
        return any(
            SessionStorage._is_regular_file_entry(a2a_pipeline_dir / name)
            for name in ("a2a-events.jsonl", "a2a-snapshot.json")
        )

    @staticmethod
    def _is_allowed_sidecar_child(child: Path) -> bool:
        allowed_dirs = {
            "a2a",
            "image-cache",
            "pipeline",
            "tool-results",
        }
        if SessionStorage._is_allowed_sidecar_file_name(child.name):
            return SessionStorage._is_regular_file_entry(child)
        return child.name in allowed_dirs and SessionStorage._is_safe_sidecar_directory_tree(child)

    @staticmethod
    def _is_allowed_sidecar_file_name(name: str) -> bool:
        allowed_files = {
            ".backup-state.json",
            ".backup-lock",
            ".permission-audit.jsonl.lock",
            "permission-audit.jsonl",
            ".usage.jsonl.lock",
            "usage.jsonl",
            "web-session.json",
            "web-session.json.tmp",
        }
        if name in allowed_files:
            return True
        web_metadata_temp_prefix = ".web-session.json."
        if name.startswith(web_metadata_temp_prefix) and name.endswith(".tmp"):
            token = name[len(web_metadata_temp_prefix) : -len(".tmp")]
            return (
                bool(token) and token.isascii() and all(character.isalnum() or character == "_" for character in token)
            )
        prefix = "permission-audit.jsonl."
        suffix = name[len(prefix) :] if name.startswith(prefix) else ""
        return suffix.isdecimal()

    @staticmethod
    def _entry_exists_no_follow(path: Path) -> bool:
        try:
            path.stat(follow_symlinks=False)
        except OSError:
            return False
        return True

    @staticmethod
    def _has_runtime_sidecar_state(session_dir: Path) -> bool:
        if not SessionStorage._is_directory_entry(session_dir):
            return False
        try:
            children = list(session_dir.iterdir())
        except OSError:
            return False
        return any(
            child.name != SESSION_METADATA_FILENAME and SessionStorage._is_allowed_sidecar_child(child)
            for child in children
        )

    @staticmethod
    def _is_safe_sidecar_directory_tree(path: Path) -> bool:
        if not SessionStorage._is_directory_entry(path):
            return False
        stack = [path]
        while stack:
            current = stack.pop()
            try:
                children = list(current.iterdir())
            except OSError:
                return False
            for child in children:
                if child.is_symlink() or SessionStorage._is_reparse_point(child):
                    return False
                try:
                    mode = child.stat(follow_symlinks=False).st_mode
                except OSError:
                    return False
                if stat.S_ISDIR(mode):
                    stack.append(child)
                elif not stat.S_ISREG(mode):
                    return False
        return True

    @staticmethod
    def _is_regular_file_entry(path: Path) -> bool:
        if path.is_symlink() or SessionStorage._is_reparse_point(path):
            return False
        try:
            return stat.S_ISREG(path.stat(follow_symlinks=False).st_mode)
        except OSError:
            return False

    @staticmethod
    def _is_directory_entry(path: Path) -> bool:
        if path.is_symlink() or SessionStorage._is_reparse_point(path):
            return False
        try:
            return stat.S_ISDIR(path.stat(follow_symlinks=False).st_mode)
        except OSError:
            return False

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        try:
            attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
        except OSError:
            return False
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))

    def _is_new_directory_session(self, cwd: str, session_id: str) -> bool:
        return (
            not self._has_directory_session_state(cwd, session_id)
            and self._existing_legacy_session_path(cwd, session_id) is None
        )

    def _prepare_session_write(self, cwd: str, session_id: str) -> tuple[Path, bool]:
        was_new = self._is_new_directory_session(cwd, session_id)
        directory_session_dir = self._directory_session_dir(cwd, session_id)
        legacy_path = self._existing_legacy_session_path(cwd, session_id)
        metadata_only_session_dir = (
            None if legacy_path is not None else self._metadata_only_session_dir(cwd, session_id)
        )
        path = self._session_path(cwd, session_id)
        conflicting_metadata_dir = self._conflicting_metadata_session_dir(cwd, session_id)
        if (
            conflicting_metadata_dir is not None
            and directory_session_dir is None
            and metadata_only_session_dir is None
            and legacy_path is None
        ):
            raise UnsupportedSessionLayoutError(
                _("Unsupported session metadata: {path}").format(path=conflicting_metadata_dir)
            )
        if directory_session_dir is not None and path == self._directory_session_path_for_dir(directory_session_dir):
            require_supported_session_layout(directory_session_dir)
        return path, was_new

    def _ensure_new_session_metadata(
        self,
        cwd: str,
        session_id: str,
        *,
        git_branch: str | None,
        was_new: bool,
    ) -> None:
        if self._legacy_file_wins_over_metadata_only_dir(cwd, session_id):
            return
        session_dir = self._session_dir(cwd, session_id)
        if was_new:
            now = _utc_now()
            write_session_metadata(
                session_dir,
                SessionMetadata(
                    session_id=session_id,
                    cwd=cwd,
                    git_branch=git_branch,
                    created_at=now,
                    updated_at=now,
                    layout_version=SESSION_LAYOUT_VERSION_V2,
                ),
            )
            return
        current = read_session_metadata(session_dir)
        if current and current.layout_version == SESSION_LAYOUT_VERSION_V2 and git_branch and not current.git_branch:
            write_session_metadata(
                session_dir,
                SessionMetadata(
                    session_id=current.session_id,
                    name=current.name,
                    cwd=current.cwd,
                    git_branch=git_branch,
                    created_at=current.created_at,
                    updated_at=_utc_now(),
                    schema_version=current.schema_version,
                    layout_version=current.layout_version,
                ),
            )

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
        path, was_new = self._prepare_session_write(cwd, session_id)
        ensure_private_dir(path.parent)
        ensure_message_created_at(message)
        data = self._stamp(message.to_dict(), cwd, session_id, git_branch)
        append_jsonl_locked(path, [data])
        ensure_private_file(path)
        self._ensure_new_session_metadata(cwd, session_id, git_branch=git_branch, was_new=was_new)

    def append_meta(self, cwd: str, session_id: str, meta_entry: dict[str, Any]) -> None:
        """Append a lite-meta row (no ``role``, distinguished by ``type``)."""
        if "type" not in meta_entry:
            raise ValueError("meta_entry must include a 'type' field")
        path, was_new = self._prepare_session_write(cwd, session_id)
        ensure_private_dir(path.parent)
        entry = dict(meta_entry)
        entry["session_id"] = session_id
        stamp_meta_row_created_at(entry)
        append_jsonl_locked(path, [entry])
        ensure_private_file(path)
        self._ensure_new_session_metadata(cwd, session_id, git_branch=None, was_new=was_new)

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
        path, was_new = self._prepare_session_write(cwd, session_id)
        if preserve_cleanup_prompts:
            messages = self._merge_preserved_cleanup_prompts(cwd, session_id, messages)
        ensure_private_dir(path.parent)
        lines = []
        for msg in messages:
            ensure_message_created_at(msg)
            data = self._stamp(msg.to_dict(), cwd, session_id, git_branch)
            lines.append(json.dumps(data, ensure_ascii=False) + "\n")
        atomic_write_text(path, "".join(lines), durable=True)
        ensure_private_file(path)
        self._ensure_new_session_metadata(cwd, session_id, git_branch=git_branch, was_new=was_new)

    def _merge_preserved_cleanup_prompts(
        self,
        cwd: str,
        session_id: str,
        messages: list[Message],
    ) -> list[Message]:
        path = self._session_path(cwd, session_id)
        if not path.exists():
            return messages
        return merge_preserved_cleanup_prompts(self.load(cwd, session_id), messages)

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
        path = self._session_path(cwd, session_id)
        if path.exists():
            return True
        if self._directory_session_dir(cwd, session_id) is not None:
            return True
        return self._metadata_only_session_dir(cwd, session_id) is not None

    # ------------------------------------------------------------------
    # Rename / migration
    # ------------------------------------------------------------------

    def _iter_project_session_dirs(self, cwd: str) -> list[Path]:
        session_dirs: list[Path] = []
        for project_dir in self._project_read_dirs_for(cwd):
            if not project_dir.exists():
                continue
            session_dirs.extend(
                p for p in project_dir.iterdir() if p.is_dir() and (p / SESSION_JSONL_FILENAME).exists()
            )
        return session_dirs

    def _name_owner_in_project(self, cwd: str, name: str) -> str | None:
        for session_dir in self._iter_project_session_dirs(cwd):
            try:
                if not is_supported_session_dir_for_id(session_dir, session_dir.name):
                    continue
            except UnsupportedSessionLayoutError:
                continue
            metadata = read_session_metadata(session_dir)
            if metadata and metadata.session_id == session_dir.name and metadata.name == name:
                return metadata.session_id
        return None

    def _ensure_directory_format(self, cwd: str, session_id: str) -> Path:
        existing_directory_dir = self._directory_session_dir(cwd, session_id)
        if existing_directory_dir is not None:
            return existing_directory_dir
        session_dir = self._session_write_dir(cwd, session_id)
        directory_path = session_dir / SESSION_JSONL_FILENAME
        legacy_path = self._legacy_session_path(cwd, session_id)
        if not legacy_path.exists():
            ensure_private_dir(session_dir)
            directory_path.touch()
            ensure_private_file(directory_path)
            return session_dir
        ensure_private_dir(session_dir)
        self._merge_legacy_sidecar_placeholder(legacy_path, session_dir)
        safe_replace(str(legacy_path), str(directory_path))
        ensure_private_file(directory_path)
        return session_dir

    def _merge_legacy_sidecar_placeholder(self, legacy_path: Path, session_dir: Path) -> None:
        placeholder_dir = self._legacy_sidecar_placeholder_dir(legacy_path)
        if not self._entry_exists_no_follow(placeholder_dir) or not self._is_directory_entry(placeholder_dir):
            return
        for child in list(placeholder_dir.iterdir()):
            if not self._is_allowed_sidecar_child(child):
                continue
            destination = session_dir / child.name
            if destination.exists():
                continue
            try:
                child.replace(destination)
            except OSError:
                continue
        try:
            placeholder_dir.rmdir()
        except OSError:
            pass

    def rename_session(self, cwd: str, session_id: str, name: str, *, git_branch: str | None = None) -> str:
        normalized = normalize_session_name(name)
        was_new = self._is_new_directory_session(cwd, session_id)
        legacy_file_wins = self._legacy_file_wins_over_metadata_only_dir(cwd, session_id)
        if not legacy_file_wins:
            conflicting_metadata_dir = self._conflicting_metadata_session_dir(cwd, session_id)
            if conflicting_metadata_dir is not None:
                raise UnsupportedSessionLayoutError(
                    _("Unsupported session metadata: {path}").format(path=conflicting_metadata_dir)
                )
        session_dir = (
            self._session_write_dir(cwd, session_id) if legacy_file_wins else self._session_dir(cwd, session_id)
        )
        if self._has_directory_session_state(cwd, session_id) and not legacy_file_wins:
            require_supported_session_layout(session_dir)
        current = None if legacy_file_wins else self.read_metadata(cwd, session_id)
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
            layout_version=SESSION_LAYOUT_VERSION_V2
            if was_new or legacy_file_wins
            else current.layout_version
            if current
            else None,
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
                if not is_supported_session_dir_for_id(candidate.parent, session_id):
                    continue
                cwd = self._read_cwd_from_directory_session(candidate.parent, candidate) or ""
                return cwd, candidate
            metadata_candidate = proj_dir / session_id / SESSION_METADATA_FILENAME
            if metadata_candidate.exists() and not (proj_dir / f"{session_id}.jsonl").exists():
                if not is_supported_session_dir_for_id(metadata_candidate.parent, session_id):
                    continue
                metadata = read_session_metadata(metadata_candidate.parent)
                if self._metadata_only_shadowed_by_legacy_session(session_id, metadata, scanned_project_dir=proj_dir):
                    continue
                cwd = metadata.cwd if metadata and metadata.cwd else ""
                return cwd, metadata_candidate
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
        latest_unsupported: tuple[float, UnsupportedSessionLayoutError] | None = None
        for proj_dir in self._projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            for session_dir in proj_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                jsonl = session_dir / SESSION_JSONL_FILENAME
                if jsonl.exists():
                    mtime = jsonl.stat().st_mtime
                    try:
                        if not is_supported_session_dir_for_id(session_dir, session_dir.name):
                            continue
                    except UnsupportedSessionLayoutError as exc:
                        if latest is None or mtime > latest[0]:
                            latest_unsupported = (mtime, exc)
                        continue
                    if latest is None or mtime > latest[0]:
                        latest = (mtime, jsonl)
                    continue
                metadata = session_dir / SESSION_METADATA_FILENAME
                if metadata.exists():
                    if (proj_dir / f"{session_dir.name}.jsonl").exists():
                        continue
                    try:
                        if not is_supported_session_dir_for_id(session_dir, session_dir.name):
                            continue
                    except UnsupportedSessionLayoutError:
                        continue
                    session_metadata = read_session_metadata(session_dir)
                    if session_metadata is None:
                        continue
                    if self._metadata_only_shadowed_by_legacy_session(
                        session_dir.name,
                        session_metadata,
                        scanned_project_dir=proj_dir,
                    ):
                        continue
                    mtime = metadata.stat().st_mtime
                    if latest is None or mtime > latest[0]:
                        latest = (mtime, metadata)
            for jsonl in proj_dir.glob("*.jsonl"):
                if not is_conversation_session_file(jsonl):
                    continue
                mtime = jsonl.stat().st_mtime
                if latest is None or mtime > latest[0]:
                    latest = (mtime, jsonl)
        if latest is None:
            if latest_unsupported is not None:
                raise latest_unsupported[1]
            return None
        if latest_unsupported is not None and latest_unsupported[0] > latest[0]:
            raise latest_unsupported[1]
        path = latest[1]
        if path.name == SESSION_JSONL_FILENAME:
            is_supported_session_dir_for_id(path.parent, path.parent.name)
            cwd = self._read_cwd_from_directory_session(path.parent, path) or ""
            session_id = path.parent.name
        elif path.name == SESSION_METADATA_FILENAME:
            metadata = read_session_metadata(path.parent)
            cwd = metadata.cwd if metadata and metadata.cwd else ""
            session_id = path.parent.name
        else:
            cwd = self._read_cwd_from_file(path) or ""
            session_id = path.stem
        return cwd, session_id

    def _metadata_only_shadowed_by_legacy_session(
        self,
        session_id: str,
        metadata: SessionMetadata | None,
        *,
        scanned_project_dir: Path | None = None,
    ) -> bool:
        for project_dir in self._metadata_shadow_project_dirs(metadata, scanned_project_dir):
            legacy_path = project_dir / f"{session_id}.jsonl"
            if legacy_path.exists() and is_conversation_session_file(legacy_path):
                return True
        return False

    def _metadata_shadow_project_dirs(
        self,
        metadata: SessionMetadata | None,
        scanned_project_dir: Path | None,
    ) -> tuple[Path, ...]:
        project_dirs: list[Path] = []
        seen: set[Path] = set()

        def add(project_dir: Path) -> None:
            if project_dir not in seen:
                project_dirs.append(project_dir)
                seen.add(project_dir)

        if metadata is not None and metadata.cwd:
            for project_dir in project_dir_candidates(metadata.cwd, self._projects_dir):
                add(project_dir)
        if scanned_project_dir is not None:
            add(scanned_project_dir)
            suffix = _long_project_dir_hash_suffix(scanned_project_dir)
            if suffix is not None and self._projects_dir.exists():
                for project_dir in self._projects_dir.iterdir():
                    if project_dir.is_dir() and _long_project_dir_hash_suffix(project_dir) == suffix:
                        add(project_dir)
        return tuple(project_dirs)

    @staticmethod
    def _read_cwd_from_directory_session(session_dir: Path, session_path: Path) -> str | None:
        metadata = read_session_metadata(session_dir)
        if metadata and metadata.session_id == session_dir.name and metadata.cwd:
            return metadata.cwd
        return SessionStorage._read_cwd_from_file(session_path)

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
