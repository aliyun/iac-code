"""Terminal tool-failure memory so equivalent failing calls are not resent.

A same-origin client error (for example an Alibaba Cloud HTTP 400 that rejects a
template resource type) fails identically no matter how many times the exact
same request is repeated. Transport retry deliberately excludes those statuses,
so the repetition originates from the agent loop re-issuing an equivalent tool
call. This module remembers those terminal failures per
``(tool name, canonical input)`` so the second equivalent call is refused
locally instead of hitting the cloud again.
"""

from __future__ import annotations

from collections import OrderedDict

from iac_code.tools.base import ToolResult

TERMINAL_FAILURE_METADATA_KEY = "_iac_code_terminal_failure"

_DEFAULT_MAX_ENTRIES = 128


def terminal_failure_signature(*, status: int, code: str | None = None) -> str:
    """Build the stable, value-free signature recorded for a terminal failure."""
    suffix = f":{code}" if code else ""
    return f"http_{int(status)}{suffix}"


def mark_terminal_failure(result: ToolResult, signature: str) -> ToolResult:
    """Tag an error result as terminal for its exact request shape."""
    metadata = dict(result.metadata or {})
    metadata[TERMINAL_FAILURE_METADATA_KEY] = signature
    result.metadata = metadata
    return result


def take_terminal_failure(result: ToolResult) -> str | None:
    """Read and remove the internal terminal marker so it never leaves the executor."""
    metadata = result.metadata
    if not isinstance(metadata, dict):
        return None
    signature = metadata.pop(TERMINAL_FAILURE_METADATA_KEY, None)
    if not metadata:
        result.metadata = None
    return signature if isinstance(signature, str) and signature else None


class TerminalFailureLedger:
    """Bounded per-executor memory of terminally failed request shapes."""

    def __init__(self, *, max_entries: int = _DEFAULT_MAX_ENTRIES) -> None:
        self._max_entries = max_entries
        self._entries: OrderedDict[tuple[str, str], str] = OrderedDict()

    @staticmethod
    def _key(tool_name: str, canonical_input_sha256: str) -> tuple[str, str]:
        return (tool_name, canonical_input_sha256)

    def record(self, *, tool_name: str, canonical_input_sha256: str, signature: str) -> None:
        key = self._key(tool_name, canonical_input_sha256)
        self._entries.pop(key, None)
        self._entries[key] = signature
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def lookup(self, *, tool_name: str, canonical_input_sha256: str) -> str | None:
        key = self._key(tool_name, canonical_input_sha256)
        signature = self._entries.get(key)
        if signature is not None:
            self._entries.move_to_end(key)
        return signature
