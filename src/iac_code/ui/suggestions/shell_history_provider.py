"""Shell history suggestion provider."""

from __future__ import annotations

import os

from iac_code.ui.suggestions.types import CompletionToken, SuggestionItem, SuggestionProvider


def _detect_history_path() -> str | None:
    """Detect the shell history file path from the SHELL environment variable."""
    shell = os.environ.get("SHELL", "")
    home = os.path.expanduser("~")

    if "zsh" in shell:
        candidate = os.path.join(home, ".zsh_history")
    elif "bash" in shell:
        candidate = os.path.join(home, ".bash_history")
    else:
        # Fallback: try zsh first, then bash
        zsh = os.path.join(home, ".zsh_history")
        bash = os.path.join(home, ".bash_history")
        if os.path.exists(zsh):
            return zsh
        if os.path.exists(bash):
            return bash
        return None

    return candidate if os.path.exists(candidate) else None


def _read_history(path: str) -> list[str]:
    """Read history entries from a history file.

    Handles both plain bash history and zsh extended history format.
    Returns entries in file order (oldest first).
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return []

    # Decode, ignoring errors (history files can have mixed encodings)
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()

    entries: list[str] = []
    for line in lines:
        # zsh extended_history format: ": <timestamp>:<elapsed>;<command>"
        if line.startswith(": ") and ";" in line:
            _, _, cmd = line.partition(";")
            cmd = cmd.strip()
            if cmd:
                entries.append(cmd)
        else:
            line = line.strip()
            if line:
                entries.append(line)

    return entries


class ShellHistoryProvider(SuggestionProvider):
    """Provides shell history suggestions for ! trigger."""

    trigger = "!"

    def __init__(self) -> None:
        self._history_path: str | None = _detect_history_path()

    def provide(self, token: CompletionToken) -> list[SuggestionItem]:
        """Return shell history suggestions matching the query."""
        if not self._history_path:
            return []

        # Strip the leading "!" to get the query
        query = token.text[1:] if token.text.startswith("!") else token.text

        entries = _read_history(self._history_path)

        # Substring match, dedup, most recent first
        seen: set[str] = set()
        matched: list[str] = []

        # Iterate in reverse so most recent entries come first
        for entry in reversed(entries):
            if entry in seen:
                continue
            if query.lower() in entry.lower():
                seen.add(entry)
                matched.append(entry)

        items: list[SuggestionItem] = []
        for i, entry in enumerate(matched):
            items.append(
                SuggestionItem(
                    id=f"shell:{i}",
                    display_text=entry,
                    completion=f"!{entry}",
                    description="",
                    icon="↑",
                    source="shell",
                    score=float(len(matched) - i),
                )
            )

        return items
