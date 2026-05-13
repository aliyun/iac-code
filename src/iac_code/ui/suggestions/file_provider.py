"""File suggestion provider."""

from __future__ import annotations

import os
import time

from iac_code.ui.components.fuzzy_picker import fuzzy_match
from iac_code.ui.suggestions.types import CompletionToken, SuggestionItem, SuggestionProvider

EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".svn",
        ".hg",
        ".bzr",
        ".jj",
        ".sl",
        ".vscode",
        ".idea",
        ".claude",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".eggs",
        ".nox",
        "node_modules",
        ".next",
        ".nuxt",
        "bower_components",
        "dist",
        "build",
        "_build",
        ".build",
        "target",
        ".cache",
        ".npm",
        ".yarn",
    }
)

MAX_INDEX_FILES = 10_000
INDEX_STALE_SECONDS = 30


def _should_exclude_dir(name: str) -> bool:
    """Return True if this directory should be excluded from indexing."""
    if name in EXCLUDE_DIRS:
        return True
    # Exclude *.egg-info directories
    if name.endswith(".egg-info"):
        return True
    return False


class FileProvider(SuggestionProvider):
    """Suggests files from the project tree matching an @-prefixed query."""

    trigger = "@"

    def __init__(self, root_dir: str) -> None:
        self._root_dir = os.path.abspath(root_dir)
        self._index: list[str] = []  # relative paths
        self._index_time: float = 0.0

    def _needs_refresh(self) -> bool:
        return (time.monotonic() - self._index_time) > INDEX_STALE_SECONDS

    def _build_index(self) -> None:
        """Walk the directory tree and build the file index."""
        files: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self._root_dir):
            # Prune excluded dirs in-place so os.walk won't descend into them
            dirnames[:] = [d for d in dirnames if not _should_exclude_dir(d)]
            for fname in filenames:
                abs_path = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(abs_path, self._root_dir)
                files.append(rel_path)
                if len(files) >= MAX_INDEX_FILES:
                    break
            if len(files) >= MAX_INDEX_FILES:
                break

        self._index = files
        self._index_time = time.monotonic()

    def provide(self, token: CompletionToken) -> list[SuggestionItem]:
        """Return file suggestions for the given token."""
        if self._needs_refresh():
            self._build_index()

        # Strip the leading "@" to get the query
        query = token.text[1:] if token.text.startswith("@") else token.text

        scored: list[tuple[float, str]] = []
        for rel_path in self._index:
            score = fuzzy_match(query, rel_path) if query else 0.0
            if score is not None:
                scored.append((score, rel_path))

        scored.sort(key=lambda x: x[0], reverse=True)

        items: list[SuggestionItem] = []
        for score, rel_path in scored:
            items.append(
                SuggestionItem(
                    id=f"file:{rel_path}",
                    display_text=rel_path,
                    completion=f"@{rel_path}",
                    description="",
                    icon="+",
                    source="file",
                    score=score,
                )
            )

        return items
