"""Persistent input history with navigation and prefix search."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from iac_code.utils.file_security import atomic_write_text, ensure_private_file

_HISTORY_FORMAT = "iac-code-input-history-v1"


class InputHistory:
    """Stores and retrieves terminal input history from a JSONL file.

    New entries are stored as marked JSONL records, one entry per physical
    line, most-recently-appended at the end. Legacy plain-line entries remain
    readable.

    Attributes:
        _entries: In-memory list of history entries (oldest first).
        _nav_index: Current navigation position; -1 means not navigating.
        _saved_input: The input text that was active when navigation started.
    """

    def __init__(self, history_file: str) -> None:
        self._file = history_file
        self._entries: list[str] = []
        self._session_only: set[int] = set()
        self._nav_index: int = -1
        self._saved_input: str = ""
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_line(line: str) -> str:
        """Decode one history file line, accepting JSONL and legacy plain text."""
        try:
            data: Any = json.loads(line)
        except json.JSONDecodeError:
            return line
        if isinstance(data, dict) and data.get("format") == _HISTORY_FORMAT and isinstance(data.get("text"), str):
            return data["text"]
        return line

    @staticmethod
    def _encode_entry(entry: str) -> str:
        """Encode one history entry as a JSONL object."""
        return json.dumps({"format": _HISTORY_FORMAT, "text": entry}, ensure_ascii=False)

    def _load(self) -> None:
        """Load entries from the history file if it exists."""
        if not os.path.exists(self._file):
            return
        with open(self._file, encoding="utf-8") as f:
            for line in f:
                raw = line.rstrip("\n")
                if raw:
                    self._entries.append(self._decode_line(raw))

    def _save(self) -> None:
        """Persist only non-session-only entries to the history file."""
        lines = [
            self._encode_entry(entry) + "\n" for i, entry in enumerate(self._entries) if i not in self._session_only
        ]
        atomic_write_text(Path(self._file), "".join(lines))
        ensure_private_file(Path(self._file))

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def append(self, entry: str, *, persist: bool = True) -> None:
        """Append an entry, skipping empty strings and consecutive duplicates.

        Args:
            entry: The text to add.
            persist: If True (default), save to disk immediately.
                     If False, the entry is kept in memory only for this session.

        Navigation state is always reset regardless of dedup or persist.
        """
        self._nav_index = -1
        self._saved_input = ""
        if not entry:
            return
        if self._entries and self._entries[-1] == entry:
            return
        self._entries.append(entry)
        if not persist:
            self._session_only.add(len(self._entries) - 1)
        else:
            self._save()

    def reset_navigation(self) -> None:
        """Reset navigation state without modifying entries."""
        self._nav_index = -1
        self._saved_input = ""

    @property
    def is_navigating(self) -> bool:
        """True when the user is actively navigating history."""
        return self._nav_index != -1

    @property
    def saved_input(self) -> str:
        """The input text saved when navigation started."""
        return self._saved_input

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, prefix: str) -> list[str]:
        """Return entries whose text starts with *prefix*, most recent first."""
        return [e for e in reversed(self._entries) if e.startswith(prefix)]

    def entries(self) -> list[str]:
        """Return all in-memory history entries, oldest first."""
        return list(self._entries)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def navigate(self, direction: int, current_input: str = "") -> str | None:
        """Navigate through history.

        Args:
            direction: -1 to go older, +1 to go newer.
            current_input: The current buffer text; saved on the first call
                           so it can be restored when navigating back past the
                           newest entry.

        Returns:
            The history entry at the new position, or None when navigating
            past the newest entry (caller should restore original input).
        """
        if not self._entries:
            return None

        n = len(self._entries)

        if direction == -1:
            # Going older
            if self._nav_index == -1:
                # First navigation — save current input
                self._saved_input = current_input
                self._nav_index = n - 1
            else:
                # Stay at oldest
                if self._nav_index > 0:
                    self._nav_index -= 1
            return self._entries[self._nav_index]

        else:
            # Going newer (direction == 1)
            if self._nav_index == -1:
                # Not navigating; nothing to do
                return None
            if self._nav_index < n - 1:
                self._nav_index += 1
                return self._entries[self._nav_index]
            else:
                # Past the newest — stop navigating, signal restore
                self._nav_index = -1
                return None
