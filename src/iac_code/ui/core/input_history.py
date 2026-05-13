"""Persistent input history with navigation and prefix search."""

from __future__ import annotations

import os


class InputHistory:
    """Stores and retrieves terminal input history from a plain text file.

    File format: one entry per line, most-recently-appended at the end.

    Attributes:
        _entries: In-memory list of history entries (oldest first).
        _nav_index: Current navigation position; -1 means not navigating.
        _saved_input: The input text that was active when navigation started.
    """

    def __init__(self, history_file: str) -> None:
        self._file = history_file
        self._entries: list[str] = []
        self._nav_index: int = -1
        self._saved_input: str = ""
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load entries from the history file if it exists."""
        if not os.path.exists(self._file):
            return
        with open(self._file, encoding="utf-8") as f:
            for line in f:
                entry = line.rstrip("\n")
                if entry:
                    self._entries.append(entry)

    def _save(self) -> None:
        """Persist all entries to the history file."""
        with open(self._file, "w", encoding="utf-8") as f:
            for entry in self._entries:
                f.write(entry + "\n")

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def append(self, entry: str) -> None:
        """Append an entry, skipping empty strings and consecutive duplicates.

        The new entry is persisted to disk immediately.
        Navigation state is reset.
        """
        if not entry:
            return
        if self._entries and self._entries[-1] == entry:
            return
        self._entries.append(entry)
        self._nav_index = -1
        self._saved_input = ""
        self._save()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, prefix: str) -> list[str]:
        """Return entries whose text starts with *prefix*, most recent first."""
        return [e for e in reversed(self._entries) if e.startswith(prefix)]

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
