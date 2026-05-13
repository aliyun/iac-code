"""Directory suggestion provider."""

from __future__ import annotations

import os

from iac_code.ui.components.fuzzy_picker import fuzzy_match
from iac_code.ui.suggestions.file_provider import EXCLUDE_DIRS
from iac_code.ui.suggestions.types import CompletionToken, SuggestionItem, SuggestionProvider


class DirectoryProvider(SuggestionProvider):
    """Suggests directory entries (dirs + files) matching an @-prefixed query.

    Unlike FileProvider which indexes the entire tree, DirectoryProvider lists
    entries in the directory that corresponds to the query path prefix, similar
    to shell tab-completion behaviour.
    """

    trigger = "@"

    def __init__(self, root_dir: str) -> None:
        self._root_dir = os.path.abspath(root_dir)

    def _list_entries(self, dir_path: str) -> list[tuple[str, bool]]:
        """List (name, is_dir) entries in dir_path, excluding hidden/excluded dirs."""
        entries: list[tuple[str, bool]] = []
        try:
            for entry in os.scandir(dir_path):
                name = entry.name
                is_dir = entry.is_dir(follow_symlinks=False)
                # Skip hidden entries and excluded directories
                if name.startswith("."):
                    continue
                if is_dir and name in EXCLUDE_DIRS:
                    continue
                entries.append((name, is_dir))
        except PermissionError:
            pass
        entries.sort(key=lambda e: (not e[1], e[0].lower()))
        return entries

    def provide(self, token: CompletionToken) -> list[SuggestionItem]:
        """Return directory-listing suggestions for the given token."""
        # Strip the leading "@"
        query = token.text[1:] if token.text.startswith("@") else token.text

        # Split query into directory prefix and filename fragment
        # e.g. "src/ui/inp" → dir_prefix="src/ui", fragment="inp"
        # e.g. "src/" → dir_prefix="src", fragment=""
        # e.g. "src" → dir_prefix="", fragment="src"
        if "/" in query:
            last_slash = query.rfind("/")
            dir_prefix = query[:last_slash]
            fragment = query[last_slash + 1 :]
        else:
            dir_prefix = ""
            fragment = query

        # Resolve the directory to list
        if dir_prefix:
            list_dir = os.path.join(self._root_dir, dir_prefix)
        else:
            list_dir = self._root_dir

        entries = self._list_entries(list_dir)

        items: list[SuggestionItem] = []
        for name, is_dir in entries:
            if fragment:
                score = fuzzy_match(fragment, name)
                if score is None:
                    continue
            else:
                score = 0.0

            if dir_prefix:
                rel_path = f"{dir_prefix}/{name}"
            else:
                rel_path = name

            completion_suffix = "/" if is_dir else ""
            items.append(
                SuggestionItem(
                    id=f"dir:{rel_path}",
                    display_text=rel_path + completion_suffix,
                    completion=f"@{rel_path}{completion_suffix}",
                    description="directory" if is_dir else "",
                    icon="◇",
                    source="directory",
                    score=score,
                )
            )

        return items
